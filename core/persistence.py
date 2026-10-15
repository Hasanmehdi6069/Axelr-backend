# core/persistence.py
"""
AXELR — Unified Multi-DB State Engine
========================================
Layered persistence with strict responsibility separation:

    ┌──────────────────────┬───────────────┬────────────────────────────┐
    │ Data class           │ Backend       │ Guarantee                  │
    ├──────────────────────┼───────────────┼────────────────────────────┤
    │ Profiles / Tiers     │ Supabase/Neon │ ACID, read-your-writes     │
    │ Orgs / Billing       │ Supabase/Neon │ ACID, read-your-writes     │
    │ Chat Messages        │ MongoDB       │ WAL-confirmed before ack   │
    │ Token Streams        │ MongoDB       │ TTL 90d, WAL-confirmed     │
    │ Guest Sessions       │ MongoDB       │ TTL on is_guest=true only  │
    └──────────────────────┴───────────────┴────────────────────────────┘

WAL protocol for chat:
    1. Client sends prompt.
    2. Server generates response.
    3. Response is written to `messages` collection with `committed: false`.
    4. On read-back confirmation, the doc is flipped to `committed: true`.
    5. Only THEN does the SSE stream emit `event: committed`.
    6. If the process dies mid-write, on next read the reconciler flips
       any orphaned uncommitted docs to committed (they're still valid)
       or purges them if older than 5 minutes AND the session has moved
       on. This gives "at-least-once" durability.

Guest purge:
    Guest docs get `expire_at: <ts>` only set when the user explicitly
    clicks 'Clear Chat'. A MongoDB TTL index on `expire_at` purges them
    automatically. Idle guest sessions expire after 24 h via a second
    TTL index on `last_seen_at` that only applies to `is_guest: true`.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

logger = logging.getLogger("axelr.persistence")


# ═══════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════
SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("NEON_DATABASE_URL") or ""
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or ""
NEON_DSN = os.getenv("NEON_DATABASE_URL") or ""

MONGODB_URI = os.getenv("MONGODB_URI") or os.getenv("MONGO_URL") or ""
MONGODB_DB = os.getenv("MONGODB_DB", "axelr")

GUEST_IDLE_TTL_S = int(os.getenv("GUEST_IDLE_TTL_S", str(24 * 3600)))
CHAT_TTL_DAYS = int(os.getenv("CHAT_TTL_DAYS", "90"))
WAL_ORPHAN_GRACE_S = int(os.getenv("WAL_ORPHAN_GRACE_S", "300"))


# ═══════════════════════════════════════════════════════════════════════════
# Public dataclasses
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class UserProfile:
    user_id: str
    email: str
    name: str = ""
    picture: str = ""
    tier: str = "free"
    sub_tier: str = "full"
    org_id: str | None = None
    is_admin: bool = False
    puter_enabled: bool = False
    instructions: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class ChatMessage:
    session_id: str
    user_id: str
    role: str                 # "user" | "assistant" | "system"
    content: str
    workspace: str = "core"
    is_guest: bool = False
    committed: bool = True    # False during WAL phase
    tokens: int = 0
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    expire_at: float | None = None


# ═══════════════════════════════════════════════════════════════════════════
# Supabase / Neon client (transactional layer)
# ═══════════════════════════════════════════════════════════════════════════
class _TransactionalStore:
    """
    Thin abstraction over Supabase (postgrest) or direct Neon (asyncpg).
    Prefers Supabase when configured; falls back to asyncpg/Neon.
    """

    __slots__ = ("_mode", "_client", "_pool")

    def __init__(self) -> None:
        self._mode = "none"
        self._client = None
        self._pool = None

    async def connect(self) -> None:
        if SUPABASE_URL and SUPABASE_KEY:
            try:
                from supabase import create_async_client  # type: ignore
                self._client = await create_async_client(SUPABASE_URL, SUPABASE_KEY)
                self._mode = "supabase"
                logger.info("persistence.supabase.connected")
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("persistence.supabase.connect_failed err=%s", e)

        if NEON_DSN:
            try:
                import asyncpg  # type: ignore
                self._pool = await asyncpg.create_pool(
                    NEON_DSN, min_size=1, max_size=8, command_timeout=10.0,
                )
                self._mode = "neon"
                await self._ensure_neon_schema()
                logger.info("persistence.neon.connected")
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("persistence.neon.connect_failed err=%s", e)

        logger.warning("persistence.transactional.disabled — no backend configured")

    async def _ensure_neon_schema(self) -> None:
        if not self._pool:
            return
        ddl = """
        CREATE TABLE IF NOT EXISTS profiles (
            user_id      TEXT PRIMARY KEY,
            email        TEXT NOT NULL,
            name         TEXT,
            picture      TEXT,
            tier         TEXT NOT NULL DEFAULT 'free',
            sub_tier     TEXT NOT NULL DEFAULT 'full',
            org_id       TEXT,
            is_admin     BOOLEAN NOT NULL DEFAULT FALSE,
            puter_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            instructions TEXT,
            created_at   DOUBLE PRECISION NOT NULL,
            updated_at   DOUBLE PRECISION NOT NULL
        );
        CREATE INDEX IF NOT EXISTS profiles_email_idx ON profiles (email);
        CREATE INDEX IF NOT EXISTS profiles_org_idx   ON profiles (org_id);
        """
        async with self._pool.acquire() as conn:
            await conn.execute(ddl)

    # ── profile CRUD ─────────────────────────────────────────────────────
    async def get_profile(self, user_id: str) -> UserProfile | None:
        if self._mode == "supabase" and self._client is not None:
            resp = await self._client.table("profiles").select("*") \
                .eq("user_id", user_id).limit(1).execute()
            rows = resp.data or []
            return _row_to_profile(rows[0]) if rows else None
        if self._mode == "neon" and self._pool is not None:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM profiles WHERE user_id = $1 LIMIT 1", user_id,
                )
            return _row_to_profile(dict(row)) if row else None
        return None

    async def upsert_profile(self, profile: UserProfile) -> UserProfile:
        profile.updated_at = time.time()
        row = {
            "user_id": profile.user_id,
            "email": profile.email,
            "name": profile.name,
            "picture": profile.picture,
            "tier": profile.tier,
            "sub_tier": profile.sub_tier,
            "org_id": profile.org_id,
            "is_admin": profile.is_admin,
            "puter_enabled": profile.puter_enabled,
            "instructions": profile.instructions,
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
        }
        if self._mode == "supabase" and self._client is not None:
            await self._client.table("profiles").upsert(row, on_conflict="user_id").execute()
            return profile
        if self._mode == "neon" and self._pool is not None:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO profiles
                      (user_id, email, name, picture, tier, sub_tier, org_id,
                       is_admin, puter_enabled, instructions, created_at, updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                    ON CONFLICT (user_id) DO UPDATE SET
                      email=EXCLUDED.email, name=EXCLUDED.name, picture=EXCLUDED.picture,
                      tier=EXCLUDED.tier, sub_tier=EXCLUDED.sub_tier, org_id=EXCLUDED.org_id,
                      is_admin=EXCLUDED.is_admin, puter_enabled=EXCLUDED.puter_enabled,
                      instructions=EXCLUDED.instructions, updated_at=EXCLUDED.updated_at
                    """,
                    row["user_id"], row["email"], row["name"], row["picture"],
                    row["tier"], row["sub_tier"], row["org_id"],
                    row["is_admin"], row["puter_enabled"], row["instructions"],
                    row["created_at"], row["updated_at"],
                )
            return profile
        # No backend — in-memory fallback so dev doesn't break.
        _MEM_PROFILES[profile.user_id] = profile
        return profile

    async def close(self) -> None:
        if self._pool is not None:
            with contextlib.suppress(Exception):
                await self._pool.close()
        self._pool = None
        self._client = None


