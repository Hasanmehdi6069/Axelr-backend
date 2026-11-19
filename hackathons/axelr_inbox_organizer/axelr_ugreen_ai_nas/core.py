"""UGOS Skill engine — Inbox Organizer (Automate Your Workflow track).

Pipeline: inbox watcher → Apex Router local extraction → safe file
organization (Year/Month/Category) → 50-entry undo log → daily summary.
Everything runs on-device. Zero cloud calls.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .apex_client import (
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    ApexRouterClient,
)

logger = logging.getLogger("axelr.ugreen_ai_nas.core")
__version__ = "2.0.0"

__all__ = [
    "DocumentSkill", "DocumentMetadata", "UndoEntry", "ProcessingResult",
    "Settings", "get_skill", "init", "shutdown", "health", "is_healthy",
    "compute_hash", "extract_text",
]

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    WATCHDOG_AVAILABLE = True
except ImportError:  # pragma: no cover
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment]
    WATCHDOG_AVAILABLE = False
    logger.warning("watchdog unavailable — backlog scan only, no live watching")


@dataclass(frozen=True)
class Settings:
    """Runtime tunables, overridable via env for the NAS host."""

    inbox: str
    library: str
    state_dir: str
    summary_dir: str
    apex_endpoint: str = DEFAULT_ENDPOINT
    apex_model: str = DEFAULT_MODEL
    debounce_s: float = 2.0
    max_text_chars: int = 8000
    undo_log_size: int = 50
    allowed_ext: tuple[str, ...] = (".pdf", ".eml", ".txt", ".md", ".png", ".jpg", ".jpeg")

    @classmethod
    def from_env(cls, root: str | Path) -> "Settings":
        base = Path(root).expanduser().resolve()
        return cls(
            inbox=str(base / "inbox"),
            library=str(base / "library"),
            state_dir=str(base / "_skill"),
            summary_dir=str(base / "_summary"),
            apex_endpoint=os.getenv("APEX_ROUTER_ENDPOINT", DEFAULT_ENDPOINT),
            apex_model=os.getenv("APEX_ROUTER_MODEL", DEFAULT_MODEL),
        )


@dataclass
class DocumentMetadata:
    document_type: str = "other"
    date: Optional[str] = None
    sender: Optional[str] = None
    total_amount: Optional[float] = None
    currency: Optional[str] = None
    due_date: Optional[str] = None
    category: str = "Other"
    confidence: float = 0.0

    @classmethod
    def from_payload(cls, p: dict[str, Any]) -> "DocumentMetadata":
        return cls(
            document_type=str(p.get("document_type") or "other"),
            date=p.get("date"),
            sender=p.get("sender"),
            total_amount=p.get("total_amount"),
            currency=p.get("currency"),
            due_date=p.get("due_date"),
            category=str(p.get("category") or "Other"),
            confidence=float(p.get("confidence") or 0.0),
        )


@dataclass
class UndoEntry:
    op_id: str
    timestamp: float
    src: str
    dst: str
    filename: str
    category: str
    content_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProcessingResult:
    source: str
    source_name: str
    destination: Optional[str] = None
    metadata: Optional[DocumentMetadata] = None
    error: Optional[str] = None
    elapsed_ms: float = 0.0


# ─── Content helpers ────────────────────────────────────────────────────

async def compute_hash(path: str | Path) -> str:
    """SHA-256 of file bytes, computed off the event loop."""
    def _run() -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    try:
        return await asyncio.to_thread(_run)
    except OSError as exc:
        logger.warning("hash_failed path=%s err=%s", path, exc)
        return ""


def _extract_pdf_text(path: Path, max_chars: int) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        logger.warning("pypdf_missing path=%s degraded_extraction", path)
        return ""
    try:
        reader = PdfReader(str(path))
        chunks: list[str] = []
        total = 0
        for page in reader.pages[:10]:
            text = page.extract_text() or ""
            chunks.append(text)
            total += len(text)
            if total >= max_chars:
                break
        return "\n".join(chunks)[:max_chars]
    except Exception as exc:
        logger.warning("pdf_extract_failed path=%s err=%s", path, exc)
        return ""


def _eml_header_text(path: Path) -> str:
    """Pre-extract From/Date/Subject so the model has strong anchors."""
    try:
        from email import policy
        from email.parser import BytesParser
        with open(path, "rb") as fh:
            msg = BytesParser(policy=policy.default).parse(fh)
        return "\n".join(
            f"{key}: {msg.get(key, '')}" for key in ("From", "Date", "Subject", "To")
        )
    except Exception:
        return ""


async def extract_text(path: Path, max_chars: int) -> str:
    """Best-effort local text extraction — no network, no external services."""
    suffix = path.suffix.lower()
    if suffix == ".eml":
        header = await asyncio.to_thread(_eml_header_text, path)
        try:
            body = await asyncio.to_thread(path.read_text, errors="ignore")
        except OSError:
            body = ""
        return (header + "\n\n" + body)[:max_chars]
    if suffix in {".txt", ".md", ".csv", ".json", ".xml", ".log"}:
        try:
            text = await asyncio.to_thread(path.read_text, errors="ignore")
            return text[:max_chars]
        except OSError:
            return ""
    if suffix == ".pdf":
        return await asyncio.to_thread(_extract_pdf_text, path, max_chars)
    try:
        raw = await asyncio.to_thread(path.read_bytes)
        return raw[: max_chars * 4].decode("latin-1", errors="ignore")[:max_chars]
    except OSError:
        return ""


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_segment(value: str, fallback: str = "untitled") -> str:
    cleaned = _UNSAFE.sub("-", value.strip())[:80].strip("-._")
    return cleaned or fallback


def _target_path(library: Path, meta: DocumentMetadata, original: Path) -> Path:
    date = meta.date or time.strftime("%Y-%m-%d")
    year, month = date[:4], date[5:7]
    category = _safe_segment(meta.category, "Other")
    sender = _safe_segment(meta.sender or "", "unknown")
    stem = _safe_segment(original.stem)[:60]
    ext = original.suffix.lower()
    return library / year / month / category / f"{date}__{sender}__{stem}{ext}"


# ─── Undo log ───────────────────────────────────────────────────────────

class UndoLog:
    """Persisted ring buffer of the last N move operations, as JSONL."""

    def __init__(self, path: Path, max_entries: int = 50) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._max = max_entries
        self._lock = asyncio.Lock()
        self._entries: list[UndoEntry] = self._load()

    def _load(self) -> list[UndoEntry]:
        if not self._path.exists():
            return []
        entries: list[UndoEntry] = []
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(UndoEntry(**json.loads(line)))
                    except (json.JSONDecodeError, TypeError):
                        continue
        except OSError as exc:
            logger.warning("undo_log_load_failed err=%s", exc)
        return entries[-self._max :]

    def _write_all(self) -> None:
        tmp = self._path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for entry in self._entries:
                fh.write(json.dumps(entry.__dict__, default=str) + "\n")
        os.replace(tmp, self._path)

    async def record(self, entry: UndoEntry) -> None:
        async with self._lock:
            self._entries.append(entry)
            if len(self._entries) > self._max:
                self._entries = self._entries[-self._max :]
            await asyncio.to_thread(self._write_all)

    async def pop_last(self) -> Optional[UndoEntry]:
        async with self._lock:
            if not self._entries:
                return None
            entry = self._entries.pop()
            await asyncio.to_thread(self._write_all)
            return entry

    async def snapshot(self) -> list[UndoEntry]:
        async with self._lock:
            return list(self._entries)


# ─── Daily summary ──────────────────────────────────────────────────────

class DailySummary:
    """JSONL sidecar + rendered markdown for each day's processed documents."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def _paths(self, when: Optional[float] = None) -> tuple[Path, Path]:
        day = time.strftime("%Y-%m-%d", time.localtime(when))
        return self._root / f"{day}.jsonl", self._root / f"{day}.md"

    async def append(self, result: ProcessingResult) -> None:
        if result.metadata is None:
            return
        record = {
            "source_name": result.source_name,
            "destination": result.destination,
            "category": result.metadata.category,
            "document_type": result.metadata.document_type,
            "sender": result.metadata.sender,
            "total_amount": result.metadata.total_amount,
            "currency": result.metadata.currency,
            "due_date": result.metadata.due_date,
            "confidence": result.metadata.confidence,
            "processed_at": time.time(),
        }
        jsonl_path, md_path = self._paths()
        async with self._lock:
            def _write() -> None:
                with jsonl_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, default=str) + "\n")
                entries = []
                with jsonl_path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
                md_path.write_text(_render_summary(entries), encoding="utf-8")
            await asyncio.to_thread(_write)

    async def render(self, when: Optional[float] = None) -> str:
        jsonl_path, _ = self._paths(when)
        if not jsonl_path.exists():
            return f"# What's new — {time.strftime('%Y-%m-%d', time.localtime(when))}\n\n_No documents processed yet._\n"

        def _read() -> list[dict[str, Any]]:
            entries = []
            with jsonl_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            return entries
        return _render_summary(await asyncio.to_thread(_read), when)


