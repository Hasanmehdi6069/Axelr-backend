# orchestrator.py – Zero‑error, pure Python, runs on any machine
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import time
from typing import Optional, List, Dict, Any

app = FastAPI()

# Use TinyLlama – only 1.1B params, fits in ~2.2 GB RAM
MODEL_NAME = "TinyLlama/TinyLlama-1.1B-Chat-v1.0"

print("Loading model... this may take a few minutes on first run.")
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
model = AutoModelForCausalLM.from_pretrained(
    MODEL_NAME,
    torch_dtype=torch.float16,   # reduces memory to ~2.2 GB
    low_cpu_mem_usage=True
)
model.eval()
print("Model loaded!")

class RouteRequest(BaseModel):
    workspace: str   # "data" or "design"
    prompt: str
    history: Optional[List[Dict[str, Any]]] = None
    max_tokens: Optional[int] = 512
    temperature: Optional[float] = 0.2

@app.post("/api/route")
async def route(req: RouteRequest):
    start = time.time()
    # Build prompt
    if req.history:
        history_text = "\n".join([f"{m['role']}: {m['content']}" for m in req.history[-4:]])
        prompt_text = f"{history_text}\nuser: {req.prompt}\nassistant:"
    else:
        prompt_text = f"user: {req.prompt}\nassistant:"

    inputs = tokenizer(prompt_text, return_tensors="pt", truncation=True, max_length=1024)
    with torch.no_grad():
        outputs = model.generate(
            inputs.input_ids,
            max_new_tokens=req.max_tokens,
            temperature=req.temperature,
            do_sample=True,
            pad_token_id=tokenizer.eos_token_id
        )
    response = tokenizer.decode(outputs[0], skip_special_tokens=True)
    # Remove the prompt part from response
    if response.startswith(prompt_text):
        response = response[len(prompt_text):].strip()
    latency = (time.time() - start) * 1000
    return {
        "success": True,
        "text": response,
        "provider": "local",
        "model_used": MODEL_NAME,
        "tokens_used": len(response.split()),
        "latency_ms": round(latency, 2)
    }

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5001)