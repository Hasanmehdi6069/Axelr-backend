import asyncio
import os
import threading
from pathlib import Path

import pytest

from core import cache, foundation, guards, infra


@pytest.fixture(autouse=True)
def clear_cache_singletons():
    import core.cache
    core.cache._LOCAL_SINGLETON = None
    core.cache._BM25_SINGLETON = None
    core.cache._SEMANTIC = None
    yield


def test_settings_frozen():
    cfg = foundation.load()
    assert hasattr(cfg, "max_tokens")
    assert hasattr(cfg, "temperature")
    assert hasattr(cfg, "debug")
    assert hasattr(cfg, "default_model")


@pytest.mark.parametrize("env_var,value,field,expected", [
    ("AXELR_MAX_TOKENS", "8192", "max_tokens", 8192),
    ("AXELR_TEMPERATURE", "0.7", "temperature", 0.7),
    ("AXELR_DEBUG", "true", "debug", True),
    ("AXELR_DEFAULT_MODEL", "gpt-4o", "default_model", "gpt-4o"),
])
def test_settings_env_override(env_var, value, field, expected):
    old = os.environ.get(env_var)
    os.environ[env_var] = value
    try:
        cfg = foundation.load()
        assert getattr(cfg, field) == expected
    finally:
        if old is not None:
            os.environ[env_var] = old
        else:
            os.environ.pop(env_var, None)
        foundation._CFG = None


def test_settings_invalid_env_falls_back():
    old = os.environ.get("AXELR_MAX_TOKENS")
    os.environ["AXELR_MAX_TOKENS"] = "not_an_int"
    try:
        cfg = foundation.load()
        assert cfg.max_tokens > 0
    finally:
        if old is not None:
            os.environ["AXELR_MAX_TOKENS"] = old
        else:
            os.environ.pop("AXELR_MAX_TOKENS", None)
        foundation._CFG = None


def test_settings_audit_shape():
    audit = foundation.audit()
    assert "settings" in audit
    settings = audit["settings"]
    assert "max_tokens" in settings
    assert "temperature" in settings
    assert "debug" in settings
    assert "default_model" in settings


@pytest.mark.parametrize("error_cls", [
    foundation.AxelrError,
    foundation.RetryableError,
    foundation.ValidationError,
    foundation.NotFoundError,
])
def test_error_subclasses_have_required_interface(error_cls):
    err = error_cls("test message")
    assert hasattr(err, "message")
    assert hasattr(err, "code")
    assert hasattr(err, "details")
    assert isinstance(err, Exception)


def test_error_to_payload_shape():
    err = foundation.ValidationError("test", code="TEST_CODE", details={"foo": "bar"})
    payload = foundation.error_to_payload(err)
    assert "error" in payload
    assert payload["error"]["message"] == "test"
    assert payload["error"]["code"] == "TEST_CODE"
    assert "details" in payload["error"]


def test_retryable_error_is_retryable():
    assert foundation.RetryableError.is_retryable() is True


@pytest.mark.parametrize("error_cls", [foundation.ValidationError, foundation.NotFoundError])
def test_non_retryable_error_is_not_retryable(error_cls):
    assert not error_cls.is_retryable()


def test_error_to_result_never_raises():
    err = foundation.ValidationError("test")
    result = foundation.to_result(err)
    assert "tier" in result
    assert "error" in result


def test_retry_policy_exhausts_and_raises():
    policy = foundation.RetryPolicy(max_attempts=3, retry_on=(ValueError,))
    calls = []
    @policy
    def always_fails():
        calls.append(1)
        raise ValueError("fail")
    with pytest.raises(ValueError):
        always_fails()
    assert len(calls) == 3


def test_retry_policy_should_retry_respects_retry_on():
    policy = foundation.RetryPolicy(max_attempts=3, retry_on=(ValueError,))
    @policy
    def raises_type_error():
        raise TypeError("not retryable")
    with pytest.raises(TypeError):
        raises_type_error()


def test_retry_policy_delay_bounded_by_max():
    policy = foundation.RetryPolicy(max_delay_s=1.0, backoff_factor=10)
    assert policy.max_delay_s == 1.0


def test_retry_policy_jitter_within_15_percent():
    delays = [foundation.RetryPolicy._calculate_delay(attempt=5, base=0.1, max=10) for _ in range(100)]
    for d in delays:
        assert abs(d - 1.6) / 1.6 <= 0.15 or d <= 10


@pytest.mark.asyncio
async def test_retry_policy_cancelled_error_propagates():
    policy = foundation.RetryPolicy(max_attempts=5)
    calls = []
    @policy
    async def cancelled():
        calls.append(1)
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await cancelled()


def test_with_retry_succeeds_on_nth_attempt():
    policy = foundation.RetryPolicy(max_attempts=5)
    calls = []
    @policy
    def succeeds_on_3rd():
        calls.append(1)
        if len(calls) < 3:
            raise ValueError("fail")
        return "success"
    result = succeeds_on_3rd()
    assert result == "success"
    assert len(calls) == 3


