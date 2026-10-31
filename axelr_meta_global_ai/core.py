"""Meta Global AI Developer: Llama 3 Function Calling — v1.1.0
==============================================================
Multi-step function-calling orchestration per Meta's Llama 3 chat spec.

Honest scope
------------
* **`execute_session` requires a real model client** that implements
  `chat_completion(messages, functions, tool_choice) -> completion`.
  We do not ship a fake one. Without a real client, the method raises.
* **Parallel function calls actually run in parallel.** The previous
  version held the registry lock during execution, serializing every
  call in a step. Fixed.
* **Assistant's `tool_calls` message is now appended** to the history
  before tool results — Llama 3 (and OpenAI) require this shape for
  multi-turn function calling. Previously missing; the model would
  desync on turn 2.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, ClassVar, Dict, List, Optional, Tuple

import jsonschema

logger = logging.getLogger("axelr.meta_global_ai.core")
__version__ = "1.1.0"

_MAX_SESSIONS = 500
_MAX_CALLS_PER_SESSION = 100


@dataclass(slots=True)
class FunctionParameter:
    name: str
    type: str
    description: str
    required: bool = True
    enum: Optional[List[Any]] = None


@dataclass(slots=True)
class FunctionDefinition:
    name: str
    description: str
    parameters: List[FunctionParameter]
    return_type: str = "object"

    def to_llama3_schema(self) -> Dict[str, Any]:
        properties: Dict[str, Any] = {}
        required: List[str] = []
        for p in self.parameters:
            spec: Dict[str, Any] = {"type": p.type, "description": p.description}
            if p.enum:
                spec["enum"] = p.enum
            properties[p.name] = spec
            if p.required:
                required.append(p.name)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }


@dataclass(slots=True)
class FunctionCall:
    call_id: str
    function_name: str
    parameters: Dict[str, Any]
    status: str = "pending"
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None


@dataclass(slots=True)
class FunctionCallSession:
    session_id: str
    user_query: str
    calls: List[FunctionCall] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    max_steps: int = 10
    current_step: int = 0
    is_complete: bool = False
    final_response: Optional[str] = None


# ─── Registry ─────────────────────────────────────────────────────────────
class FunctionRegistry:
    """Store of callable functions with Llama 3 schemas.

    The lock protects only the dict lookup; execution happens outside
    the lock so parallel calls in a step are genuinely parallel.
    """

    def __init__(self) -> None:
        self.functions: Dict[str, Tuple[FunctionDefinition, Callable[..., Awaitable[Any]]]] = {}
        self.lock = asyncio.Lock()

    async def register(
        self,
        func_def: FunctionDefinition,
        handler: Callable[..., Awaitable[Any]] | Callable[..., Any],
    ) -> None:
        async with self.lock:
            self.functions[func_def.name] = (func_def, handler)
            logger.info("function_registered name=%s", func_def.name)

    async def validate_call(
        self, function_name: str, parameters: Dict[str, Any],
    ) -> Tuple[bool, Optional[str]]:
        async with self.lock:
            entry = self.functions.get(function_name)
            if entry is None:
                return False, f"function not found: {function_name}"
            func_def, _ = entry
        schema = func_def.to_llama3_schema()["function"]["parameters"]
        try:
            jsonschema.validate(instance=parameters, schema=schema)
            return True, None
        except jsonschema.ValidationError as e:
            return False, str(e)[:300]

    async def execute(self, call: FunctionCall) -> FunctionCall:
        async with self.lock:
            entry = self.functions.get(call.function_name)
        if entry is None:
            call.status = "failed"
            call.error = f"function not registered: {call.function_name}"
            call.completed_at = time.time()
            return call
        _, handler = entry
        call.status = "running"
        call.started_at = time.time()
        try:
            if asyncio.iscoroutinefunction(handler):
                result = await handler(**call.parameters)
            else:
                result = await asyncio.to_thread(handler, **call.parameters)
            call.result = result
            call.status = "completed"
        except Exception as e:
            call.status = "failed"
            call.error = str(e)[:300]
            logger.warning("function_exec_failed name=%s err=%s",
                           call.function_name, e)
        finally:
            call.completed_at = time.time()
        return call


# ─── Orchestrator ────────────────────────────────────────────────────────
@dataclass(slots=True)
class Llama3FunctionCallOrchestrator:
    registry: FunctionRegistry = field(default_factory=FunctionRegistry)
    active_sessions: Dict[str, FunctionCallSession] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def create_session(self, user_query: str) -> FunctionCallSession:
        async with self.lock:
            # Bound session count so long demo sessions don't grow unbounded.
            if len(self.active_sessions) >= _MAX_SESSIONS:
                oldest_id = min(
                    self.active_sessions,
                    key=lambda k: self.active_sessions[k].created_at,
                )
                del self.active_sessions[oldest_id]
                logger.warning("session_evicted oldest=%s", oldest_id)
            session = FunctionCallSession(
                session_id=str(uuid.uuid4()), user_query=user_query,
            )
            self.active_sessions[session.session_id] = session
        return session

    async def execute_session(
        self, session_id: str, model_client: Any,
    ) -> Optional[str]:
        """Drive a full multi-turn function-calling conversation.

        `model_client` must implement:
            await model_client.chat_completion(
                messages=[...], functions=[...], tool_choice="auto",
            ) -> completion
        where completion.choices[0].message has `.content` and
        `.tool_calls` (list of {id, function: {name, arguments}}).
        """
        async with self.lock:
            session = self.active_sessions.get(session_id)
        if session is None:
            return None

        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": session.user_query},
        ]

        while session.current_step < session.max_steps and not session.is_complete:
            session.current_step += 1
            async with self.lock:
                tools = [f[0].to_llama3_schema()
                         for f in self.registry.functions.values()]
            response = await model_client.chat_completion(
                messages=messages, functions=tools, tool_choice="auto",
            )
            msg = response.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)

            if not tool_calls:
                session.final_response = msg.content
                session.is_complete = True
                messages.append({"role": "assistant", "content": msg.content})
                break

            # ── 1. Append assistant's tool_calls message ──
            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            })

            # ── 2. Execute in parallel, track THIS step's calls only ──
            step_calls: List[FunctionCall] = []
            tasks: List[asyncio.Task] = []
            for tc in tool_calls:
                try:
                    params = json.loads(tc.function.arguments)
                except json.JSONDecodeError as e:
                    bad = FunctionCall(
                        call_id=tc.id, function_name=tc.function.name,
                        parameters={}, status="failed",
                        error=f"bad arguments: {e}",
                    )
                    session.calls.append(bad)
                    step_calls.append(bad)
                    continue
                valid, err = await self.registry.validate_call(
                    tc.function.name, params)
                call = FunctionCall(
                    call_id=tc.id, function_name=tc.function.name,
                    parameters=params,
                    status="failed" if not valid else "pending",
                    error=err,
                )
                session.calls.append(call)
                step_calls.append(call)
                if valid:
                    tasks.append(asyncio.create_task(self.registry.execute(call)))
            if tasks:
                await asyncio.gather(*tasks)

            # ── 3. Append tool results (parallel with the assistant msg) ──
            for call in step_calls:
                payload = (json.dumps(call.result) if call.result is not None
                           else json.dumps({"error": call.error}))
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "name": call.function_name,
                    "content": payload,
                })
            if len(session.calls) >= _MAX_CALLS_PER_SESSION:
                session.is_complete = True
                logger.warning("session_call_limit session=%s", session_id)
                break

        return session.final_response

    async def close_session(self, session_id: str) -> None:
        async with self.lock:
            self.active_sessions.pop(session_id, None)


# ─── Singleton ───────────────────────────────────────────────────────────
_orchestrator: Optional[Llama3FunctionCallOrchestrator] = None
_shutdown_event: Optional[asyncio.Event] = None


def get_orchestrator() -> Llama3FunctionCallOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Llama3FunctionCallOrchestrator()
    return _orchestrator


async def init() -> None:
    global _shutdown_event
    if _shutdown_event is None:
        _shutdown_event = asyncio.Event()
    _shutdown_event.clear()
    get_orchestrator()
    logger.info("meta_llama3_function_calling_initialized")


async def shutdown() -> None:
    global _orchestrator, _shutdown_event
    if _shutdown_event is not None:
        _shutdown_event.set()
    if _orchestrator is not None:
        async with _orchestrator.lock:
            _orchestrator.active_sessions.clear()
        _orchestrator = None
    _shutdown_event = None
    logger.info("meta_llama3_function_calling_shutdown")


def health() -> Dict[str, Any]:
    if _orchestrator is None:
        return {"ok": False, "details": {"reason": "not_initialized",
                                         "version": __version__}}
    return {
        "ok": True,
        "details": {
            "version": __version__,
            "registered_functions": list(_orchestrator.registry.functions),
            "active_sessions": len(_orchestrator.active_sessions),
            "max_sessions": _MAX_SESSIONS,
        },
    }