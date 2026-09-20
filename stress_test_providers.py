#!/usr/bin/env python3
"""
AXELR AI - PROVIDER STRESS TEST v1.2
Tests failover behavior and circuit breakers under load.
Run: python stress_test_providers.py [--requests 10] [--concurrency 3]
"""

import argparse
import asyncio
import json
import ssl
import time

import httpx
from dotenv import load_dotenv

load_dotenv(override=True)

# Disable SSL verification for development
ssl._create_default_https_context = ssl._create_unverified_context


class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    END = '\033[0m'


import sys

sys.path.append('.')
from app import (
    CLOUDFLARE_ACCOUNT_ID,
    CLOUDFLARE_API_TOKEN,
    GEMINI_API_KEY,
    GITHUB_MODELS_TOKEN,
    GROQ_API_KEY,
    HF_API_KEY,
    MISTRAL_API_KEY,
    NROUTER_API_KEY,
    OPENROUTER_API_KEY,
    PROVIDER_CHAIN,
    PROVIDER_MODELS,
)

# Provider availability flags – updated list (removed Cerebras, DeepSeek, Perplexity, Pollinations)
AVAILABLE_PROVIDERS = {
    "gemini": bool(GEMINI_API_KEY),
    "groq": bool(GROQ_API_KEY),
    "cloudflare": bool(CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID),
    "openrouter": bool(OPENROUTER_API_KEY),
    "mistral": bool(MISTRAL_API_KEY),
    "huggingface": bool(HF_API_KEY),
    "github_models": bool(GITHUB_MODELS_TOKEN),
    "nrouter": bool(NROUTER_API_KEY),
    # Add other free providers that don't need keys (they are always "available")
    "puter": True,
    "freetheai": True,
    "omnigpt_gateway": True,
    "opencode_zen": True,
    "freeflow": True,
    "qoder": True,
    "keylessai": True,
    "chubvenus": True,
    "blockrun": True,
    "aymo": True,
    "zerotwo": True,
    "aihubmix": True,
    "aisure": True,
}