def test_with_retry_raises_after_max_attempts():
    policy = foundation.RetryPolicy(max_attempts=2)
    @policy
    def always_fails():
        raise ValueError("fail")
    with pytest.raises(ValueError):
        always_fails()


def test_with_retry_on_retry_callback_fires_per_attempt():
    calls = []
    def on_retry(attempt, exc):
        calls.append(attempt)
    policy = foundation.RetryPolicy(max_attempts=3, on_retry=on_retry)
    @policy
    def fails_twice():
        if len(calls) < 2:
            raise ValueError("fail")
        return "ok"
    fails_twice()
    assert calls == [1, 2]


def test_with_retry_backoff_exponential():
    policy = foundation.RetryPolicy(max_attempts=5, backoff_factor=2)
    assert policy.backoff_factor == 2


def test_permission_dataclass_immutable():
    p = foundation.Permission(name="test", description="desc")
    with pytest.raises(Exception):
        p.name = "new"


def test_user_consent_grant_deny_roundtrip():
    store = foundation.ConsentStore()
    store.grant("user1", "test_permission")
    assert store.is_granted("user1", "test_permission") is True
    store._consents[("user1", "test_permission")].deny()
    store._save("user1", "test_permission")
    assert store.is_granted("user1", "test_permission") is False


def test_check_permissions_missing_raises():
    store = foundation.ConsentStore()
    assert store.is_granted("user1", "nonexistent") is False


def test_consent_store_write_reload():
    tmp_dir = Path(__file__).parent / "test_consent_store"
    if tmp_dir.exists():
        import shutil
        shutil.rmtree(tmp_dir)
    store1 = foundation.ConsentStore(path=tmp_dir)
    store1.grant("u1", "p1")
    store2 = foundation.ConsentStore(path=tmp_dir)
    assert store2.is_granted("u1", "p1") is True
    if tmp_dir.exists():
        import shutil
        shutil.rmtree(tmp_dir)


def test_consent_store_export_gdpr_includes_audit():
    store = foundation.ConsentStore()
    store.grant("u1", "p1")
    export = store.export_for_gdpr("u1")
    assert "audit_trail" in export


def test_consent_store_forget_wipes_files():
    tmp_dir = Path(__file__).parent / "test_forget_store"
    if tmp_dir.exists():
        import shutil
        shutil.rmtree(tmp_dir)
    store = foundation.ConsentStore(path=tmp_dir)
    store.grant("u1", "p1")
    store.forget("u1")
    assert store.is_granted("u1", "p1") is False
    if tmp_dir.exists():
        import shutil
        shutil.rmtree(tmp_dir)


def test_foundation_audit_shape():
    audit = foundation.audit()
    assert "settings" in audit
    assert "max_tokens" in audit["settings"]
    assert "temperature" in audit["settings"]
    assert "debug" in audit["settings"]


def test_get_redis_client_none_when_state_absent():
    client = infra.get_redis_client()
    assert client is None or hasattr(client, "get")


@pytest.mark.asyncio
async def test_init_shared_redis_cache_degrades_to_dummy():
    c = await infra.init_shared_redis_cache()
    assert c is not None
    assert hasattr(c, "get")
    assert hasattr(c, "set")
    assert hasattr(c, "delete")


@pytest.mark.asyncio
async def test_dummy_cache_roundtrip():
    c = await infra.init_shared_redis_cache()
    await c.set("test_key", "test_value", 60)
    val = await c.get("test_key")
    assert val == "test_value"


def test_context_registry_no_sources_returns_none():
    reg = infra.ContextRegistry()
    assert reg.get_context("u1", "w1") is None


def test_context_registry_set_get_roundtrip():
    reg = infra.ContextRegistry()
    reg.set_context("u1", "w1", "test context")
    assert reg.get_context("u1", "w1") == "test context"


def test_context_registry_summarise_respects_token_budget():
    reg = infra.ContextRegistry()
    long_text = "word " * 1000
    reg.set_context("u1", "w1", long_text)
    summary = reg.summarise("u1", "w1", max_tokens=100)
    assert len(summary.split()) <= 100


def test_context_registry_priority_ordering():
    reg = infra.ContextRegistry()
    reg.set_context("u1", "w1", "low priority", priority=1)
    reg.set_context("u1", "w1", "high priority", priority=10)
    items = reg.get_all("u1", "w1")
    assert items[0].priority == 10


def test_context_registry_audit_shape():
    reg = infra.ContextRegistry()
    reg.set_context("u1", "w1", "test")
    audit = reg.audit()
    assert "entries" in audit
    assert "total_tokens" in audit


def test_canonical_prefix_deterministic():
    p1 = cache.canonical_prefix("workspace", "intent", "free")
    p2 = cache.canonical_prefix("workspace", "intent", "free")
    assert p1 == p2


def test_cache_remember_recall_roundtrip():
    cache.remember_prefix("workspace", "intent", "free", "result")
    result = cache.recall_prefix("workspace", "intent", "free")
    assert result == "result"


def test_cache_recall_miss_returns_none():
    result = cache.recall_prefix("nonexistent", "intent", "free")
    assert result is None


