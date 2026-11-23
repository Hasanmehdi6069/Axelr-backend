import asyncio
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core import cache


@pytest.fixture(autouse=True)
def clear_cache_singletons():
    cache._PREFIX_LOCAL_CACHE.clear()
    cache._LOCAL_SINGLETON = None
    cache._BM25_SINGLETON = None
    cache._SEMANTIC = None
    yield


class TestPrefixCacheHelpers:
    def test_prefix_hash_deterministic(self):
        h1 = cache._prefix_hash("part1", "part2", "part3")
        h2 = cache._prefix_hash("part1", "part2", "part3")
        assert h1 == h2
        assert len(h1) == 16

    def test_prefix_hash_different_inputs_different_hashes(self):
        h1 = cache._prefix_hash("a", "b")
        h2 = cache._prefix_hash("c", "d")
        assert h1 != h2

    def test_canonical_prefix_returns_tuple(self):
        pid, extra = cache.canonical_prefix("workspace", "intent", "tier")
        assert isinstance(pid, str)
        assert isinstance(extra, str)

    def test_canonical_prefix_deterministic(self):
        p1 = cache.canonical_prefix("ws", "intent", "free")
        p2 = cache.canonical_prefix("ws", "intent", "free")
        assert p1[0] == p2[0]

    def test_provider_cache_hint_anthropic_format(self):
        hint = cache.provider_cache_hint("prefix text", "anthropic")
        assert "system" in hint
        assert "cache_control" in hint["system"][0]

    def test_provider_cache_hint_gemini_format(self):
        hint = cache.provider_cache_hint("prefix text", "gemini")
        assert "_axelr_prefix_id" in hint

    def test_provider_cache_hint_unknown_provider(self):
        hint = cache.provider_cache_hint("prefix", "unknown")
        assert "prefix" in hint
        assert "provider" in hint


class TestPrefixCache:
    def test_remember_recall_roundtrip(self):
        cache.remember_prefix("ws", "intent", "free", "result value")
        result = cache.recall_prefix("ws", "intent", "free")
        assert result == "result value"

    def test_recall_miss_returns_none(self):
        result = cache.recall_prefix("nonexistent", "intent", "free")
        assert result is None

    def test_remember_overwrites_existing(self):
        cache.remember_prefix("ws", "intent", "free", "first")
        cache.remember_prefix("ws", "intent", "free", "second")
        result = cache.recall_prefix("ws", "intent", "free")
        assert result == "second"

    def test_different_workspaces_isolated(self):
        cache.remember_prefix("ws1", "intent", "free", "val1")
        cache.remember_prefix("ws2", "intent", "free", "val2")
        assert cache.recall_prefix("ws1", "intent", "free") == "val1"
        assert cache.recall_prefix("ws2", "intent", "free") == "val2"

    def test_different_intents_isolated(self):
        cache.remember_prefix("ws", "intent1", "free", "val1")
        cache.remember_prefix("ws", "intent2", "free", "val2")
        assert cache.recall_prefix("ws", "intent1", "free") == "val1"
        assert cache.recall_prefix("ws", "intent2", "free") == "val2"

    def test_different_tiers_isolated(self):
        cache.remember_prefix("ws", "intent", "free", "free_val")
        cache.remember_prefix("ws", "intent", "pro", "pro_val")
        assert cache.recall_prefix("ws", "intent", "free") == "free_val"
        assert cache.recall_prefix("ws", "intent", "pro") == "pro_val"

    def test_prefix_hit_miss_metrics_no_errors(self):
        cache.prefix_hit("ws", "free")
        cache.prefix_miss("ws", "free")
        assert True


