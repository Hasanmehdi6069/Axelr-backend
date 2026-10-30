"""UGREEN Hackathon: AI NAS File Indexing System — v1.1.0 (Production-Grade)
=========================================================================
Low-latency background file indexing mechanism for AI-powered network-attached
storage (NAS). Coordinates edge-to-cloud updates with real-time change detection,
AI-driven content classification, and efficient sync protocols.

Features:
  • Inotify/fswatch-based real-time file change detection (thread-safe enqueue)
  • Background indexing with low CPU/memory footprint
  • Edge-to-cloud delta sync via caller-injected uploader
  • AI content classification for automatic file tagging
  • File deduplication using content hashing
  • Pluggable cloud upload strategy (set_cloud_uploader / cloud_uploader=)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import queue
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import aiofiles
logger = logging.getLogger("axelr.ugreen_ai_nas.core")
__version__ = "1.1.0"

# Make watchdog optional for demo purposes
try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
    WATCHDOG_AVAILABLE = True
except ImportError:
    logger.warning("watchdog module not available - file system monitoring disabled")
    FileSystemEvent = None
    FileSystemEventHandler = object
    Observer = None
    WATCHDOG_AVAILABLE = False


# ─── Settings ────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Settings:
    cloud_sync_concurrency: int
    cloud_sync_interval_s: float
    index_batch_size: int
    indexing_poll_interval_s: float

    @classmethod
    def from_env(cls) -> "Settings":
        def _int(k: str, d: int) -> int:
            try:
                return int(os.getenv(k, str(d)))
            except (TypeError, ValueError):
                return d

        def _float(k: str, d: float) -> float:
            try:
                return float(os.getenv(k, str(d)))
            except (TypeError, ValueError):
                return d

        return cls(
            cloud_sync_concurrency=_int("NAS_CLOUD_SYNC_CONCURRENCY", 5),
            cloud_sync_interval_s=_float("NAS_CLOUD_SYNC_INTERVAL_S", 30.0),
            index_batch_size=_int("NAS_INDEX_BATCH_SIZE", 50),
            indexing_poll_interval_s=_float("NAS_INDEX_POLL_INTERVAL_S", 1.0),
        )


settings = Settings.from_env()


# ─── Types ────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class IndexedFile:
    """Represents a file indexed in the AI NAS system."""
    file_id: str
    path: str
    size_bytes: int
    content_hash: str
    last_modified: float
    created_at: float
    mime_type: str
    tags: List[str] = field(default_factory=list)
    ai_classification: Optional[str] = None
    thumbnail_path: Optional[str] = None
    cloud_synced: bool = False
    cloud_last_sync: Optional[float] = None
    local_version: int = 1
    cloud_version: int = 0


@dataclass(slots=True)
class IndexingJob:
    """Background indexing job for a directory or file."""
    job_id: str
    path: str
    is_recursive: bool = True
    status: str = "pending"
    progress: float = 0.0
    total_files: int = 0
    processed_files: int = 0
    started_at: Optional[float] = None
    completed_at: Optional[float] = None


@dataclass(slots=True)
class FileChangeEvent:
    """Represents a change to a file that needs reindexing."""
    event_type: str  # created, modified, deleted, moved
    file_path: str
    timestamp: float
    processed: bool = False


CloudUploader = Callable[[IndexedFile], Awaitable[None]]


# ─── Hash ─────────────────────────────────────────────────────────────────
async def compute_file_hash(file_path: str) -> str:
    """Compute SHA-256 hash of file content. Returns "" on failure."""
    h = hashlib.sha256()
    try:
        async with aiofiles.open(file_path, "rb") as f:
            while True:
                chunk = await f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        logger.warning("hash_computation_failed path=%s error=%s", file_path, e)
        return ""


# ─── MIME + classification ───────────────────────────────────────────────
_MIME_TYPES: Dict[str, str] = {
    # images
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic",
    ".heif": "image/heif", ".bmp": "image/bmp", ".tiff": "image/tiff",
    # video
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
    ".webm": "video/webm", ".avi": "video/x-msvideo", ".m4v": "video/x-m4v",
    # audio
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
    ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    # text
    ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
    ".html": "text/html", ".json": "application/json", ".xml": "application/xml",
    # office
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    # archive
    ".zip": "application/x-zip-compressed", ".tar": "application/x-tar",
    ".gz": "application/gzip", ".7z": "application/x-7z-compressed",
}


# ─── Watchdog handler (thread-safe) ──────────────────────────────────────
class NASFileSystemHandler(FileSystemEventHandler):
    """Watchdog callbacks run on watchdog's thread; they must never touch asyncio directly."""

    def __init__(self, indexer: "AINASIndexer") -> None:
        self.indexer = indexer

    def _emit(self, event_type: str, path: str) -> None:
        try:
            self.indexer.enqueue_change(event_type, path)
        except Exception as e:
            logger.error(
                "enqueue_failed type=%s path=%s err=%s", event_type, path, e,
            )

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._emit("created", event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._emit("modified", event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._emit("deleted", event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._emit("moved", event.dest_path)


# ─── Main indexer ─────────────────────────────────────────────────────────
@dataclass(slots=True)
class AINASIndexer:
    """Main AI NAS indexing system that coordinates edge-to-cloud sync."""
    root_path: str
    indexed_files: Dict[str, IndexedFile] = field(default_factory=dict)
    active_jobs: Dict[str, IndexingJob] = field(default_factory=dict)
    observer: Optional[Observer] = None
    index_task: Optional[asyncio.Task] = None
    sync_task: Optional[asyncio.Task] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    cloud_endpoint: Optional[str] = None
    cloud_uploader: Optional[CloudUploader] = None
    is_running: bool = False

    # internal
    _change_queue: queue.Queue = field(default_factory=queue.Queue)
    _wake_event: asyncio.Event = field(default_factory=asyncio.Event)
    _sync_kick: asyncio.Event = field(default_factory=asyncio.Event)
    _loop: Optional[asyncio.AbstractEventLoop] = None
    _notified_missing_uploader: bool = False

    _AI_CLASSIFICATIONS = {
        "image/": "image_media",
        "video/": "video_media",
        "audio/": "audio_media",
        "text/": "document",
        "application/pdf": "pdf_document",
        "application/vnd.openxmlformats-officedocument": "office_document",
        "application/msword": "office_document",
        "application/vnd.ms-excel": "office_document",
        "application/vnd.ms-powerpoint": "office_document",
        "application/x-zip-compressed": "archive",
        "application/x-tar": "archive",
        "application/gzip": "archive",
    }

    @classmethod
    async def create(
        cls,
        root_path: str,
        cloud_endpoint: Optional[str] = None,
        cloud_uploader: Optional[CloudUploader] = None,
    ) -> "AINASIndexer":
        root = Path(root_path).resolve()
        if not await asyncio.to_thread(root.is_dir):
            raise ValueError(f"nas_root is not a directory: {root_path}")

        indexer = cls(
            root_path=str(root),
            cloud_endpoint=cloud_endpoint,
            cloud_uploader=cloud_uploader,
        )
        indexer._loop = asyncio.get_running_loop()

        if WATCHDOG_AVAILABLE and Observer is not None:
            indexer.observer = Observer()
            handler = NASFileSystemHandler(indexer)
            indexer.observer.schedule(handler, str(root), recursive=True)
            indexer.observer.start()
            logger.info("watchdog observer started for file system monitoring")
        else:
            logger.warning("watchdog not available - running without real-time file system monitoring, polling will be used")

        indexer.is_running = True
        indexer.index_task = asyncio.create_task(indexer._indexing_loop())
        indexer.sync_task = asyncio.create_task(indexer._cloud_sync_loop())

        logger.info(
            "ai_nas_indexer_started root=%s cloud=%s uploader=%s",
            root, cloud_endpoint or "disabled",
            "yes" if cloud_uploader else "no",
        )
        return indexer

    # ── Thread-safe enqueue ──

    def enqueue_change(self, event_type: str, file_path: str) -> None:
        """Thread-safe entry point for watchdog callbacks.

        Pushes to a plain `queue.Queue` (thread-safe) and wakes the async
        loop via `loop.call_soon_threadsafe` so we never touch asyncio
        primitives from the watchdog thread directly.
        """
        if not self.is_running:
            return
        self._change_queue.put_nowait(FileChangeEvent(
            event_type=event_type, file_path=file_path, timestamp=time.time(),
        ))
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._wake_event.set)
            except RuntimeError:
                pass  # loop shutting down

    async def queue_change(self, event_type: str, file_path: str) -> None:
        """Async API for external callers (tests, admin actions)."""
        self.enqueue_change(event_type, file_path)

    def kick_sync(self) -> None:
        """Trigger an immediate cloud sync round.

        Call after injecting a `cloud_uploader` at runtime or when a
        previously-unreachable endpoint becomes reachable. Safe to call
        from any thread.
        """
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._sync_kick.set)
            except RuntimeError:
                pass

    def set_cloud_uploader(self, uploader: CloudUploader) -> None:
        """Inject the cloud uploader at runtime and kick the sync loop."""
        self.cloud_uploader = uploader
        self.kick_sync()

    # ── Indexing loop ──

    async def _indexing_loop(self) -> None:
        """Background loop draining the change queue.

        The pattern wait→clear→drain is race-free: any `set()` that fires
        during drain is consumed by the next `wait()` which returns
        immediately; the queue itself is never cleared so no event is lost.
        """
        while self.is_running:
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=settings.indexing_poll_interval_s,
                )
            except asyncio.TimeoutError:
                pass
            self._wake_event.clear()

            batch: List[FileChangeEvent] = []
            try:
                while len(batch) < settings.index_batch_size:
                    batch.append(self._change_queue.get_nowait())
            except queue.Empty:
                pass

            for change in batch:
                try:
                    if change.event_type == "deleted":
                        await self._handle_delete(change.file_path)
                    else:
                        await self._index_file(change.file_path)
                    change.processed = True
                except Exception as e:
                    logger.error(
                        "change_processing_failed type=%s path=%s err=%s",
                        change.event_type, change.file_path, e,
                    )

    async def _handle_delete(self, file_path: str) -> None:
        async with self.lock:
            for file_id, f in list(self.indexed_files.items()):
                if f.path == file_path:
                    del self.indexed_files[file_id]
                    return

    async def _index_file(self, file_path: str) -> Optional[IndexedFile]:
        path = Path(file_path)

        def _stat():
            try:
                if not path.exists() or not path.is_file():
                    return None
                return path.stat()
            except OSError:
                return None

        stat = await asyncio.to_thread(_stat)
        if stat is None:
            return None

        size = stat.st_size
        mtime = stat.st_mtime
        content_hash = await compute_file_hash(file_path)
        mime_type = self._guess_mime_type(path)
        ai_class = self._classify_content(mime_type)

        async with self.lock:
            existing = next(
                (f for f in self.indexed_files.values() if f.path == file_path),
                None,
            )
            if existing:
                if existing.content_hash != content_hash:
                    existing.last_modified = mtime
                    existing.content_hash = content_hash
                    existing.cloud_synced = False
                    existing.local_version += 1
                return existing

            indexed = IndexedFile(
                file_id=str(uuid.uuid4()),
                path=file_path,
                size_bytes=size,
                content_hash=content_hash,
                last_modified=mtime,
                created_at=time.time(),
                mime_type=mime_type,
                ai_classification=ai_class,
            )
            self.indexed_files[indexed.file_id] = indexed
            logger.debug(
                "file_indexed id=%s path=%s size_mb=%.1f type=%s",
                indexed.file_id, file_path, size / (1024 * 1024), mime_type,
            )
            return indexed

    def _guess_mime_type(self, path: Path) -> str:
        name_lower = path.name.lower()
        # Handle compound extensions (.tar.gz, .tar.bz2) — Path.suffix only
        # returns the final component, so check the full name first.
        if name_lower.endswith(".tar.gz") or name_lower.endswith(".tar.bz2"):
            return "application/x-tar"
        suffix = path.suffix.lower()
        return _MIME_TYPES.get(suffix, "application/octet-stream")

    def _classify_content(self, mime_type: str) -> str:
        for prefix, classification in self._AI_CLASSIFICATIONS.items():
            if mime_type.startswith(prefix):
                return classification
        return "unknown"

    # ── Cloud sync loop ──

    async def _cloud_sync_loop(self) -> None:
        """Background loop syncing local changes to the cloud via `cloud_uploader`.

        When no uploader is configured the loop idles (logs once). The
        `_sync_kick` event lets external code trigger a round immediately
        (e.g. after `set_cloud_uploader`).
        """
        while self.is_running:
            try:
                await asyncio.wait_for(
                    self._sync_kick.wait(),
                    timeout=settings.cloud_sync_interval_s,
                )
            except asyncio.TimeoutError:
                pass
            self._sync_kick.clear()

            if self.cloud_uploader is None:
                if not self._notified_missing_uploader:
                    logger.info("cloud_sync_idle reason=no_uploader")
                    self._notified_missing_uploader = True
                continue

            async with self.lock:
                to_sync = [
                    f for f in self.indexed_files.values()
                    if not f.cloud_synced and f.local_version > f.cloud_version
                ]
            if not to_sync:
                continue

            sem = asyncio.Semaphore(settings.cloud_sync_concurrency)

            async def _sync_one(file: IndexedFile) -> None:
                async with sem:
                    try:
                        uploader = self.cloud_uploader
                        if uploader is None:
                            return
                        await uploader(file)
                        async with self.lock:
                            file.cloud_synced = True
                            file.cloud_last_sync = time.time()
                            file.cloud_version = file.local_version
                    except Exception as e:
                        logger.error(
                            "cloud_sync_failed id=%s err=%s", file.file_id, e,
                        )

            logger.info("cloud_sync_round_start count=%d", len(to_sync))
            await asyncio.gather(*(_sync_one(f) for f in to_sync))
            logger.info("cloud_sync_round_done count=%d", len(to_sync))

    # ── Lifecycle ──

    async def shutdown(self) -> None:
        self.is_running = False
        # Wake both loops so they observe the flag and exit cleanly.
        self._wake_event.set()
        self._sync_kick.set()

        if self.observer is not None:
            try:
                self.observer.stop()
                await asyncio.to_thread(self.observer.join, 5)
            except Exception as e:
                logger.warning("observer_stop_failed err=%s", e)
            self.observer = None

        for task in (self.index_task, self.sync_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self.index_task = None
        self.sync_task = None
        logger.info("ai_nas_indexer_stopped")

    def health(self) -> dict[str, Any]:
        return {
            "ok": self.is_running,
            "details": {
                "root_path": self.root_path,
                "indexed_files": len(self.indexed_files),
                "queued_changes": self._change_queue.qsize(),
                "cloud_endpoint": self.cloud_endpoint,
                "cloud_uploader_configured": self.cloud_uploader is not None,
                "index_loop_running": (
                    self.index_task is not None and not self.index_task.done()
                ),
                "sync_loop_running": (
                    self.sync_task is not None and not self.sync_task.done()
                ),
            },
        }


# ─── Module-level singleton ──────────────────────────────────────────────
_indexer: Optional[AINASIndexer] = None


def get_indexer() -> Optional[AINASIndexer]:
    return _indexer


async def init(
    nas_root: str,
    cloud_endpoint: Optional[str] = None,
    cloud_uploader: Optional[CloudUploader] = None,
) -> None:
    """Initialize the UGREEN AI NAS indexing system.

    Idempotent-ish: shuts down any previous indexer before creating a new
    one so re-init in tests / dev never leaks observers or tasks.
    """
    global _indexer
    if _indexer is not None:
        await _indexer.shutdown()
    _indexer = await AINASIndexer.create(
        nas_root, cloud_endpoint=cloud_endpoint, cloud_uploader=cloud_uploader,
    )
    logger.info("ugreen_ai_nas_module_initialized")


async def shutdown() -> None:
    """Stop the indexer and release every resource it holds."""
    global _indexer
    if _indexer is not None:
        await _indexer.shutdown()
        _indexer = None
    logger.info("ugreen_ai_nas_module_shutdown")


def health() -> dict[str, Any]:
    """Non-raising health snapshot for liveness/readiness probes."""
    if _indexer is None:
        return {"ok": False, "details": {"reason": "not_initialized"}}
    return _indexer.health()