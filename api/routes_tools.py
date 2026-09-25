# api/routes_tools.py
"""
AXELR — Tooling surface.
========================
Everything extracted from routes_core.py that isn't the canonical chat path:

  * /api/tools/{tool} + TOOL_PROMPTS / TOOL_CONFIGS / ToolRequest
  * legacy redirect stubs (/api/refactor, /api/explain-code, …)
  * /api/execute-code, /api/deploy, /api/touch_fix, /api/generate_api
  * /api/refine-response, /api/summarize-chat, /api/session/{id}/stream
  * /api/suggestions
  * elite tools: translate-code, mermaid, scan-pii, meeting-minutes,
    decision-matrix
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import time
import urllib.parse
import zipfile
from datetime import datetime
from typing import Any

import httpx
import jinja2
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel

from .auth import get_current_user
from .config import NETLIFY_ACCESS_TOKEN
from .quota import check_rate_limit
from .routes_core import route_ai_request_parallel
from .state import (
    get_object_id,
    get_redis_cache,
    limiter,
    set_redis_cache,
    state,
)

import structlog
logger = structlog.get_logger("axelr.tools")

router = APIRouter(tags=["tools"])


# Guarded worker-client import (same pattern used in routes_core.py).
try:
    from core.worker_client import execute_code_on_worker
except Exception:
    async def execute_code_on_worker(language, code, timeout=8):
        return {"success": False, "output": "", "error": "worker_unavailable"}


# ═══════════════════════════════════════════════════════════════════════════
# Tools
# ═══════════════════════════════════════════════════════════════════════════

TOOL_PROMPTS = {
    "refactor":   "Refactor the following code for better readability, performance, and accessibility. Return only the refactored code.\n\n```\n{input}\n```",
    "explain":    "Explain the following code in clear, simple terms (max 200 words).\n\n```\n{input}\n```",
    "tests":      "Generate unit tests for the following code using an appropriate framework.\n\n```\n{input}\n```",
    "summarize":  "Summarize the following text concisely (max 150 words):\n\n{input}",
    "brainstorm": "Brainstorm 10 creative, actionable ideas related to: {input}. List them with brief explanations.",
}

TOOL_CONFIGS = {
    "refactor":   {"workspace": "design", "max_tokens": 2048, "temp": 0.2, "key": "refactored_code"},
    "explain":    {"workspace": "design", "max_tokens": 1024, "temp": 0.3, "key": "explanation"},
    "tests":      {"workspace": "design", "max_tokens": 2048, "temp": 0.2, "key": "tests"},
    "summarize":  {"workspace": "core",   "max_tokens": 512,  "temp": 0.3, "key": "summary"},
    "brainstorm": {"workspace": "core",   "max_tokens": 1024, "temp": 0.7, "key": "ideas"},
}


class ToolRequest(BaseModel):
    code: str | None = None
    text: str | None = None


@router.post("/api/tools/{tool}")
@limiter.limit("10/minute")
async def run_tool(request: Request, tool: str, data: ToolRequest,
                    user: dict = Depends(get_current_user)):
    if tool not in TOOL_PROMPTS:
        raise HTTPException(status_code=404, detail="Tool not found")
    input_data = data.code if data.code is not None else data.text
    if not input_data:
        raise HTTPException(status_code=400, detail="No input provided")

    config = TOOL_CONFIGS[tool]
    prompt = TOOL_PROMPTS[tool].format(input=input_data)
    ai_result = await route_ai_request_parallel(
        workspace=config["workspace"], task_type=tool, prompt=prompt,
        history=[], files=[], max_tokens=config["max_tokens"],
        temp=config["temp"], tier=user.get("tier", "free"), user=user,
    )
    if not ai_result.get("success"):
        raise HTTPException(status_code=503, detail="AI service unavailable")

    result_text = ai_result["text"]
    if tool in ("refactor", "tests"):
        m = re.search(r"```(?:html|javascript|css|python|js)?\s*([\s\S]*?)```", result_text, re.DOTALL)
        if m:
            result_text = m.group(1).strip()

    return {"success": True, config["key"]: result_text}


# Legacy redirects
for _old, _new in (
    ("/api/refactor", "/api/tools/refactor"),
    ("/api/explain-code", "/api/tools/explain"),
    ("/api/generate-tests", "/api/tools/tests"),
    ("/api/summarize", "/api/tools/summarize"),
    ("/api/brainstorm", "/api/tools/brainstorm"),
):
    def _make_redirect(target: str):
        async def _redirect(request: Request):
            return RedirectResponse(url=target, status_code=307)
        return _redirect
    router.add_api_route(_old, _make_redirect(_new), methods=["POST"],
                         include_in_schema=False, name=f"redirect_{_old.strip('/')}")


# ═══════════════════════════════════════════════════════════════════════════
# Execute code
# ═══════════════════════════════════════════════════════════════════════════

class ExecuteRequest(BaseModel):
    language: str
    code: str
    timeout: int | None = 5


@router.post("/api/execute-code")
async def execute_code_endpoint(data: ExecuteRequest, user: dict = Depends(get_current_user)):
    if len(data.code) > 40_000:
        raise HTTPException(status_code=413, detail="code_too_large")
    timeout = max(1, min(int(data.timeout or 5), 10))
    return await execute_code_on_worker(data.language, data.code, timeout)


# ═══════════════════════════════════════════════════════════════════════════
# Deploy
# ═══════════════════════════════════════════════════════════════════════════

class DeployRequest(BaseModel):
    htmlContent: str


@router.post("/api/deploy")
async def deploy(data: DeployRequest, user: dict = Depends(get_current_user)):
    html = data.htmlContent
    if not html:
        raise HTTPException(status_code=400, detail="Missing HTML content")
    if "<html" not in html or "</html>" not in html:
        raise HTTPException(status_code=400, detail="Generated HTML is incomplete.")

    # nh3-based sanitization
    try:
        import nh3
        ALLOWED_TAGS = {
            "html", "head", "title", "body", "div", "span", "p", "a", "img",
            "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
            "table", "thead", "tbody", "tr", "th", "td",
            "form", "input", "button", "select", "option", "textarea", "label",
            "fieldset", "legend", "style", "script", "link", "meta",
            "header", "footer", "nav", "section", "article", "aside", "main",
            "figure", "figcaption", "canvas", "svg",
            "path", "circle", "rect", "line", "polygon", "g", "defs", "use",
            "blockquote", "pre", "code", "br", "hr",
            "strong", "em", "b", "i", "u", "s", "sub", "sup", "mark", "small",
            "del", "ins", "details", "summary", "dialog", "menu", "menuitem",
        }
        ALLOWED_ATTRS = {
            "*": ["class", "id", "style", "title", "lang", "dir", "hidden",
                  "tabindex", "role"],
            "a": ["href", "target", "rel", "download", "type"],
            "img": ["src", "alt", "width", "height", "loading", "decoding",
                    "crossorigin", "srcset", "sizes"],
            "iframe": ["src", "width", "height", "allow", "allowfullscreen",
                       "loading", "referrerpolicy", "sandbox"],
            "input": ["type", "name", "value", "placeholder", "checked",
                      "disabled", "readonly", "required", "min", "max", "step",
                      "pattern", "autocomplete", "autofocus", "multiple"],
            "button": ["type", "name", "value", "disabled"],
            "select": ["name", "multiple", "disabled", "required", "size"],
            "option": ["value", "selected", "disabled"],
            "textarea": ["name", "rows", "cols", "disabled", "readonly",
                         "required", "placeholder", "wrap"],
            "form": ["action", "method", "enctype", "target", "novalidate",
                     "autocomplete"],
            "style": ["type", "media"],
            "script": ["type", "src", "async", "defer", "integrity", "crossorigin"],
            "link": ["href", "rel", "type", "media", "crossorigin", "integrity"],
            "meta": ["name", "content", "charset", "http-equiv"],
        }
        sanitized = nh3.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True)
    except ImportError:
        sanitized = re.sub(r"<script\b[^>]*>.*?</script>", "", html,
                           flags=re.DOTALL | re.IGNORECASE)
        sanitized = re.sub(r'\son\w+\s*=\s*"[^"]*"', "", sanitized, flags=re.IGNORECASE)
        sanitized = re.sub(r"\son\w+\s*=\s*'[^']*'", "", sanitized, flags=re.IGNORECASE)

    if NETLIFY_ACCESS_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                create_resp = await client.post(
                    "https://api.netlify.com/api/v1/sites",
                    headers={"Authorization": f"Bearer {NETLIFY_ACCESS_TOKEN}",
                             "Content-Type": "application/json"},
                    json={"name": f"axelr-deploy-{int(time.time())}"},
                )
                create_data = create_resp.json()
                if create_resp.status_code == 200 and create_data.get("id"):
                    site_id = create_data["id"]
                    files = {"file": ("index.html", sanitized.encode("utf-8"), "text/html")}
                    deploy_resp = await client.post(
                        f"https://api.netlify.com/api/v1/sites/{site_id}/deploys",
                        headers={"Authorization": f"Bearer {NETLIFY_ACCESS_TOKEN}"},
                        files=files,
                    )
                    deploy_data = deploy_resp.json()
                    if deploy_resp.status_code == 200 and deploy_data.get("deploy_url"):
                        return {"success": True, "liveUrl": deploy_data["deploy_url"]}
        except Exception as e:
            logger.warning("netlify_deploy_failed error=%s", e)

    data_uri = f"data:text/html;charset=utf-8,{urllib.parse.quote(sanitized)}"
    return {"success": True, "liveUrl": data_uri, "message": "Preview via data URI."}


# ═══════════════════════════════════════════════════════════════════════════
# Touch-fix
# ═══════════════════════════════════════════════════════════════════════════

class TouchFixRequest(BaseModel):
    code: str
    error_message: str
    task_type: str | None = "touch_fix"
    diff: str | None = None


@router.post("/api/touch_fix")
async def touch_fix_endpoint(data: TouchFixRequest, user: dict = Depends(get_current_user)):
    engine = state.touch_fix_engine
    if engine is None:
        try:
            from core.healing import TouchFixEngine   # was: core.code_fixer
            engine = TouchFixEngine(route_func=route_ai_request_parallel)
            state.touch_fix_engine = engine
        except Exception:
            raise HTTPException(status_code=503, detail="TouchFix unavailable")

    if data.diff:
        return {"success": True, "fixed_code": engine.apply_diff(data.code, data.diff)}

    fixed_code = await engine.fix_block(
        full_code=data.code, error_block=data.code,
        error_message=data.error_message, language="html",
        tier=user.get("tier", "free"), user=user,
    )
    return {"success": True, "fixed_code": fixed_code}


# ═══════════════════════════════════════════════════════════════════════════
# Generate API microservice
# ═══════════════════════════════════════════════════════════════════════════

_jinja_env = jinja2.Environment(loader=jinja2.DictLoader({
    "fastapi_template.py.j2": """
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional

app = FastAPI()

class Item(BaseModel):
{% for f in fields %}    {{ f.name }}: {{ f.type }}
{% endfor %}

items: List[Item] = []
counter = 1

@app.get("/items", response_model=List[Item])
async def list_items():
    return items

@app.post("/items", response_model=Item, status_code=201)
async def create_item(payload: Item):
    global counter
    data = payload.dict()
    data["id"] = counter
    counter += 1
    item = Item(**data)
    items.append(item)
    return item

@app.get("/items/{item_id}", response_model=Item)
async def get_item(item_id: int):
    for item in items:
        if item.id == item_id:
            return item
    raise HTTPException(status_code=404, detail="Item not found")

@app.put("/items/{item_id}", response_model=Item)
async def update_item(item_id: int, updated: Item):
    for idx, item in enumerate(items):
        if item.id == item_id:
            data = updated.dict()
            data["id"] = item_id
            items[idx] = Item(**data)
            return items[idx]
    raise HTTPException(status_code=404, detail="Item not found")

@app.delete("/items/{item_id}")
async def delete_item(item_id: int):
    for idx, item in enumerate(items):
        if item.id == item_id:
            items.pop(idx)
            return {"message": "Deleted"}
    raise HTTPException(status_code=404, detail="Item not found")
""",
    "express_template.js.j2": """
