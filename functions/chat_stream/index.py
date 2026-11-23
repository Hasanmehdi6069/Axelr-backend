import os
import json
from typing import AsyncGenerator, Dict, Any

async def stream_ai_response_from_provider(
    workspace: str,
    task_type: str,
    prompt: str,
    history: list,
    files: list,
    max_tokens: int,
    temp: float,
    tier: str,
    user: dict,
    context: str
) -> AsyncGenerator[str, None]:
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        yield json.dumps({"error": "No LLM API key configured"}) + "\n\n"
        return

    yield json.dumps({"text": "Streaming initialized...", "type": "token"}) + "\n\n"
    yield json.dumps({"type": "done"}) + "\n\n"

async def main(context):
    req = context.req
    res = context.res
    
    headers = {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    res.headers(headers)
    
    async def stream_generator():
        try:
            body = {}
            if req.body:
                try:
                    body = json.loads(req.body) if isinstance(req.body, str) else req.body
                except:
                    body = {}
            
            workspace = body.get("workspace", "core")
            task_type = body.get("task_type", "structuring")
            prompt = body.get("prompt", "")
            history = body.get("history", [])
            files = body.get("files", [])
            max_tokens = body.get("max_tokens", 4096)
            temp = body.get("temperature", 0.7)
            tier = body.get("tier", "free")
            user = body.get("user", {})
            context_str = body.get("context", "")
            
            async for token_event in stream_ai_response_from_provider(
                workspace=workspace,
                task_type=task_type,
                prompt=prompt,
                history=history,
                files=files,
                max_tokens=max_tokens,
                temp=temp,
                tier=tier,
                user=user,
                context=context_str
            ):
                yield f"data: {token_event}\n\n"
            
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e), 'type': 'error'})}\n\n"
    
    await res.sendStream(stream_generator())