"""Apex Router — CLI + bench driver.

    python -m apex.cli route "<prompt>" --policy cost-first
    python -m apex.cli bench --mix receipt:80,clause:15,extraction:5
    python -m apex.cli telemetry [--reset]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys

from .router import ApexRouter
from .telemetry import aggregate
from .telemetry import reset as reset_telemetry

PROMPT_MIX = {
    "receipt": lambda i: (f"Receipt {i}: 2x Coffee $8.00, 1x Bagel $4.40. "
                          f"Total: $12.40. Extract line items and total."),
    "clause": lambda i: (f'Clause {i}: "Lessee shall not assign this Agreement '
                         f'without Lessor\'s prior written consent, which shall '
                         f'not be unreasonably withheld." Summarize obligations.'),
    "extraction": lambda i: (f"Extract dates and parties: agreement dated "
                             f"2025-01-15 between Acme Corp and Zeta LLC "
                             f"(ref {i})."),
    "summarization": lambda i: (f"Summarize in one sentence: the party of the "
                                f"first part agrees to indemnify the party of "
                                f"the second part for all claims arising from "
                                f"negligence (case {i})."),
}


# ─── Commands ─────────────────────────────────────────────────────────────
async def _cmd_route(args: argparse.Namespace) -> int:
    router = await ApexRouter.create(policy=args.policy)
    try:
        result = await router.route(
            args.prompt, taskType=args.task_type, maxTokens=args.max_tokens)
        print(json.dumps(result.to_dict(), indent=2))
        return 0
    finally:
        await router.close()


async def _cmd_bench(args: argparse.Namespace) -> int:
    if args.reset:
        reset_telemetry()

    router = await ApexRouter.create(policy=args.policy)
    try:
        mix = _parse_mix(args.mix)
        queue: list[dict] = []
        for task, count in mix.items():
            for i in range(count):
                queue.append({"taskType": task, "i": i})
        random.shuffle(queue)

        total = len(queue)
        for idx, item in enumerate(queue, 1):
            prompt = PROMPT_MIX[item["taskType"]](item["i"])
            result = await router.route(prompt, taskType=item["taskType"])
            bar = _progress_bar(idx, total)
            sys.stdout.write(
                f"\r{bar} {idx}/{total} → {result.tier:<9} "
                f"(conf {result.confidence})  ")
            sys.stdout.flush()
        sys.stdout.write("\n\n")

        agg = await aggregate()
        print(json.dumps(agg.to_dict(), indent=2))
        return 0
    finally:
        await router.close()


async def _cmd_telemetry(args: argparse.Namespace) -> int:
    if args.reset:
        reset_telemetry()
        print(json.dumps({"reset": True}))
        return 0
    agg = await aggregate(recent_n=args.recent)
    print(json.dumps(agg.to_dict(), indent=2))
    return 0


# ─── Helpers ──────────────────────────────────────────────────────────────
def _parse_mix(mix_arg: str | None) -> dict[str, int]:
    if not mix_arg:
        return {"receipt": 80, "clause": 15, "extraction": 5}
    out: dict[str, int] = {}
    for piece in mix_arg.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if ":" not in piece:
            raise SystemExit(f"bad mix component: {piece!r}")
        task, count = piece.split(":", 1)
        if task not in PROMPT_MIX:
            raise SystemExit(
                f"unknown task type: {task!r} "
                f"(known: {', '.join(PROMPT_MIX)})")
        out[task] = int(count)
    return out


def _progress_bar(current: int, total: int, width: int = 30) -> str:
    filled = int(width * current / total) if total else width
    return "[" + "█" * filled + "░" * (width - filled) + "]"


# ─── Argparse ─────────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apex", description="Apex Router — hybrid inference router")
    parser.add_argument(
        "--policy", default=None,
        help="routing policy (latency-first|cost-first|quality-first)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_route = sub.add_parser("route", help="route a single prompt")
    p_route.add_argument("prompt", help="the prompt text")
    p_route.add_argument("--task-type", default=None)
    p_route.add_argument("--max-tokens", type=int, default=512)
    p_route.set_defaults(func=_cmd_route)

    p_bench = sub.add_parser("bench", help="run a mixed benchmark")
    p_bench.add_argument("--mix", default=None,
                         help="e.g. receipt:80,clause:15,extraction:5")
    p_bench.add_argument("--reset", action="store_true",
                         help="truncate telemetry before running")
    p_bench.set_defaults(func=_cmd_bench)

    p_telem = sub.add_parser("telemetry", help="show or reset telemetry")
    p_telem.add_argument("--reset", action="store_true")
    p_telem.add_argument("--recent", type=int, default=25)
    p_telem.set_defaults(func=_cmd_telemetry)

    return parser


def main() -> None:
    args = _build_parser().parse_args()
    try:
        rc = asyncio.run(args.func(args))
    except KeyboardInterrupt:
        rc = 130
    except Exception as exc:
        print(f"[apex] error: {exc}", file=sys.stderr)
        rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