const express = require('express');
const app = express();
app.use(express.json());

let items = [];
let counter = 1;

app.get('/items', (req, res) => res.json(items));
app.get('/items/:id', (req, res) => {
    const item = items.find(i => i.id === parseInt(req.params.id));
    if (!item) return res.status(404).json({error: 'Not found'});
    res.json(item);
});
app.post('/items', (req, res) => {
    const newItem = {...req.body, id: counter++};
    items.push(newItem);
    res.status(201).json(newItem);
});
app.put('/items/:id', (req, res) => {
    const idx = items.findIndex(i => i.id === parseInt(req.params.id));
    if (idx === -1) return res.status(404).json({error: 'Not found'});
    const updated = {...req.body, id: parseInt(req.params.id)};
    items[idx] = updated;
    res.json(updated);
});
app.delete('/items/:id', (req, res) => {
    const idx = items.findIndex(i => i.id === parseInt(req.params.id));
    if (idx === -1) return res.status(404).json({error: 'Not found'});
    items.splice(idx, 1);
    res.json({message: 'Deleted'});
});

app.listen(3000, () => console.log('Server running on port 3000'));
""",
}))


class GenerateAPIRequest(BaseModel):
    data: list[dict[str, Any]]
    language: str = "python"


@router.post("/api/generate_api")
async def generate_api(request: Request, req: GenerateAPIRequest,
                        user: dict = Depends(get_current_user)):
    data = req.data
    language = req.language
    if not data:
        raise HTTPException(status_code=400, detail="No data provided")
    if language not in ("python", "javascript"):
        raise HTTPException(status_code=400, detail="Unsupported language")

    sample = data[0]
    type_map = {int: "int", float: "float", str: "str", bool: "bool",
                list: "List", dict: "dict"}
    fields = []
    for key, value in sample.items():
        fields.append({"name": key, "type": type_map.get(type(value), "str")})
    if "id" not in [f["name"] for f in fields]:
        fields.insert(0, {"name": "id", "type": "int"})

    if language == "python":
        code = _jinja_env.get_template("fastapi_template.py.j2").render(fields=fields)
        filename, requirements = "api.py", "fastapi\nuvicorn\npydantic"
    else:
        code = _jinja_env.get_template("express_template.js.j2").render(fields=fields)
        filename, requirements = "server.js", "express"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(filename, code)
        if language == "python":
            zf.writestr("requirements.txt", requirements)
        else:
            zf.writestr("package.json", json.dumps({
                "name": "generated-api", "version": "1.0.0",
                "scripts": {"start": "node server.js"},
                "dependencies": {"express": "^4.18.2"},
            }, indent=2))
    buf.seek(0)

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=api_microservice_{language}.zip"},
    )


# ═══════════════════════════════════════════════════════════════════════════
# Refine / summarize-chat / collab / suggestions
# ═══════════════════════════════════════════════════════════════════════════

class RefineRequest(BaseModel):
    sessionId: str
    msgId: str
    newText: str
    originalText: str


@router.post("/api/refine-response")
async def refine_response(data: RefineRequest, user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    ObjectId = get_object_id()
    if not ObjectId or not ObjectId.is_valid(data.sessionId):
        raise HTTPException(400, "Invalid session ID")
    session = await state.sessions_col.find_one(
        {"_id": ObjectId(data.sessionId), "userId": user["_id"]}
    )
    if not session:
        raise HTTPException(404, "Session not found")

    messages = session.get("messages", [])
    msg_index = next((i for i, m in enumerate(messages)
                      if str(m.get("_id")) == data.msgId), -1)
    if msg_index == -1:
        raise HTTPException(404, "Message not found")

    messages[msg_index]["text"] = data.newText
    messages = messages[:msg_index + 1]

    history = messages[:-1]
    command = messages[-1].get("text", "")
    workspace = session.get("workspace", "core")

    result = await route_ai_request_parallel(
        workspace=workspace, task_type="structuring", prompt=command,
        history=history, files=[], max_tokens=2048, temp=0.5,
        tier=user.get("tier", "free"), user=user, context="",
    )
    if not result.get("success"):
        raise HTTPException(503, "AI service unavailable")

    new_response = result["text"]
    messages.append({
        "role": "model", "text": new_response,
        "variants": [new_response], "activeVariant": 0,
        "canRegenerate": True, "createdAt": datetime.utcnow(),
    })
    await state.sessions_col.update_one(
        {"_id": ObjectId(data.sessionId)},
        {"$set": {"messages": messages}},
    )
    return {"success": True, "refined": new_response}


@router.post("/api/summarize-chat")
async def summarize_chat(session_id: str, user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    ObjectId = get_object_id()
    if not ObjectId or not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")
    session = await state.sessions_col.find_one(
        {"_id": ObjectId(session_id), "userId": user["_id"]}
    )
    if not session:
        raise HTTPException(404, "Session not found")
    messages = session.get("messages", [])
    if not messages:
        return {"summary": "No messages to summarize."}

    conv = "\n".join(f"{m['role']}: {m['text']}" for m in messages if m.get("text"))
    prompt = f"Summarize the following conversation concisely (max 300 words). Include key decisions, questions, and answers.\n\n{conv}"
    result = await route_ai_request_parallel(
        workspace="core", task_type="summarize", prompt=prompt,
        history=[], files=[], max_tokens=1024, temp=0.2,
        tier=user.get("tier", "free"), user=user,
    )
    return {"success": True, "summary": result.get("text", "Unable to summarize.")}


@router.get("/api/session/{session_id}/stream")
async def session_stream(session_id: str, request: Request,
                          user: dict = Depends(get_current_user)):
    if not state.db_available:
        raise HTTPException(503, "Database unavailable")
    ObjectId = get_object_id()
    if not ObjectId or not ObjectId.is_valid(session_id):
        raise HTTPException(400, "Invalid session ID")
    session = await state.sessions_col.find_one(
        {"_id": ObjectId(session_id), "userId": user["_id"]}
    )
    if not session:
        raise HTTPException(404, "Session not found")

    async def event_generator():
        session_key = f"session:clients:{session_id}"
        clients_data = await get_redis_cache(session_key)
        clients = set(clients_data) if clients_data else set()
        clients.add(str(user["_id"]))
        await set_redis_cache(session_key, list(clients), ttl=3600)
        try:
            yield f"data: {json.dumps({'type': 'init', 'messages': session.get('messages', [])})}\n\n"
            last_count = len(session.get("messages", []))
            last_beat = time.time()
            while True:
                if await request.is_disconnected():
                    break
                await asyncio.sleep(2)
                if time.time() - last_beat > 15:
                    yield ": heartbeat\n\n"
                    last_beat = time.time()
                updated = await state.sessions_col.find_one({"_id": ObjectId(session_id)})
                if not updated:
                    break
                msgs = updated.get("messages", [])
                if len(msgs) > last_count:
                    for m in msgs[last_count:]:
                        yield f"data: {json.dumps({'type': 'new_message', 'message': m})}\n\n"
                    last_count = len(msgs)
        finally:
            clients_data = await get_redis_cache(session_key)
            clients = set(clients_data) if clients_data else set()
            clients.discard(str(user["_id"]))
            await set_redis_cache(session_key, list(clients), ttl=3600)

    return StreamingResponse(
        event_generator(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


@router.get("/api/suggestions")
async def get_suggestions(workspace: str = "data",
                           user: dict = Depends(get_current_user)):
    suggestions = {
        "data": [
            "Extract key metrics from this invoice",
            "Analyze sales data and identify trends",
            "Clean and transform this dataset",
            "Generate a summary of this CSV file",
            "Compare these two spreadsheets",
        ],
        "design": [
            "Design a responsive navbar with dropdown",
            "Create a dark mode toggle button",
            "Generate a pricing card component",
            "Build a login form with validation",
            "Make this existing page mobile-friendly",
        ],
        "core": [
            "Summarize this text",
            "Explain this concept in simple terms",
            "Draft a professional email",
            "Provide a step-by-step guide",
            "Brainstorm ideas for a project",
        ],
    }
    return {"suggestions": suggestions.get(workspace, suggestions["core"])}


# ═══════════════════════════════════════════════════════════════════════════
# Elite tools
# ═══════════════════════════════════════════════════════════════════════════

class CodeTranslateRequest(BaseModel):
    code: str
    source_lang: str
    target_lang: str


@router.post("/api/tools/translate-code")
async def tool_translate_code(data: CodeTranslateRequest,
                               user: dict = Depends(get_current_user)):
    prompt = (
        f"Translate the following code from {data.source_lang} to {data.target_lang}. "
        f"Preserve idiomatic patterns, comments, and safety. Return only the code block.\n\n"
        f"```{data.source_lang}\n{data.code}\n```"
    )
    res = await route_ai_request_parallel(
        "design", "structuring", prompt, [], [], 4096, 0.2,
        user.get("tier", "free"), user,
    )
    return {"success": True, "translated_code": res["text"]}


class MermaidRequest(BaseModel):
    process_description: str


@router.post("/api/tools/mermaid")
async def tool_mermaid_generator(data: MermaidRequest,
                                  user: dict = Depends(get_current_user)):
    prompt = (
        f"Generate a syntactically valid Mermaid.js diagram representing this process:\n\n"
        f"{data.process_description}\n\nOutput ONLY a valid ```mermaid code block."
    )
    res = await route_ai_request_parallel(
        "core", "structuring", prompt, [], [], 2048, 0.2,
        user.get("tier", "free"), user,
    )
    return {"success": True, "mermaid": res["text"]}


class PIIScanRequest(BaseModel):
    document_text: str


@router.post("/api/tools/scan-pii")
async def tool_pii_scanner(data: PIIScanRequest, user: dict = Depends(get_current_user)):
    prompt = (
        "Audit this text for PII including names, emails, phones, IPs, credentials, "
        "and financial references. Return a clean JSON array: "
        "[{'type': str, 'value': str, 'risk': 'low'|'medium'|'high'}].\n\n"
        f"Text:\n{data.document_text[:8000]}"
    )
    res = await route_ai_request_parallel(
        "data", "extraction", prompt, [], [], 2048, 0.1,
        user.get("tier", "free"), user,
    )
    return {"success": True, "scan_report": res["text"]}


class MeetingMinutesRequest(BaseModel):
    transcript: str


@router.post("/api/tools/meeting-minutes")
async def tool_meeting_minutes(data: MeetingMinutesRequest,
                                user: dict = Depends(get_current_user)):
    prompt = (
        "Extract structured meeting minutes:\n"
        "1. Executive Summary\n2. Key Decisions\n"
        "3. Action Items Table: Task, Owner, Priority, Target Date.\n\n"
        f"Transcript:\n{data.transcript[:10000]}"
    )
    res = await route_ai_request_parallel(
        "core", "structuring", prompt, [], [], 4096, 0.2,
        user.get("tier", "free"), user,
    )
    return {"success": True, "minutes": res["text"]}


class DecisionMatrixRequest(BaseModel):
    options: list[str]
    criteria: list[str]
    context: str | None = ""


@router.post("/api/tools/decision-matrix")
async def tool_decision_matrix(data: DecisionMatrixRequest,
                                user: dict = Depends(get_current_user)):
    prompt = (
        f"Build a weighted Decision Matrix comparing: {', '.join(data.options)}.\n"
        f"Criteria: {', '.join(data.criteria)}.\nContext: {data.context}\n\n"
        "Provide a Markdown table with weights (1-5), ratings (1-10), total scores, "
        "and a decisive recommendation."
    )
    res = await route_ai_request_parallel(
        "data", "extraction", prompt, [], [], 4096, 0.3,
        user.get("tier", "free"), user,
    )
    return {"success": True, "matrix": res["text"]}


__all__ = [
    "router",
    "TOOL_PROMPTS", "TOOL_CONFIGS", "ToolRequest",
    "ExecuteRequest", "DeployRequest", "TouchFixRequest",
    "GenerateAPIRequest", "RefineRequest",
    "CodeTranslateRequest", "MermaidRequest", "PIIScanRequest",
    "MeetingMinutesRequest", "DecisionMatrixRequest",
]