def _render_summary(entries: list[dict[str, Any]], when: Optional[float] = None) -> str:
    day = time.strftime("%Y-%m-%d", time.localtime(when))
    lines = [f"# What's new — {day}", ""]
    if not entries:
        lines.append("_No documents processed yet._")
        return "\n".join(lines) + "\n"
    by_cat = Counter(e.get("category", "Other") for e in entries)
    totals: dict[str, float] = defaultdict(float)
    for entry in entries:
        amount = entry.get("total_amount")
        if isinstance(amount, (int, float)):
            totals[(entry.get("currency") or "USD").upper()] += float(amount)
    due_soon = 0
    today = datetime.now().date()
    for entry in entries:
        due = entry.get("due_date")
        if not due:
            continue
        try:
            delta = (datetime.strptime(due, "%Y-%m-%d").date() - today).days
            if 0 <= delta <= 7:
                due_soon += 1
        except ValueError:
            continue
    lines.append(f"**{len(entries)} document(s) processed.**")
    lines.append("")
    lines.append("By category: " + ", ".join(f"{k}: {v}" for k, v in by_cat.items()))
    if totals:
        lines.append("Totals: " + ", ".join(f"{cur} {amt:,.2f}" for cur, amt in totals.items()))
    if due_soon:
        lines.append(f"{due_soon} due within 7 days.")
    lines.append("")
    for entry in entries:
        lines.append(
            f"- `{entry.get('source_name', '?')}` → `{entry.get('destination', '?')}` "
            f"({entry.get('category', 'Other')})"
        )
    return "\n".join(lines) + "\n"