class TestLocalSemanticCache:
    def test_semantic_cache_initialization(self):
        sem = cache.LocalSemanticCache(path=":memory:")
        assert sem is not None

    def test_semantic_cache_put_get_roundtrip(self):
        sem = cache.LocalSemanticCache(path=":memory:")
        assert sem.put("query", "result") is True
        assert sem.get("query") == "result"

    def test_semantic_cache_get_nonexistent_returns_none(self):
        sem = cache.LocalSemanticCache(path=":memory:")
        assert sem.get("nonexistent") is None

    def test_semantic_cache_empty_query_returns_none(self):
        sem = cache.LocalSemanticCache(path=":memory:")
        assert sem.get("") is None

    def test_semantic_cache_put_empty_query_returns_false(self):
        sem = cache.LocalSemanticCache(path=":memory:")
        assert sem.put("", "result") is False

    def test_semantic_cache_stats_shape(self):
        sem = cache.LocalSemanticCache(path=":memory:")
        sem.put("q1", "r1")
        stats = sem.stats()
        assert "hits" in stats
        assert "misses" in stats
        assert "rows" in stats
        assert "hit_rate" in stats

    def test_semantic_cache_hit_increments(self):
        sem = cache.LocalSemanticCache(path=":memory:")
        sem.put("query", "result")
        sem.get("query")
        stats = sem.stats()
        assert stats["hits"] == 1

    def test_semantic_cache_miss_increments(self):
        sem = cache.LocalSemanticCache(path=":memory:")
        sem.get("nonexistent")
        stats = sem.stats()
        assert stats["misses"] == 1

    def test_semantic_cache_hit_rate_calculation(self):
        sem = cache.LocalSemanticCache(path=":memory:")
        sem.put("q", "r")
        sem.get("q")
        sem.get("nonexistent")
        stats = sem.stats()
        assert stats["hit_rate"] == 0.5

    def test_semantic_cache_eviction_at_max_rows(self):
        sem = cache.LocalSemanticCache(path=":memory:", max_rows=5)
        for i in range(10):
            sem.put(f"q{i}", f"r{i}")
        stats = sem.stats()
        assert stats["rows"] <= 5

    def test_semantic_cache_delete(self):
        sem = cache.LocalSemanticCache(path=":memory:")
        sem.put("q", "r")
        sem.delete("q")
        assert sem.get("q") is None

    def test_semantic_cache_clear(self):
        sem = cache.LocalSemanticCache(path=":memory:")
        sem.put("q1", "r1")
        sem.put("q2", "r2")
        sem.clear()
        stats = sem.stats()
        assert stats["rows"] == 0

    def test_semantic_cache_export_import(self):
        sem1 = cache.LocalSemanticCache(path=":memory:")
        sem1.put("q1", "r1")
        data = sem1.export()
        sem2 = cache.LocalSemanticCache(path=":memory:")
        sem2.import_data(data)
        assert sem2.get("q1") == "r1"


