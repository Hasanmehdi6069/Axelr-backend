import asyncio
import unittest
from unittest.mock import patch

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app


class RouterTests(unittest.TestCase):
    def test_route_ai_request_accepts_text_history_and_returns_success(self):
        async def run_test():
            async def fake_provider(prompt, max_tokens, temp):
                return "ok"

            with patch.object(app, "PROVIDER_CHAIN", [("mock", fake_provider, {})]), patch.object(app, "ai_cache", {}):
                result = await app.route_ai_request(
                    workspace="data",
                    task_type="extraction",
                    prompt="hello",
                    history=[{"role": "user", "text": "Earlier"}],
                    files=[],
                    max_tokens=256,
                    temp=0.2,
                    tier="free",
                )
                self.assertTrue(result["success"])
                self.assertEqual(result["text"], "ok")

        asyncio.run(run_test())

    def test_call_ollama_returns_message_content(self):
        async def run_test():
            async def fake_http_post_async(url, headers, json_data, timeout=90.0):
                self.assertEqual(url, "http://127.0.0.1:11434/api/chat")
                self.assertEqual(json_data["model"], "llama3.2:3b")
                return {"message": {"content": "hello from ollama"}}

            with patch.object(app, "http_post_async", side_effect=fake_http_post_async):
                result = await app.call_ollama("hello", 128, 0.2)
                self.assertEqual(result, "hello from ollama")

        asyncio.run(run_test())

    def test_provider_model_selection_uses_configured_models(self):
        with patch.dict(app.os.environ, {"OPENROUTER_MODEL": "meta-llama/llama-3.1-8b-instruct:free"}, clear=False):
            self.assertEqual(app.get_provider_model("openrouter", "extraction"), "meta-llama/llama-3.1-8b-instruct:free")

        with patch.dict(app.os.environ, {"SAMBANOVA_MODEL": "Meta-Llama-3.1-8B-Instruct"}, clear=False):
            self.assertEqual(app.get_provider_model("sambanova", "analytics"), "Meta-Llama-3.1-8B-Instruct")


if __name__ == "__main__":
    unittest.main()