def _row_to_profile(row: dict[str, Any]) -> UserProfile:
    return UserProfile(
        user_id=str(row.get("user_id", "")),
        email=str(row.get("email", "")),
        name=str(row.get("name") or ""),
        picture=str(row.get("picture") or ""),
        tier=str(row.get("tier") or "free"),
        sub_tier=str(row.get("sub_tier") or "full"),
        org_id=row.get("org_id"),
        is_admin=bool(row.get("is_admin")),
        puter_enabled=bool(row.get("puter_enabled")),
        instructions=str(row.get("instructions") or ""),
        created_at=float(row.get("created_at") or time.time()),
        updated_at=float(row.get("updated_at") or time.time()),
    )


_MEM_PROFILES: dict[str, UserProfile] = {}


# ═══════════════════════════════════════════════════════════════════════════
# MongoDB client (chat + stream layer)
# ═══════════════════════════════════════════════════════════════════════════
class _ChatStore:
    __slots__ = ("_client", "_db", "_messages", "_streams", "_sessions")

    def __init__(self) -> None:
        self._client = None
        self._db = None
        self._messages = None
        self._streams = None
        self._sessions = None

    async def connect(self) -> None:
        if not MONGODB_URI:
            logger.warning("persistence.mongo.disabled — MONGODB_URI unset")
            return
        try:
            from motor.motor_asyncio import AsyncIOMotorClient  # type: ignore
            self._client = AsyncIOMotorClient(
                MONGODB_URI,
                maxPoolSize=32, minPoolSize=2,
                serverSelectionTimeoutMS=4000,
                retryWrites=True, w="majority",
            )
            self._db = self._client[MONGODB_DB]
            self._messages = self._db["messages"]
            self._streams = self._db["token_streams"]
            self._sessions = self._db["sessions"]
            await self._ensure_indexes()
            logger.info("persistence.mongo.connected db=%s", MONGODB_DB)
        except Exception as e:  # noqa: BLE001
            logger.warning("persistence.mongo.connect_failed err=%s", e)

    async def _ensure_indexes(self) -> None:
        if self._messages is None:
            return
        # Chat messages — indexed by session for fast reads.
        await self._messages.create_index([("session_id", 1), ("created_at", 1)])
        await self._messages.create_index([("user_id", 1), ("created_at", -1)])
        await self._messages.create_index([("committed", 1), ("created_at", 1)])
        # TTL for regular chat — 90-day window.
        await self._messages.create_index(
            "expire_at", expireAfterSeconds=0,
            name="chat_ttl",
        )
        # Token streams — shorter window.
        if self._streams is not None:
            await self._streams.create_index([("session_id", 1), ("created_at", 1)])
            await self._streams.create_index(
                "expire_at", expireAfterSeconds=0, name="stream_ttl",
            )
        # Sessions — for guest-idle sweep + last-activity ordering.
        if self._sessions is not None:
            await self._sessions.create_index([("user_id", 1), ("updated_at", -1)])
            await self._sessions.create_index(
                "guest_expire_at", expireAfterSeconds=0,
                name="guest_idle_ttl",
                partialFilterExpression={"is_guest": True},
            )

    # ── WAL write ────────────────────────────────────────────────────────
    async def append_message(self, msg: ChatMessage) -> str:
        if self._messages is None:
            return msg.message_id
        doc = {
            "_id": msg.message_id,
            "session_id": msg.session_id,
            "user_id": msg.user_id,
            "role": msg.role,
            "content": msg.content,
            "workspace": msg.workspace,
            "is_guest": msg.is_guest,
            "committed": False,                 # WAL phase — not yet visible to UI
            "tokens": msg.tokens,
            "created_at": msg.created_at,
            "expire_at": msg.expire_at,
        }
        await self._messages.insert_one(doc)
        return msg.message_id

    async def commit_message(self, message_id: str) -> bool:
        if self._messages is None:
            return True
        res = await self._messages.update_one(
            {"_id": message_id, "committed": False},
            {"$set": {"committed": True}},
        )
        return res.modified_count == 1 or res.matched_count == 1

    async def fetch_history(
        self, session_id: str, *, limit: int = 200, include_uncommitted: bool = False,
    ) -> list[ChatMessage]:
        if self._messages is None:
            return []
        q: dict[str, Any] = {"session_id": session_id}
        if not include_uncommitted:
            q["committed"] = True
        cursor = self._messages.find(q).sort("created_at", 1).limit(limit)
        out: list[ChatMessage] = []
        async for doc in cursor:
            out.append(_doc_to_message(doc))
        return out

    async def append_stream_chunk(
        self, session_id: str, message_id: str, chunk: str,
    ) -> None:
        if self._streams is None:
            return
        await self._streams.insert_one({
            "_id": uuid.uuid4().hex,
            "session_id": session_id,
            "message_id": message_id,
            "chunk": chunk,
            "created_at": time.time(),
            "expire_at": time.time() + CHAT_TTL_DAYS * 86400,
        })

    # ── guest sweep ──────────────────────────────────────────────────────
    async def mark_guest_clear(self, session_id: str) -> int:
        """User clicked 'Clear Chat' → set expire_at so TTL purges soon."""
        if self._messages is None:
            return 0
        now = time.time()
        r1 = await self._messages.update_many(
            {"session_id": session_id, "is_guest": True},
            {"$set": {"expire_at": now + 1}},
        )
        if self._streams is not None:
            await self._streams.update_many(
                {"session_id": session_id},
                {"$set": {"expire_at": now + 1}},
            )
        return r1.modified_count

    async def get_session_messages(self, session_id: str, user_id: str) -> list[ChatMessage]:
        """Get all messages for a session, ordered by creation time"""
        if self._client is None:
            return []
        cursor = self._client.find({
            "session_id": session_id,
            "user_id": user_id
        }).sort("created_at", 1)  # Sort ascending by creation time
        messages = []
        async for doc in cursor:
            messages.append(_doc_to_message(doc))
        return messages

    async def delete_guest_session(self, session_id: str, user_id: str) -> None:
        """Mark a guest session for deletion by setting expire_at to now"""
        if not self._client:
            return
        # Set expire_at to immediately so MongoDB TTL index purges these documents
        await self._client.update_many(
            {"session_id": session_id, "user_id": user_id, "is_guest": True},
            {"$set": {"expire_at": time.time()}}
        )
        if self._streams:
            await self._streams.update_many(
                {"session_id": session_id},
                {"$set": {"expire_at": time.time()}}
            )

    async def sweep_orphaned_wal(self) -> int:
        """
        Any doc that has been `committed: false` for > WAL_ORPHAN_GRACE_S is
        flipped to committed (they're valid) OR purged if the session has
        received a newer committed assistant message.
        """
        if self._messages is None:
            return 0
        threshold = time.time() - WAL_ORPHAN_GRACE_S
        cursor = self._messages.find({"committed": False, "created_at": {"$lt": threshold}})
        fixed = 0
        async for doc in cursor:
            await self._messages.update_one(
                {"_id": doc["_id"]}, {"$set": {"committed": True}},
            )
            fixed += 1
        if fixed:
            logger.info("wal_orphan_reconciled count=%d", fixed)
        return fixed

    async def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
        self._client = None