# ─── Watcher bridge (thread → asyncio) ──────────────────────────────────

class _WatchHandler(FileSystemEventHandler):
    """Watchdog callbacks run on a foreign thread — never touch asyncio here."""

    def __init__(self, skill: "DocumentSkill") -> None:
        self._skill = skill

    def on_created(self, event) -> None:  # type: ignore[override]
        if not getattr(event, "is_directory", False):
            self._skill.enqueue(Path(event.src_path))

    def on_modified(self, event) -> None:  # type: ignore[override]
        if not getattr(event, "is_directory", False):
            self._skill.enqueue(Path(event.src_path))


# ─── The orchestrator ───────────────────────────────────────────────────

@dataclass
class DocumentSkill:
    settings: Settings
    client: ApexRouterClient
    undo_log: UndoLog
    summary: DailySummary
    inbox: Path
    library: Path
    is_running: bool = False
    _observer: Any = field(default=None, repr=False)
    _drain_task: Optional[asyncio.Task] = field(default=None, repr=False)
    _loop: Optional[asyncio.AbstractEventLoop] = field(default=None, repr=False)
    _pending: dict[str, float] = field(default_factory=dict, repr=False)
    _pending_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _processed: set[str] = field(default_factory=set, repr=False)

    @classmethod
    async def create(cls, root: str | Path, *, settings: Optional[Settings] = None) -> "DocumentSkill":
        s = settings or Settings.from_env(root)
        inbox, library = Path(s.inbox), Path(s.library)
        state = Path(s.state_dir)
        for directory in (inbox, library, state, Path(s.summary_dir)):
            directory.mkdir(parents=True, exist_ok=True)
        client = ApexRouterClient(endpoint=s.apex_endpoint, model=s.apex_model)
        skill = cls(
            settings=s,
            client=client,
            undo_log=UndoLog(state / "undo.jsonl", s.undo_log_size),
            summary=DailySummary(Path(s.summary_dir)),
            inbox=inbox,
            library=library,
        )
        skill._loop = asyncio.get_running_loop()
        skill._load_processed()
        logger.info(
            "skill_created inbox=%s library=%s model=%s offline=True",
            inbox, library, s.apex_model,
        )
        return skill

    # ── Dedupe state ──

    def _processed_path(self) -> Path:
        return Path(self.settings.state_dir) / "processed.json"

    def _load_processed(self) -> None:
        path = self._processed_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._processed = set(data)
        except (OSError, json.JSONDecodeError):
            self._processed = set()

    def _persist_processed(self) -> None:
        path = self._processed_path()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sorted(self._processed)), encoding="utf-8")
        os.replace(tmp, path)

    # ── Lifecycle ──

    async def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        if WATCHDOG_AVAILABLE and Observer is not None:
            self._observer = Observer()
            self._observer.schedule(_WatchHandler(self), str(self.inbox), recursive=True)
            self._observer.start()
            logger.info("watcher_started inbox=%s", self.inbox)
        else:
            logger.warning("watcher_disabled backlog_scan_only=True")
        self._drain_task = asyncio.create_task(self._drain_loop())
        await self._scan_backlog()

    async def stop(self) -> None:
        self.is_running = False
        self._wake.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                await asyncio.to_thread(self._observer.join, 5)
            except Exception as exc:  # pragma: no cover
                logger.warning("observer_stop_failed err=%s", exc)
            self._observer = None
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None
        await self.client.close()
        logger.info("skill_stopped")

    async def _scan_backlog(self) -> None:
        def _walk() -> list[Path]:
            return [p for p in self.inbox.rglob("*") if p.is_file()]
        for path in await asyncio.to_thread(_walk):
            self.enqueue(path, immediate=True)
        logger.info("backlog_scanned count=%d", len(await asyncio.to_thread(_walk)))

    # ── Enqueue (thread-safe) ──

    def enqueue(self, path: Path, immediate: bool = False) -> None:
        if not self.is_running:
            return
        deadline = time.time() + (0.0 if immediate else self.settings.debounce_s)
        with self._pending_lock:
            self._pending[str(path)] = deadline
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._wake.set)
            except RuntimeError:
                pass

    async def _drain_loop(self) -> None:
        while self.is_running:
            now = time.time()
            with self._pending_lock:
                due = [p for p, dl in self._pending.items() if dl <= now]
                for p in due:
                    self._pending.pop(p, None)
            if due:
                for raw in due:
                    await self._process(Path(raw))
                continue
            with self._pending_lock:
                deadlines = list(self._pending.values())
            timeout = max(0.05, min(deadlines) - time.time()) if deadlines else 1.0
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    # ── Pipeline ──

    async def _process(self, path: Path) -> Optional[ProcessingResult]:
        started = time.perf_counter()
        try:
            if not path.is_file():
                return None
            if path.suffix.lower() not in self.settings.allowed_ext:
                return None
            digest = await compute_hash(path)
            if not digest:
                return None
            if digest in self._processed:
                logger.debug("skip_duplicate path=%s", path)
                return None
            content = await extract_text(path, self.settings.max_text_chars)
            payload = await self.client.extract_metadata(path.name, content)
            meta = DocumentMetadata.from_payload(payload)
            target = _target_path(self.library, meta, path)
            final = await asyncio.to_thread(self._move, path, target)
            entry = UndoEntry(
                op_id=str(uuid.uuid4()), timestamp=time.time(),
                src=str(path), dst=str(final), filename=path.name,
                category=meta.category, content_hash=digest,
                metadata=payload,
            )
            await self.undo_log.record(entry)
            self._processed.add(digest)
            await asyncio.to_thread(self._persist_processed)
            result = ProcessingResult(
                source=str(path), source_name=path.name, destination=str(final),
                metadata=meta, elapsed_ms=(time.perf_counter() - started) * 1000,
            )
            await self.summary.append(result)
            logger.info(
                "document_filed src=%s dst=%s type=%s category=%s confidence=%.2f",
                path.name, final, meta.document_type, meta.category, meta.confidence,
            )
            return result
        except Exception as exc:
            logger.exception("processing_failed path=%s err=%s", path, exc)
            return ProcessingResult(
                source=str(path), source_name=path.name, error=str(exc),
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )

    @staticmethod
    def _move(src: Path, dst: Path) -> Path:
        dst.parent.mkdir(parents=True, exist_ok=True)
        final, i = dst, 1
        while final.exists():
            final = dst.with_name(f"{dst.stem}-{i}{dst.suffix}")
            i += 1
        try:
            os.replace(src, final)  # atomic within the same filesystem
        except OSError:
            shutil.move(str(src), str(final))  # cross-device fallback
        return final

    async def process_now(self, path: str | Path) -> Optional[ProcessingResult]:
        """Synchronous single-file entry point for tests and the demo."""
        return await self._process(Path(path))

    async def undo_last(self) -> Optional[UndoEntry]:
        entry = await self.undo_log.pop_last()
        if entry is None:
            return None
        src, dst = Path(entry.src), Path(entry.dst)
        if dst.exists():
            src.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.move, str(dst), str(src))
        self._processed.discard(entry.content_hash)
        await asyncio.to_thread(self._persist_processed)
        logger.info("undo_applied op_id=%s dst=%s restored_to=%s", entry.op_id, dst, src)
        return entry

    async def whats_new(self, when: Optional[float] = None) -> str:
        return await self.summary.render(when)

    def health(self) -> dict[str, Any]:
        return {
            "ok": self.is_running,
            "details": {
                "inbox": str(self.inbox),
                "library": str(self.library),
                "pending": len(self._pending),
                "processed_hashes": len(self._processed),
                "undo_entries": len(self.undo_log._entries),  # noqa: SLF001
                "watchdog": WATCHDOG_AVAILABLE,
                "apex_endpoint": self.settings.apex_endpoint,
                "apex_model": self.settings.apex_model,
                "cloud_calls": False,
            },
        }


# ─── Module singleton ───────────────────────────────────────────────────

_skill: Optional[DocumentSkill] = None


def get_skill() -> Optional[DocumentSkill]:
    return _skill


async def init(root: str | Path, *, settings: Optional[Settings] = None) -> DocumentSkill:
    global _skill
    if _skill is not None:
        await _skill.stop()
    _skill = await DocumentSkill.create(root, settings=settings)
    await _skill.start()
    logger.info("skill_initialized root=%s", root)
    return _skill


async def shutdown() -> None:
    global _skill
    if _skill is not None:
        await _skill.stop()
        _skill = None
    logger.info("skill_shutdown")


def health() -> dict[str, Any]:
    if _skill is None:
        return {"ok": False, "details": {"reason": "not_initialized"}}
    return _skill.health()


async def is_healthy() -> bool:
    return health().get("ok", False)
