
import asyncio

from app import (
    PROVIDER_CHAIN,
    PROVIDER_FUNC_MAP,
    PROVIDER_KEY_CHECK,
    PROVIDER_MODELS,
    TIER_CONFIG,
    detect_workspace,
    estimate_tokens,
    get_dynamically_ranked_providers,
    get_provider_order,
    strip_fluff,
)


async def main():
    # 1. Provider coverage
    for name, _ in PROVIDER_CHAIN:
        assert name in PROVIDER_FUNC_MAP, f"{name} missing from FUNC_MAP"
        assert name in PROVIDER_KEY_CHECK, f"{name} missing from KEY_CHECK"
        assert name in PROVIDER_MODELS,    f"{name} missing from MODELS"
    print("PROVIDER MAP CONSISTENT")

    # 2. Ordering is deterministic and includes 'local' last
    for ws in ("data", "design", "core", "prompt", "touch_fix"):
        order = get_provider_order(ws)
        assert order[-1] == "local", f"{ws}: local not last"
        assert len(order) == len(set(order)), f"{ws}: duplicate providers"

    # 3. Dynamic ranking never includes a provider with no key
    for ws in ("data", "design", "core"):
        for p in get_dynamically_ranked_providers(ws):
            if p == "local":
                continue
            assert PROVIDER_KEY_CHECK.get(p), f"{ws}: {p} has no key but was ranked"

    # 4. Tier gating
    for tier, cfg in TIER_CONFIG.items():
        assert "providers" in cfg
        assert isinstance(cfg["providers"], (list, str))

    # 5. Workspace detection
    assert detect_workspace("extract columns from this CSV", []) == "data"
    assert detect_workspace("design a navbar", []) == "design"
    assert detect_workspace("hello", []) == "core"

    # 6. Utilities
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 400) == 100
    assert "Sure!" not in strip_fluff("Sure! Here you go: hello")

    print("ALL ROUTER INVARIANTS PASS")

if __name__ == "__main__":
    asyncio.run(main())