class TestBM25Index:
    def test_bm25_initialization(self):
        bm25 = cache.BM25Index()
        assert bm25 is not None

    def test_bm25_add_document(self):
        bm25 = cache.BM25Index()
        bm25.add("doc1", "test document content")
        stats = bm25.stats()
        assert stats["documents"] == 1

    def test_bm25_query_returns_results(self):
        bm25 = cache.BM25Index()
        bm25.add("doc1", "cat dog bird")
        bm25.add("doc2", "car truck bike")
        hits = bm25.query("cat dog")
        assert len(hits) >= 1

    def test_bm25_query_topk_respected(self):
        bm25 = cache.BM25Index()
        for i in range(20):
            bm25.add(f"doc{i}", f"test document {i}")
        hits = bm25.query("test", top_k=5)
        assert len(hits) <= 5

    def test_bm25_query_no_match_returns_empty(self):
        bm25 = cache.BM25Index()
        bm25.add("doc1", "hello world")
        hits = bm25.query("nonexistent term")
        assert len(hits) == 0

    def test_bm25_empty_query_returns_empty(self):
        bm25 = cache.BM25Index()
        bm25.add("doc1", "test")
        hits = bm25.query("")
        assert hits == []

    def test_bm25_scores_positive(self):
        bm25 = cache.BM25Index()
        bm25.add("doc1", "test document text")
        hits = bm25.query("test")
        for h in hits:
            assert h.score > 0

    def test_bm25_score_ranking_descending(self):
        bm25 = cache.BM25Index()
        for i in range(10):
            bm25.add(f"doc{i}", f"test {'test ' * i}")
        hits = bm25.query("test")
        scores = [h.score for h in hits]
        assert scores == sorted(scores, reverse=True)

    def test_bm25_filter_fn_applies(self):
        bm25 = cache.BM25Index()
        bm25.add("doc1", "test", metadata={"cat": "a"})
        bm25.add("doc2", "test", metadata={"cat": "b"})
        hits = bm25.query("test", filter_fn=lambda m: m.get("cat") == "a")
        assert len(hits) == 1
        assert hits[0].doc_id == "doc1"

    def test_bm25_eviction_at_max_docs(self):
        bm25 = cache.BM25Index(max_docs=5)
        for i in range(10):
            bm25.add(f"doc{i}", f"test {i}")
        stats = bm25.stats()
        assert stats["documents"] == 5

    def test_bm25_duplicate_doc_id_overwrites(self):
        bm25 = cache.BM25Index()
        bm25.add("doc1", "first version")
        bm25.add("doc1", "second version")
        stats = bm25.stats()
        assert stats["documents"] == 1

    def test_bm25_stats_shape(self):
        bm25 = cache.BM25Index()
        bm25.add("doc1", "test")
        stats = bm25.stats()
        assert "documents" in stats
        assert "unique_terms" in stats
        assert "total_terms" in stats

    def test_bm25_get_singleton_stable(self):
        b1 = cache.get_bm25_index()
        b2 = cache.get_bm25_index()
        assert b1 is b2

    def test_bm25_concurrent_access(self):
        bm25 = cache.BM25Index()
        import threading
        def add_many(n):
            for i in range(n):
                bm25.add(f"doc_{threading.get_ident()}_{i}", f"test {i}")
        threads = [threading.Thread(target=add_many, args=(10,)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert True


class TestCacheAudit:
    def test_cache_audit_shape(self):
        audit = cache.audit()
        assert "prefix_cache" in audit
        assert "semantic_cache" in audit
        assert "bm25" in audit
        assert "local_cache" in audit


class TestCacheModuleConstants:
    def test_prefix_ttl_constant(self):
        assert hasattr(cache, "_PREFIX_TTL_S")
        assert cache._PREFIX_TTL_S > 0

    def test_cache_metrics_no_errors(self):
        assert hasattr(cache, "prefix_hit")
        assert hasattr(cache, "prefix_miss")

    def test_cache_module_exports(self):
        required = [
            "remember_prefix", "recall_prefix", "prefix_hit", "prefix_miss",
            "canonical_prefix", "provider_cache_hint", "LocalSemanticCache",
            "BM25Index", "get_bm25_index", "audit"
        ]
        for sym in required:
            assert hasattr(cache, sym)

    def test_prefix_cache_size_tracking(self):
        cache.remember_prefix("ws", "intent1", "free", "val1")
        cache.remember_prefix("ws", "intent2", "free", "val2")
        audit = cache.audit()
        assert audit["prefix_cache"]["size"] >= 2

    def test_semantic_cache_singleton_stable(self):
        s1 = cache.get_semantic_cache()
        s2 = cache.get_semantic_cache()
        assert s1 is s2

    def test_local_cache_singleton_stable(self):
        l1 = cache.get_local_cache()
        l2 = cache.get_local_cache()
        assert l1 is l2

    def test_local_cache_put_get_roundtrip(self):
        local = cache.get_local_cache()
        local.put("key1", "value1")
        assert local.get("key1") == "value1"

    def test_local_cache_get_nonexistent_returns_none(self):
        local = cache.get_local_cache()
        assert local.get("nonexistent") is None

    def test_local_cache_delete(self):
        local = cache.get_local_cache()
        local.put("key", "val")
        local.delete("key")
        assert local.get("key") is None

    def test_local_cache_clear(self):
        local = cache.get_local_cache()
        local.put("k1", "v1")
        local.put("k2", "v2")
        local.clear()
        assert local.get("k1") is None
        assert local.get("k2") is None

    def test_local_cache_stats_shape(self):
        local = cache.get_local_cache()
        local.put("key", "val")
        stats = local.stats()
        assert "size" in stats
        assert "hits" in stats
        assert "misses" in stats

    def test_local_cache_hit_increments(self):
        local = cache.get_local_cache()
        local.put("key", "val")
        local.get("key")
        stats = local.stats()
        assert stats["hits"] >= 1

    def test_local_cache_miss_increments(self):
        local = cache.get_local_cache()
        local.get("nonexistent")
        stats = local.stats()
        assert stats["misses"] >= 1

    def test_local_cache_ttl_expires(self):
        local = cache.get_local_cache()
        local.put("key", "val", ttl=0.1)
        assert local.get("key") == "val"
        import time
        time.sleep(0.15)
        assert local.get("key") is None

    def test_local_cache_eviction_at_max_size(self):
        local = cache.get_local_cache(max_size=5)
        for i in range(10):
            local.put(f"key{i}", f"val{i}")
        stats = local.stats()
        assert stats["size"] <= 5

    def test_local_cache_concurrent_access(self):
        local = cache.get_local_cache()
        import threading
        def put_many(n):
            for i in range(n):
                local.put(f"k_{threading.get_ident()}_{i}", f"v{i}")
        threads = [threading.Thread(target=put_many, args=(10,)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert True

    def test_bm25_result_has_required_fields(self):
        bm25 = cache.BM25Index()
        bm25.add("doc1", "test content")
        hits = bm25.query("test")
        if hits:
            assert hasattr(hits[0], "doc_id")
            assert hasattr(hits[0], "score")
            assert hasattr(hits[0], "metadata")

    def test_semantic_cache_query_returns_results(self):
        sem = cache.LocalSemanticCache(path=":memory:")
        sem.put("query1", "result1")
        sem.put("query2", "result2")
        results = sem.query("query1")
        assert isinstance(results, list)

    def test_semantic_cache_query_topk_respected(self):
        sem = cache.LocalSemanticCache(path=":memory:")
        for i in range(10):
            sem.put(f"q{i}", f"r{i}")
        results = sem.query("q", top_k=3)
        assert len(results) <= 3

    def test_semantic_cache_query_min_score_filter(self):
        sem = cache.LocalSemanticCache(path=":memory:")
        sem.put("q1", "r1")
        sem.put("q2", "r2")
        results = sem.query("q", min_score=0.99)
        assert isinstance(results, list)

    def test_prefix_cache_ttl_expires(self):
        cache.remember_prefix("ws", "intent", "free", "val", ttl=0.1)
        assert cache.recall_prefix("ws", "intent", "free") == "val"
        import time
        time.sleep(0.15)
        assert cache.recall_prefix("ws", "intent", "free") is None

    def test_prefix_cache_delete(self):
        cache.remember_prefix("ws", "intent", "free", "val")
        cache.delete_prefix("ws", "intent", "free")
        assert cache.recall_prefix("ws", "intent", "free") is None

    def test_prefix_cache_clear_all(self):
        cache.remember_prefix("ws1", "intent", "free", "val1")
        cache.remember_prefix("ws2", "intent", "free", "val2")
        cache.clear_prefixes()
        assert cache.recall_prefix("ws1", "intent", "free") is None
        assert cache.recall_prefix("ws2", "intent", "free") is None

    def test_cache_audit_includes_timestamps(self):
        audit = cache.audit()
        assert "timestamp" in audit

    def test_cache_audit_includes_version(self):
        audit = cache.audit()
        assert "version" in audit

    def test_cache_health_check_returns_dict(self):
        health = cache.health_check()
        assert isinstance(health, dict)
        assert "status" in health

    def test_cache_health_check_status_ok(self):
        health = cache.health_check()
        assert health["status"] == "ok"

    def test_cache_metrics_increment_no_errors(self):
        cache.cache_hit("semantic")
        cache.cache_miss("semantic")
        cache.cache_hit("prefix")
        cache.cache_miss("prefix")
        cache.cache_hit("local")
        cache.cache_miss("local")
        assert True

    def test_cache_module_all_exports_defined(self):
        assert hasattr(cache, "__all__")
        assert isinstance(cache.__all__, list)