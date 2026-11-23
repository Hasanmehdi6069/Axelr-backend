import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core import memory


@pytest.fixture
def vectorize_client():
    return memory._VectorizeClient(
        acct="test_acct",
        key="test_key",
        index="test_index",
        http=AsyncMock()
    )


class TestMemoryHelpers:
    def test_sha_deterministic(self):
        h1 = memory._sha("test text")
        h2 = memory._sha("test text")
        assert h1 == h2
        assert len(h1) == 64

    def test_sha_different_inputs_different_hashes(self):
        h1 = memory._sha("text1")
        h2 = memory._sha("text2")
        assert h1 != h2

    def test_make_id_format(self):
        id1 = memory._make_id("user1", "session1", "text1")
        assert len(id1) == 32
        assert isinstance(id1, str)

    def test_make_id_unique_per_call(self):
        id1 = memory._make_id("user1", "session1", "text1")
        time.sleep(0.001)
        id2 = memory._make_id("user1", "session1", "text1")
        assert id1 != id2

    def test_memory_module_constants(self):
        assert hasattr(memory, "_MAX_TEXT_CHARS")
        assert hasattr(memory, "_DEFAULT_TOP_K")
        assert hasattr(memory, "_DEFAULT_MIN_SCORE")
        assert hasattr(memory, "_DEFAULT_RECENT_LIMIT")
        assert memory._MAX_TEXT_CHARS > 0
        assert memory._DEFAULT_TOP_K > 0


class TestVectorizeClient:
    def test_vectorize_client_enabled_when_configured(self, vectorize_client):
        assert vectorize_client.enabled is True

    def test_vectorize_client_disabled_when_missing_acct(self):
        client = memory._VectorizeClient(
            acct="",
            key="test_key",
            index="test_index",
            http=AsyncMock()
        )
        assert client.enabled is False

    def test_vectorize_client_disabled_when_missing_key(self):
        client = memory._VectorizeClient(
            acct="test_acct",
            key="",
            index="test_index",
            http=AsyncMock()
        )
        assert client.enabled is False

    def test_vectorize_client_disabled_when_missing_index(self):
        client = memory._VectorizeClient(
            acct="test_acct",
            key="test_key",
            index="",
            http=AsyncMock()
        )
        assert client.enabled is False

    def test_vectorize_client_url_formatting(self, vectorize_client):
        url = vectorize_client._url("/test")
        assert "test_acct" in url
        assert "test_index" in url
        assert url.endswith("/test")

    def test_vectorize_client_headers(self, vectorize_client):
        headers = vectorize_client._headers()
        assert "Authorization" in headers
        assert "Content-Type" in headers
        assert "Bearer test_key" in headers["Authorization"]

    @pytest.mark.asyncio
    async def test_vectorize_upsert_success(self, vectorize_client):
        vectorize_client._http.post.return_value = MagicMock(status_code=200)
        vectors = [{"id": "v1", "values": [1.0, 2.0]}]
        result = await vectorize_client.upsert(vectors)
        assert result is True
        assert vectorize_client._http.post.called

    @pytest.mark.asyncio
    async def test_vectorize_upsert_disabled_returns_false(self):
        client = memory._VectorizeClient(
            acct="",
            key="test_key",
            index="test_index",
            http=AsyncMock()
        )
        result = await client.upsert([{"id": "v1"}])
        assert result is False

    @pytest.mark.asyncio
    async def test_vectorize_upsert_empty_vectors_returns_false(self, vectorize_client):
        result = await vectorize_client.upsert([])
        assert result is False

    @pytest.mark.asyncio
    async def test_vectorize_upsert_exception_returns_false(self, vectorize_client):
        vectorize_client._http.post.side_effect = Exception("network error")
        result = await vectorize_client.upsert([{"id": "v1"}])
        assert result is False

    @pytest.mark.asyncio
    async def test_vectorize_query_success(self, vectorize_client):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"result": [{"id": "v1", "score": 0.9}]}
        vectorize_client._http.post.return_value = mock_response
        result = await vectorize_client.query([1.0, 2.0], top_k=5)
        assert isinstance(result, list)
        assert vectorize_client._http.post.called

    @pytest.mark.asyncio
    async def test_vectorize_query_disabled_returns_empty(self):
        client = memory._VectorizeClient(
            acct="",
            key="test_key",
            index="test_index",
            http=AsyncMock()
        )
        result = await client.query([1.0, 2.0])
        assert result == []

    @pytest.mark.asyncio
    async def test_vectorize_query_exception_returns_empty(self, vectorize_client):
        vectorize_client._http.post.side_effect = Exception("network error")
        result = await vectorize_client.query([1.0, 2.0])
        assert result == []


class TestMemoryItem:
    def test_memory_item_creation(self):
        item = memory.MemoryItem(
            id="test1",
            text="test content",
            timestamp=time.time(),
            user_id="user1",
            session_id="session1"
        )
        assert item.id == "test1"
        assert item.text == "test content"
        assert item.user_id == "user1"

    def test_memory_item_default_fields(self):
        item = memory.MemoryItem(id="test1", text="test")
        assert item.score == 0.0
        assert item.metadata == {}
        assert item.embedding is None


