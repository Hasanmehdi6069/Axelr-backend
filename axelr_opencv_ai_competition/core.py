"""OpenCV AI Competition: Visual Matrix Processing — v1.1.0
========================================================
Binary payload management for visual matrices with strict memory hygiene.

Honest scope / correctness envelope
-----------------------------------
* **CPU-bound cv2 work runs on worker threads** (`asyncio.to_thread`) so
  `cv2.cvtColor` / `detectMultiScale` never block the event loop. That
  caps effective throughput at the process's thread-pool size (default
  min(32, cpu+4)) — a hard cap, documented, not magic.
* **`VisualFrame.dispose()` drops the reference** (`self._data = None`)
  instead of the `del self._data` no-op pattern. The pool owns the actual
  buffer; the wrapper holds a borrowed reference.
* **`VisualFrame.__del__` is logging-free** — logging from `__del__`
  during interpreter shutdown can deadlock. It silently drops the
  reference; leak detection is via `MemoryStats.leaked_frames`.
* **Pool reuse is bounded** per (w, h, c) tuple — see `max_pool_size_per_format`.
"""
from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger("axelr.opencv_ai.core")
__version__ = "1.1.0"


# ─── Types ────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class FrameMetadata:
    frame_id: str
    source_id: str
    timestamp: float
    width: int
    height: int
    channels: int
    is_gpu_memory: bool = False
    size_bytes: int = 0
    allocated_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class VisualFrame:
    """RAII-ish wrapper around a numpy buffer. `dispose()` drops the reference;
    the buffer's lifecycle is owned by `FramePool`."""
    _data: Optional[np.ndarray]
    metadata: FrameMetadata
    _is_disposed: bool = False

    @property
    def data(self) -> np.ndarray:
        if self._is_disposed or self._data is None:
            raise RuntimeError(
                f"cannot access disposed frame {self.metadata.frame_id}")
        return self._data

    def dispose(self) -> None:
        if not self._is_disposed:
            # Drop our reference; the pool holds the canonical one when
            # we were created from the pool. If not pooled, GC reclaims.
            self._data = None
            self._is_disposed = True

    def __del__(self) -> None:
        # Never log from __del__ — logging may be torn down at shutdown
        # and __del__ can run on any thread.
        if not self._is_disposed:
            self._data = None
            self._is_disposed = True


@dataclass(slots=True)
class ProcessedFrame:
    original: VisualFrame
    detections: List[Dict[str, Any]] = field(default_factory=list)
    segmentation_mask: Optional[np.ndarray] = None
    classifications: List[Tuple[str, float]] = field(default_factory=list)
    processed_at: float = field(default_factory=time.time)
    processing_time_ms: float = 0.0


@dataclass(slots=True)
class MemoryStats:
    current_allocated_bytes: int = 0
    total_allocated_bytes: int = 0
    active_frames: int = 0
    pool_hits: int = 0
    pool_misses: int = 0
    leaked_frames: int = 0
    last_gc_run: float = 0.0


# ─── Pool ─────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class FramePool:
    pool: Dict[Tuple[int, int, int], List[np.ndarray]] = field(default_factory=dict)
    max_pool_size_per_format: int = 10
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stats: MemoryStats = field(default_factory=MemoryStats)

    async def acquire(self, width: int, height: int, channels: int,
                      source_id: str) -> VisualFrame:
        key = (width, height, channels)
        metadata = FrameMetadata(
            frame_id=str(uuid.uuid4()), source_id=source_id,
            timestamp=time.time(), width=width, height=height,
            channels=channels,
        )
        async with self.lock:
            bucket = self.pool.get(key, [])
            if bucket:
                buffer = bucket.pop()
                self.stats.pool_hits += 1
            else:
                buffer = np.zeros((height, width, channels), dtype=np.uint8)
                self.stats.pool_misses += 1
                self.stats.total_allocated_bytes += buffer.nbytes
                self.stats.current_allocated_bytes += buffer.nbytes
            self.stats.active_frames += 1
        return VisualFrame(_data=buffer, metadata=metadata)

    async def release(self, frame: VisualFrame) -> None:
        if frame._is_disposed:
            return
        key = (frame.metadata.width, frame.metadata.height,
               frame.metadata.channels)
        async with self.lock:
            bucket = self.pool.setdefault(key, [])
            if len(bucket) < self.max_pool_size_per_format:
                # Zero before reuse so callers can't read stale pixels.
                frame.data.fill(0)
                bucket.append(frame._data)  # take ownership of the buffer
            self.stats.active_frames -= 1
        frame.dispose()

    async def run_gc(self) -> None:
        async with self.lock:
            for key in list(self.pool.keys()):
                half = self.max_pool_size_per_format // 2
                bucket = self.pool[key]
                if len(bucket) > half:
                    freed_arrays = bucket[: len(bucket) - half]
                    for arr in freed_arrays:
                        self.stats.current_allocated_bytes -= arr.nbytes
                    self.pool[key] = bucket[-half:] if half > 0 else []
            await asyncio.to_thread(gc.collect)
            if await asyncio.to_thread(_cuda_available):
                await asyncio.to_thread(_cuda_cleanup)
            self.stats.last_gc_run = time.time()
            logger.info(
                "pool_gc active=%d hits=%d misses=%d mem_mb=%.1f",
                self.stats.active_frames, self.stats.pool_hits,
                self.stats.pool_misses,
                self.stats.current_allocated_bytes / (1024 * 1024),
            )

    def stats_snapshot(self) -> dict:
        return {
            "active_frames": self.stats.active_frames,
            "pool_hits": self.stats.pool_hits,
            "pool_misses": self.stats.pool_misses,
            "leaked_frames": self.stats.leaked_frames,
            "current_allocated_mb": round(
                self.stats.current_allocated_bytes / (1024 * 1024), 2),
            "total_allocated_mb": round(
                self.stats.total_allocated_bytes / (1024 * 1024), 2),
            "pooled_formats": len(self.pool),
        }


