import asyncio
from venv import logger
import app

async def main():
    for i in range(5):
        result = await app.route_ai_request(
            workspace='data',
            task_type='extraction',
            prompt=f'hello world {i}',
            history=[],
            files=[],
            max_tokens=256,
            temp=0.2,
            tier='free'
        )
        print(i, result['provider'], result['model_used'], result['text'][:120])
async def call_local_fallback(prompt, max_tokens, temp, workspace, task_type):
    logger.warning("All AI providers failed – using local fallback. Prompt: %s", prompt[:100])
    # Return a friendly but honest message
    return (
        "⚠️ **AI services are temporarily unavailable.**\n\n"
        "Our external providers are experiencing high load. "
        "Please try again in a few minutes. "
        "If the issue persists, contact support.\n\n"
        f"Your request: \"{prompt[:200]}\""
    )
if __name__ == '__main__':
    asyncio.run(main())