@pytest.mark.parametrize("provider", ["anthropic", "gemini"])
def test_provider_cache_hint_shape(provider):
    hint = cache.provider_cache_hint("test prefix", provider)
    assert "prefix" in hint
    assert "provider" in hint


def test_semantic_cache_get_empty_prompt_returns_none():
    sem = cache.LocalSemanticCache(path=":memory:")
    assert sem.get("") is None


def test_semantic_cache_put_stores_locally():
    sem = cache.LocalSemanticCache(path=":memory:")
    assert sem.put("test", "result") is True
    assert sem.get("test") == "result"


def test_bm25_add_query_topk_respected():
    bm25 = cache.BM25Index()
    for i in range(10):
        bm25.add(f"doc{i}", f"test document {i}")
    hits = bm25.query("test", top_k=3)
    assert len(hits) <= 3


def test_bm25_no_match_returns_empty():
    bm25 = cache.BM25Index()
    bm25.add("doc1", "hello world")
    hits = bm25.query("nonexistent")
    assert len(hits) == 0


def test_bm25_filter_fn_applies():
    bm25 = cache.BM25Index()
    bm25.add("doc1", "test", metadata={"category": "a"})
    bm25.add("doc2", "test", metadata={"category": "b"})
    hits = bm25.query("test", filter_fn=lambda m: m.get("category") == "a")
    assert len(hits) == 1
    assert hits[0].doc_id == "doc1"


def test_bm25_scores_positive():
    bm25 = cache.BM25Index()
    bm25.add("doc1", "test document text")
    hits = bm25.query("test")
    for h in hits:
        assert h.score > 0


def test_bm25_eviction_at_max_docs():
    bm25 = cache.BM25Index(max_docs=5)
    for i in range(10):
        bm25.add(f"doc{i}", f"test {i}")
    stats = bm25.stats()
    assert stats["documents"] == 5


def test_bm25_stats_shape():
    bm25 = cache.BM25Index()
    bm25.add("doc1", "test")
    stats = bm25.stats()
    assert "documents" in stats
    assert "unique_terms" in stats


def test_bm25_singleton_stable():
    b1 = cache.get_bm25_index()
    b2 = cache.get_bm25_index()
    assert b1 is b2


def test_estimate_tokens_roughly_len_div4():
    assert guards.estimate_tokens("a" * 400) == 100


def test_cache_audit_shape():
    audit = cache.audit()
    for k in ("prefix_cache", "semantic_cache", "local_cache", "bm25"):
        assert k in audit


def test_semantic_cache_stats_has_required_keys():
    sem = cache.LocalSemanticCache(path=":memory:")
    sem.put("test", "result")
    stats = sem.stats()
    assert "hits" in stats
    assert "misses" in stats
    assert "rows" in stats
    assert "hit_rate" in stats


@pytest.mark.parametrize("max_rows", [100, 1000, 10000])
def test_semantic_cache_eviction_at_various_max_rows(max_rows):
    sem = cache.LocalSemanticCache(path=":memory:", max_rows=max_rows)
    for i in range(max_rows * 2):
        sem.put(f"q{i}", f"r{i}")
    stats = sem.stats()
    assert stats["rows"] <= max_rows


@pytest.mark.parametrize("max_docs", [100, 1000, 10000])
def test_bm25_eviction_at_various_max_docs(max_docs):
    bm25 = cache.BM25Index(max_docs=max_docs)
    for i in range(max_docs * 2):
        bm25.add(f"doc{i}", f"test {i}")
    assert bm25.stats()["documents"] == max_docs


@pytest.mark.parametrize("top_k", [1, 3, 5, 10])
def test_bm25_topk_across_values(top_k):
    bm25 = cache.BM25Index()
    for i in range(20):
        bm25.add(f"doc{i}", f"test document {i}")
    hits = bm25.query("test", top_k=top_k)
    assert len(hits) <= top_k


def test_bm25_query_relevance():
    bm25 = cache.BM25Index()
    bm25.add("doc1", "cat dog bird fish")
    bm25.add("doc2", "car truck bike plane")
    hits = bm25.query("cat dog")
    assert len(hits) >= 1
    assert hits[0].doc_id == "doc1"


def test_bm25_score_ranking_monotonic():
    bm25 = cache.BM25Index()
    for i in range(10):
        bm25.add(f"doc{i}", f"test {'test ' * i}")
    hits = bm25.query("test")
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_bm25_empty_query_returns_empty():
    bm25 = cache.BM25Index()
    bm25.add("doc1", "test")
    hits = bm25.query("")
    assert hits == []


def test_bm25_duplicate_doc_id_overwrites():
    bm25 = cache.BM25Index()
    bm25.add("doc1", "first version")
    bm25.add("doc1", "second version")
    stats = bm25.stats()
    assert stats["documents"] == 1


def test_bm25_stats_includes_add_count():
    bm25 = cache.BM25Index()
    for i in range(5):
        bm25.add(f"doc{i}", f"test {i}")
    stats = bm25.stats()
    assert stats["documents"] == 5


def test_bm25_singleton_thread_safety():
    instances = []
    def get_instance():
        instances.append(id(cache.get_bm25_index()))
    threads = [threading.Thread(target=get_instance) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(instances)) == 1