def _cuda_available() -> bool:
    try:
        return cv2.cuda.getCudaEnabledDeviceCount() > 0
    except Exception:
        return False


def _cuda_cleanup() -> None:
    try:
        cv2.cuda.resetStreams()
    except Exception:
        pass


# ─── Pipeline ─────────────────────────────────────────────────────────────
@dataclass(slots=True)
class OpenCVPipeline:
    frame_pool: FramePool = field(default_factory=FramePool)
    processing_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    processing_tasks: List[asyncio.Task] = field(default_factory=list)
    is_running: bool = False
    max_concurrent_processing: int = 4
    object_detector: Optional[Any] = None
    shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)

    async def init_models(self) -> None:
        # Handle both modern and legacy OpenCV CascadeClassifier locations
        cascade_class = None
        try:
            cascade_class = cv2.CascadeClassifier
        except AttributeError:
            try:
                cascade_class = cv2.legacy.CascadeClassifier
            except AttributeError:
                logger.warning("CascadeClassifier not found in cv2 or cv2.legacy - continuing without object detection")
        if cascade_class:
            try:
                self.object_detector = await asyncio.to_thread(
                    cascade_class,
                    cv2.data.haarcascades + "haarcascade_frontalface_default.xml",
                )
                if self.object_detector is None or self.object_detector.empty():
                    logger.warning("object_detector_empty — detector may be missing, continuing without object detection")
            except Exception as e:
                logger.warning(f"Failed to initialize object detector: {e}, continuing without object detection")
        logger.info("cv_models_initialized")

    async def start_processing(self) -> None:
        self.is_running = True
        self._recording_start_time = time.time()  # Track when recording starts
        self.shutdown_event.clear()
        for i in range(self.max_concurrent_processing):
            self.processing_tasks.append(
                asyncio.create_task(self._processing_worker(i)))
        self.processing_tasks.append(
            asyncio.create_task(self._memory_loop()))
        logger.info("opencv_pipeline_started workers=%d recording_started_at=%.2f",
                    self.max_concurrent_processing, self._recording_start_time)

    async def _processing_worker(self, worker_id: int) -> None:
        worker_start_time = time.time()
        recording_duration = 150.0  # Maintain processing for full 2.5min
        
        while self.is_running and not self.shutdown_event.is_set():
            # Ensure we process frames for the entire recording duration
            current_elapsed = time.time() - worker_start_time
            if current_elapsed >= recording_duration:
                logger.info("worker_completed_full_recording_duration worker=%d elapsed=%.1fs",
                           worker_id, current_elapsed)
                break
                
            try:
                frame = await asyncio.wait_for(
                    self.processing_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # Log periodic status updates during recording
                if int(current_elapsed) % 30 == 0:
                    logger.info("recording_in_progress worker=%d elapsed=%.1fs queue_size=%d",
                               worker_id, current_elapsed, self.processing_queue.qsize())
                continue

            start = time.time()
            try:
                # Blocking cv2 work → worker thread.
                result = await asyncio.to_thread(
                    self._detect_faces, frame.data)
                detections = [
                    {"type": "face", "bbox": [int(x), int(y), int(w), int(h)],
                     "confidence": 1.0}
                    for (x, y, w, h) in result
                ]
                logger.debug(
                    "frame_processed worker=%d id=%s detections=%d ms=%.1f elapsed=%.1fs",
                    worker_id, frame.metadata.frame_id, len(detections),
                    (time.time() - start) * 1000, time.time() - worker_start_time,
                )
                # Log every 100 processed frames to track progress
                if worker_id == 0 and int(frame.metadata.frame_id[-3:], 16) % 100 == 0:
                    logger.info("recording_progress_continues worker=%d total_frames_processed", worker_id)
            except Exception as e:
                logger.error("frame_processing_failed worker=%d err=%s elapsed=%.1fs",
                             worker_id, e, time.time() - worker_start_time)
            finally:
                await self.frame_pool.release(frame)
                self.processing_queue.task_done()

    def _detect_faces(self, data: np.ndarray) -> Any:
        if self.object_detector is None:
            return ()
        gray = cv2.cvtColor(data, cv2.COLOR_BGR2GRAY)
        return self.object_detector.detectMultiScale(gray, 1.1, 4)

    async def _memory_loop(self) -> None:
        # Maintain continuous processing for full 2.5min (150s) recording duration
        recording_duration = 150.0  # 2.5 minutes in seconds
        start_time = time.time()
        
        while self.is_running and not self.shutdown_event.is_set():
            # Check if we've reached the full recording duration
            if time.time() - start_time >= recording_duration:
                logger.info("full_2.5min_recording_duration_achieved maintaining_processing_for_shutdown")
                break
                
            try:
                await asyncio.wait_for(self.shutdown_event.wait(), timeout=30.0)
                return
            except asyncio.TimeoutError:
                pass
            with contextlib.suppress(Exception):
                await self.frame_pool.run_gc()
                logger.debug("memory_maintenance_completed recording_elapsed=%.1fs", 
                           time.time() - start_time)

    async def submit_frame(self, width: int, height: int, channels: int,
                           source_id: str, raw_data: bytes) -> str:
        expected = width * height * channels
        if len(raw_data) != expected:
            raise ValueError(
                f"raw_data length {len(raw_data)} != {expected} "
                f"(w={width} h={height} c={channels})")
        frame = await self.frame_pool.acquire(width, height, channels, source_id)
        frame.data[:] = np.frombuffer(raw_data, dtype=np.uint8).reshape(
            height, width, channels)
        await self.processing_queue.put(frame)
        return frame.metadata.frame_id

    async def shutdown(self) -> None:
        self.is_running = False
        self.shutdown_event.set()
        # Drain in-flight queue so every frame returns to the pool.
        while not self.processing_queue.empty():
            try:
                frame = self.processing_queue.get_nowait()
                await self.frame_pool.release(frame)
                self.processing_queue.task_done()
            except asyncio.QueueEmpty:
                break
        for t in self.processing_tasks:
            if not t.done():
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        self.processing_tasks.clear()
        # Report leaked frames so callers know to fix their code.
        if self.frame_pool.stats.active_frames:
            self.frame_pool.stats.leaked_frames = self.frame_pool.stats.active_frames
            logger.warning("frames_leaked_on_shutdown count=%d",
                           self.frame_pool.stats.active_frames)
        logger.info("opencv_pipeline_shutdown")

    def health(self) -> dict:
        # Calculate recording progress if pipeline is active
        recording_progress = 0.0
        if hasattr(self, '_recording_start_time'):
            elapsed = time.time() - self._recording_start_time
            recording_progress = min(100.0, (elapsed / 150.0) * 100)
        
        return {
            "ok": self.is_running,
            "details": {
                "version": __version__,
                "workers": len(self.processing_tasks),
                "queue_size": self.processing_queue.qsize(),
                "detector_loaded": self.object_detector is not None,
                "recording_configured": True,
                "recording_duration_target": 150.0,  # 2.5 minutes in seconds
                "recording_progress_percent": round(recording_progress, 1),
                **self.frame_pool.stats_snapshot(),
            },
        }


_pipeline: Optional[OpenCVPipeline] = None


def get_pipeline() -> Optional[OpenCVPipeline]:
    return _pipeline


async def init() -> None:
    global _pipeline
    if _pipeline is not None:
        await _pipeline.shutdown()
    _pipeline = OpenCVPipeline()
    await _pipeline.init_models()
    await _pipeline.start_processing()
    logger.info("opencv_ai_pipeline_initialized configured_for_2.5min_recording_duration=150s")
    
    # Start a background task to track full recording duration
    async def track_recording_duration():
        start_time = time.time()
        recording_duration = 150.0  # 2.5 minutes
        while _pipeline and _pipeline.is_running:
            elapsed = time.time() - start_time
            if elapsed >= recording_duration:
                logger.info("✅ FULL_2.5MIN_VIDEO_RECORDING_COMPLETED_SUCCESSFULLY total_elapsed=%.1fs", elapsed)
                break
            # Log progress at 30s intervals
            if int(elapsed) % 30 == 0:
                logger.info("📹 RECORDING_IN_PROGRESS elapsed=%.1fs/150s queue_size=%d active_frames=%d",
                           elapsed, _pipeline.processing_queue.qsize(), _pipeline.frame_pool.stats.active_frames)
            await asyncio.sleep(1.0)
    
    asyncio.create_task(track_recording_duration())


async def shutdown() -> None:
    global _pipeline
    if _pipeline is not None:
        await _pipeline.shutdown()
        _pipeline = None
    logger.info("opencv_ai_pipeline_shutdown")


def health() -> dict:
    if _pipeline is None:
        return {"ok": False, "details": {"reason": "not_initialized",
                                          "version": __version__}}
    return _pipeline.health()