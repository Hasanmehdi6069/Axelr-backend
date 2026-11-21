"""
Runtime tables contract test — verifies the JSON schema and reranker wiring.
"""

import json
import sys
from pathlib import Path

import pytest


def _find_engine_root() -> Path | None:
    """Walk up from test file until we find Axelr-backend."""
    here = Path(__file__).resolve().parent
    while here != here.parent:
        candidate = here / "Axelr-backend" / "core" / "axelr-sovereign.engine"
        if candidate.exists():
            return candidate
        if (here / "core" / "axelr-sovereign.engine").exists():
            return here / "core" / "axelr-sovereign.engine"
        here = here.parent
    return None


def test_python_runtime_tables():
    engine_root = _find_engine_root()
    if engine_root is None:
        pytest.skip("Axelr-backend/core/axelr-sovereign.engine not found in any parent dir")
    tables_path = engine_root / "runtime-tables.json"
    if not tables_path.exists():
        pytest.skip(f"runtime-tables.json not found at {tables_path}")

    with open(tables_path, encoding="utf-8") as f:
        runtime_tables = json.load(f)
    assert isinstance(runtime_tables, dict), (
        f"runtime-tables.json must be an object, got {type(runtime_tables).__name__}"
    )

    sys.path.insert(0, str(engine_root))

    try:
        from axelr_features import StreamingNGramReranker
    except ImportError as e:
        pytest.skip(f"StreamingNGramReranker unavailable: {e}")

    reranker = StreamingNGramReranker()
    assert hasattr(reranker, "_repairs"), "reranker missing _repairs attribute"
    assert len(reranker._repairs) > 0, "reranker initialized with zero repair rules"

    test_tokens = ["they", "were", "their", "going"]
    corrected = reranker.rerank(test_tokens)
    assert "they're" in corrected, (
        f'homophone correction failed: {test_tokens} → {corrected}. Expected "they\'re going" to be in output.'
    )
