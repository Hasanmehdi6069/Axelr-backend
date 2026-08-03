import asyncio
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

if __name__ == '__main__':
    asyncio.run(main())