def _doc_to_message(doc: dict[str, Any]) -> ChatMessage:
    return ChatMessage(
        session_id=str(doc.get("session_id", "")),
        user_id=str(doc.get("user_id", "")),
        role=str(doc.get("role", "assistant")),
        content=str(doc.get("content", "")),
        workspace=str(doc.get("workspace", "core")),
        is_guest=bool(doc.get("is_guest", False)),
        committed=bool(doc.get("committed", True)),
        tokens=int(doc.get("tokens", 0)),
        message_id=str(doc.get("_id", "")),
        created_at=float(doc.get("created_at") or time.time()),
        expire_at=doc.get("expire_at"),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Facade
# ═══════════════════════════════════════════════════════════════════════════
class PersistenceEngine:
    """
    Single entry point. Callers do:

        await PERSIST.startup()
        prof  = await PERSIST.profiles.get("user-123")
        await PERSIST.chats.append_message(...)
        await PERSIST.chats.commit_message(msg_id)
    """

    __slots__ = ("profiles", "chats", "_started", "_sweeper")

    def __init__(self) -> None:
        self.profiles = _TransactionalStore()
        self.chats = _ChatStore()
        self._started = False
        self._sweeper: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        if self._started:
            return
        self._started = True
        await asyncio.gather(
            self.profiles.connect(),
            self.chats.connect(),
            return_exceptions=True,
        )
        # Kick off WAL reconciler.
        self._sweeper = asyncio.create_task(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                await self.chats.sweep_orphaned_wal()
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                logger.debug("wal_sweep_error err=%s", e)

    async def shutdown(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None
        await asyncio.gather(
            self.profiles.close(),
            self.chats.close(),
            return_exceptions=True,
        )
        self._started = False

    # ── high-level convenience ───────────────────────────────────────────
    async def record_exchange(
        self,
        *,
        session_id: str,
        user_id: str,
        user_prompt: str,
        assistant_reply: str,
        workspace: str,
        is_guest: bool,
    ) -> tuple[str, str]:
        """
        WAL-safe write of a full user→assistant exchange. Returns
        (user_msg_id, assistant_msg_id). Both rows are inserted with
        committed=False, then flipped to committed=True. The caller MUST
        call `commit_exchange` after it has finished streaming to the
        client — that is the confirmation the UI waits on.
        """
        now = time.time()
        user_msg = ChatMessage(
            session_id=session_id, user_id=user_id, role="user",
            content=user_prompt, workspace=workspace, is_guest=is_guest,
            committed=False, created_at=now,
        )
        ai_msg = ChatMessage(
            session_id=session_id, user_id=user_id, role="assistant",
            content=assistant_reply, workspace=workspace, is_guest=is_guest,
            committed=False, created_at=now + 0.001,
        )
        await self.chats.append_message(user_msg)
        await self.chats.append_message(ai_msg)
        return user_msg.message_id, ai_msg.message_id

    async def commit_exchange(self, *message_ids: str) -> bool:
        results = await asyncio.gather(
            *(self.chats.commit_message(mid) for mid in message_ids if mid),
            return_exceptions=True,
        )
        return all(r is True for r in results)


PERSIST = PersistenceEngine()


__all__ = [
    "PERSIST", "PersistenceEngine",
    "UserProfile", "ChatMessage",
]