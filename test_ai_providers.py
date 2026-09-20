import pytest
import asyncio
import sys
import os

# Add the project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import stream_ai_response

@pytest.mark.asyncio
async def test_all_ai_providers():
    providers = [
        "groq", "openrouter", "cloudflare", "mistral", "huggingface", 
        "gemini", "nararouter", "text_cortex", "bazaarlink", "siliconflow", 
        "agnes", "ollama", "anyapi", "modelscope", "ovhcloud", "requesty", 
        "manifest", "glama", "zai", "teamorouter"
    ]
    
    async def test_provider(provider):
        try:
            response_generator = stream_ai_response(
                workspace="general",
                task_type="chat",
                prompt="hello",
                history=[],
                files=[],
                max_tokens=10,
                temp=0.1,
                tier=provider,
                user=None,
                context=""
            )
            
            response = ""
            async for chunk in response_generator:
                response += chunk
            
            print(f"Provider {provider}: OK, Response: {response}")
            return True
        except Exception as e:
            print(f"Provider {provider}: FAILED, Error: {e}")
            return False

    results = await asyncio.gather(*[test_provider(p) for p in providers])
    
    assert all(results), "Not all AI providers are operational."