class TestMemoryClass:
    @pytest.mark.asyncio
    async def test_memory_initialization(self):
        mem = memory.Memory()
        assert mem is not None
        assert hasattr(mem, "_recent")
        assert hasattr(mem, "_lock")

    @pytest.mark.asyncio
    async def test_memory_add_item(self):
        mem = memory.Memory()
        item = await mem.add("user1", "session1", "test message")
        assert item is not None
        assert item.text == "test message"
        assert item.user_id == "user1"

    @pytest.mark.asyncio
    async def test_memory_get_recent_empty(self):
        mem = memory.Memory()
        recent = await mem.get_recent("user1", "session1")
        assert recent == []

    @pytest.mark.asyncio
    async def test_memory_get_recent_with_items(self):
        mem = memory.Memory()
        await mem.add("user1", "session1", "msg1")
        await mem.add("user1", "session1", "msg2")
        recent = await mem.get_recent("user1", "session1")
        assert len(recent) == 2
        assert recent[0].text == "msg1"
        assert recent[1].text == "msg2"

    @pytest.mark.asyncio
    async def test_memory_get_recent_respects_limit(self):
        mem = memory.Memory()
        for i in range(30):
            await mem.add("user1", "session1", f"msg{i}")
        recent = await mem.get_recent("user1", "session1", limit=10)
        assert len(recent) == 10

    @pytest.mark.asyncio
    async def test_memory_search_empty(self):
        mem = memory.Memory()
        results = await mem.search("user1", "session1", "query")
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_memory_delete_item(self):
        mem = memory.Memory()
        item = await mem.add("user1", "session1", "test")
        await mem.delete("user1", "session1", item.id)
        recent = await mem.get_recent("user1", "session1")
        assert item.id not in [i.id for i in recent]

    @pytest.mark.asyncio
    async def test_memory_clear_session(self):
        mem = memory.Memory()
        await mem.add("user1", "session1", "msg1")
        await mem.add("user1", "session1", "msg2")
        await mem.clear("user1", "session1")
        recent = await mem.get_recent("user1", "session1")
        assert recent == []

    @pytest.mark.asyncio
    async def test_memory_stats_shape(self):
        mem = memory.Memory()
        await mem.add("user1", "session1", "test")
        stats = await mem.stats("user1", "session1")
        assert "total_items" in stats
        assert "total_chars" in stats
        assert "sessions" in stats

    @pytest.mark.asyncio
    async def test_memory_export_format(self):
        mem = memory.Memory()
        await mem.add("user1", "session1", "test")
        export = await mem.export("user1", "session1")
        assert isinstance(export, list)
        assert len(export) == 1
        assert "text" in export[0]
        assert "timestamp" in export[0]

    @pytest.mark.asyncio
    async def test_memory_import_items(self):
        mem = memory.Memory()
        items = [{"text": "imported1"}, {"text": "imported2"}]
        await mem.import_items("user1", "session1", items)
        recent = await mem.get_recent("user1", "session1")
        assert len(recent) == 2

    @pytest.mark.asyncio
    async def test_memory_concurrent_access(self):
        mem = memory.Memory()
        async def add_many(n):
            for i in range(n):
                await mem.add("user1", "session1", f"msg{i}")
        await asyncio.gather(*[add_many(10) for _ in range(5)])
        recent = await mem.get_recent("user1", "session1")
        assert len(recent) == 50

    @pytest.mark.asyncio
    async def test_memory_different_sessions_isolation(self):
        mem = memory.Memory()
        await mem.add("user1", "session1", "msg1")
        await mem.add("user1", "session2", "msg2")
        recent1 = await mem.get_recent("user1", "session1")
        recent2 = await mem.get_recent("user1", "session2")
        assert len(recent1) == 1
        assert len(recent2) == 1

    @pytest.mark.asyncio
    async def test_memory_different_users_isolation(self):
        mem = memory.Memory()
        await mem.add("user1", "session1", "msg1")
        await mem.add("user2", "session1", "msg2")
        recent1 = await mem.get_recent("user1", "session1")
        recent2 = await mem.get_recent("user2", "session1")
        assert len(recent1) == 1
        assert len(recent2) == 1


class TestMemorySingleton:
    @pytest.mark.asyncio
    async def test_get_memory_returns_instance(self):
        mem = await memory.get_memory()
        assert mem is not None
        assert isinstance(mem, memory.Memory)

    @pytest.mark.asyncio
    async def test_get_memory_singleton_stable(self):
        mem1 = await memory.get_memory()
        mem2 = await memory.get_memory()
        assert mem1 is mem2

    @pytest.mark.asyncio
    async def test_get_memory_no_race_condition(self):
        async def get_mem():
            return await memory.get_memory()
        results = await asyncio.gather(*[get_mem() for _ in range(10)])
        assert all(r is results[0] for r in results)


class TestRepoIndexer:
    def test_repo_indexer_placeholder_exists(self):
        assert hasattr(memory, "RepoIndexer")


class TestMemoryModuleExports:
    def test_memory_module_exports(self):
        required = ["Memory", "MemoryItem", "get_memory", "_VectorizeClient"]
        for sym in required:
            assert hasattr(memory, sym)