class StressTester:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(10.0), verify=False)
        self.results: list[dict] = []
        self.failures: dict[str, int] = {}
        self.successes: dict[str, int] = {}
        self.latencies: dict[str, list[float]] = {}

    async def test_provider(self, provider_name: str, func, prompt: str, model: str) -> dict:
        start = time.time()
        success = False
        error = None
        response = None

        try:
            if provider_name == "local":
                return {"provider": provider_name, "status": "skipped", "reason": "Local fallback not tested"}

            # Skip if provider not available (key missing)
            if not AVAILABLE_PROVIDERS.get(provider_name, False):
                return {"provider": provider_name, "status": "skipped", "reason": "Not configured"}

            response = await func(prompt, 10, 0.0, model)
            if response and "OK" in response:
                success = True
        except Exception as e:
            error = str(e)[:100]

        elapsed = (time.time() - start) * 1000

        return {
            "provider": provider_name,
            "status": "success" if success else "failure",
            "elapsed_ms": elapsed,
            "error": error,
            "response": response[:50] if response else None
        }

    async def run_stress_test(self, num_requests: int = 10, concurrent: int = 3):
        print(f"\n{Colors.HEADER}{Colors.BOLD}╔══════════════════════════════════════════════════╗{Colors.END}")
        print(f"{Colors.HEADER}{Colors.BOLD}║     AXELR AI - PROVIDER STRESS TEST v1.2         ║{Colors.END}")
        print(f"{Colors.HEADER}{Colors.BOLD}╚══════════════════════════════════════════════════╝{Colors.END}\n")

        print(f"Running {num_requests} requests with {concurrent} concurrent...\n")

        tasks = []
        for i in range(num_requests):
            provider_idx = i % len(PROVIDER_CHAIN)
            provider_name, func = PROVIDER_CHAIN[provider_idx]

            models = PROVIDER_MODELS.get(provider_name, [])
            model = models[0] if models else None

            if not model and provider_name != "local":
                continue

            tasks.append(self.test_provider(provider_name, func, "Say 'OK'", model))

        semaphore = asyncio.Semaphore(concurrent)

        async def limited_task(task):
            async with semaphore:
                return await task

        print(f"{Colors.CYAN}Starting stress test...{Colors.END}\n")
        start_time = time.time()

        results = []
        for i in range(0, len(tasks), concurrent):
            batch = tasks[i:i+concurrent]
            batch_results = await asyncio.gather(*[limited_task(t) for t in batch])
            results.extend(batch_results)
            done = min(i + concurrent, len(tasks))
            print(f"Progress: {done}/{len(tasks)}", end="\r")

        print("\n")
        elapsed = time.time() - start_time

        self.results = results
        self.analyze_results(elapsed)

    def analyze_results(self, elapsed: float):
        total = len(self.results)
        successes = [r for r in self.results if r.get("status") == "success"]
        failures = [r for r in self.results if r.get("status") == "failure"]
        skipped = [r for r in self.results if r.get("status") == "skipped"]

        provider_stats: dict[str, dict] = {}
        for r in self.results:
            name = r["provider"]
            if name not in provider_stats:
                provider_stats[name] = {"success": 0, "failure": 0, "latencies": []}
            if r["status"] == "success":
                provider_stats[name]["success"] += 1
                provider_stats[name]["latencies"].append(r["elapsed_ms"])
            elif r["status"] == "failure":
                provider_stats[name]["failure"] += 1

        print(f"\n{Colors.BOLD}{'='*60}{Colors.END}")
        print(f"{Colors.BOLD}{'STRESS TEST RESULTS':^60}{Colors.END}")
        print(f"{Colors.BOLD}{'='*60}{Colors.END}\n")

        print(f"Total Requests: {total}")
        print(f"Total Time: {elapsed:.2f}s")
        print(f"Throughput: {total/elapsed:.2f} req/s")
        print()

        print(f"{Colors.BOLD}Provider Breakdown:{Colors.END}")
        for name, stats in sorted(provider_stats.items(), key=lambda x: x[1]["success"], reverse=True):
            total_req = stats["success"] + stats["failure"]
            latencies = stats["latencies"]
            avg_latency = sum(latencies) / len(latencies) if latencies else 0
            min_latency = min(latencies) if latencies else 0
            max_latency = max(latencies) if latencies else 0

            status_color = Colors.GREEN if stats["failure"] == 0 else Colors.YELLOW if stats["success"] > 0 else Colors.RED
            symbol = "✓" if stats["failure"] == 0 else "⚠" if stats["success"] > 0 else "✗"

            print(f"  {status_color}{symbol}{Colors.END} {name:<15} "
                  f"Success: {stats['success']}/{total_req} "
                  f"Avg: {avg_latency:.0f}ms "
                  f"(min: {min_latency:.0f}ms, max: {max_latency:.0f}ms)")

        print()
        if skipped:
            print(f"{Colors.YELLOW}Skipped Providers (not configured):{Colors.END}")
            for r in skipped:
                print(f"  {Colors.YELLOW}●{Colors.END} {r['provider']:<15} {r.get('reason', '')}")

        failure_providers = [r for r in self.results if r["status"] == "failure"]
        if failure_providers:
            print(f"\n{Colors.RED}Failure Details:{Colors.END}")
            for r in failure_providers[:10]:
                print(f"  {Colors.RED}●{Colors.END} {r['provider']:<15} {r.get('error', 'Unknown error')[:60]}")
            if len(failure_providers) > 10:
                print(f"  ... and {len(failure_providers)-10} more")

        print(f"\n{Colors.CYAN}Circuit Breaker Analysis:{Colors.END}")
        for name, stats in provider_stats.items():
            if stats["failure"] >= 3:
                print(f"  {Colors.YELLOW}⚠{Colors.END} {name} would trigger circuit breaker ({stats['failure']} failures)")
            elif stats["failure"] > 0:
                print(f"  {Colors.GREEN}✓{Colors.END} {name} has {stats['failure']} failures (below threshold 3)")
            else:
                print(f"  {Colors.GREEN}✓{Colors.END} {name} is healthy (0 failures)")

        print(f"\n{Colors.BOLD}{'='*60}{Colors.END}")

        with open("stress_test_results.json", "w") as f:
            json.dump({
                "summary": {
                    "total": total,
                    "success": len(successes),
                    "failure": len(failures),
                    "skipped": len(skipped),
                    "elapsed_seconds": elapsed,
                    "throughput": total/elapsed
                },
                "provider_stats": provider_stats,
                "results": self.results
            }, f, indent=2)

        print(f"\n{Colors.CYAN}Detailed results saved to stress_test_results.json{Colors.END}")


async def main():
    parser = argparse.ArgumentParser(description="Provider stress test")
    parser.add_argument("--requests", type=int, default=10, help="Number of requests to send")
    parser.add_argument("--concurrency", type=int, default=3, help="Concurrent requests")
    args = parser.parse_args()

    tester = StressTester()
    try:
        await tester.run_stress_test(num_requests=args.requests, concurrent=args.concurrency)
    finally:
        await tester.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())