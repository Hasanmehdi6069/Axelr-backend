"""AI Genesis 2026 Inference Model Abstraction Layer — v1.1.0
==========================================================
Model registry with memory-pressure-driven eviction.

Honest scope
------------
* **`torch` is lazy-imported** — the module imports cleanly on a machine
  with no PyTorch. CUDA detection happens only when `MemoryProfile.capture()`
  is called from an async context via `to_thread`.
* **`CustomLlamaModel` removed** — its `load()` returned `True` and
  `predict()` returned a hardcoded dict. That was fake work. Replaced with
  `NullModel` (raises `NotImplementedError` for every operation) as a
  test fixture, and a base `BaseInferenceModel` that real implementations
  subclass and genuinely implement.
* **`MemoryProfile.capture()` is blocking** (calls `psutil` and `torch`).
  Callers should `await asyncio.to_thread(MemoryProfile.capture)`.
"""
from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

import psutil

logger = logging.getLogger("axelr.ai_genesis.core")
__version__ = "1.1.0"


def _torch_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


@dataclass(slots=True)
class MemoryProfile:
    total_system_memory_mb: float
    available_memory_mb: float
    gpu_memory_mb: float = 0.0
    gpu_available_mb: float = 0.0
    memory_pressure: float = 0.0
    timestamp: float = field(default_factory=time.time)

    @classmethod
    def capture(cls) -> "MemoryProfile":
        """Synchronous — call via `await asyncio.to_thread(MemoryProfile.capture)`
        from async contexts to avoid blocking the event loop."""
        mem = psutil.virtual_memory()
        total = mem.total / (1024 * 1024)
        available = mem.available / (1024 * 1024)
        pressure = 1.0 - (available / total) if total else 0.0

        gpu_total = 0.0
        gpu_avail = 0.0
        if _torch_available():
            try:
                import torch
                for i in range(torch.cuda.device_count()):
                    props = torch.cuda.get_device_properties(i)
                    gpu_total += props.total_memory / (1024 * 1024)
                    alloc = torch.cuda.memory_allocated(i) / (1024 * 1024)
                    gpu_avail += (props.total_memory / (1024 * 1024)) - alloc
            except Exception as e:
                logger.debug("gpu_memory_query_failed err=%s", e)

        return cls(
            total_system_memory_mb=total,
            available_memory_mb=available,
            gpu_memory_mb=gpu_total,
            gpu_available_mb=gpu_avail,
            memory_pressure=pressure,
        )


class BaseInferenceModel(ABC):
    """Subclass and implement load/unload/predict for a real backend.

    Concrete implementations must not fake success — if the backend isn't
    ready, raise with a clear message.
    """
    model_id: str = ""
    model_name: str = ""
    memory_required_mb: float = 0.0
    gpu_memory_required_mb: float = 0.0
    is_loaded: bool = False
    last_used: float = 0.0
    load_time: float = 0.0
    inference_count: int = 0

    @abstractmethod
    async def load(self) -> bool: ...

    @abstractmethod
    async def unload(self) -> None: ...

    @abstractmethod
    async def predict(self, inputs: Dict[str, Any]) -> Dict[str, Any]: ...

    @property
    def idle_time(self) -> float:
        return time.time() - self.last_used


class NullModel(BaseInferenceModel):
    """Test fixture — always raises. Use to exercise registry lifecycle
    without a real model."""

    def __init__(self, model_id: str = "null", required_mb: float = 100.0):
        self.model_id = model_id
        self.model_name = f"NullModel({model_id})"
        self.memory_required_mb = required_mb

    async def load(self) -> bool:
        raise NotImplementedError("requires backend: real model loader")

    async def unload(self) -> None:
        self.is_loaded = False

    async def predict(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("requires backend: real model inference")


@dataclass(slots=True)
class ModelRegistry:
    models: Dict[str, BaseInferenceModel] = field(default_factory=dict)
    loaded_models: Set[str] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    profiling_task: Optional[asyncio.Task] = None
    shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    memory_pressure_threshold: float = 0.85

    async def register_model(self, model: BaseInferenceModel) -> None:
        async with self.lock:
            self.models[model.model_id] = model
        logger.info("model_registered id=%s memory_mb=%.1f",
                    model.model_id, model.memory_required_mb)

    async def get_model(self, model_id: str) -> Optional[BaseInferenceModel]:
        async with self.lock:
            model = self.models.get(model_id)
        if model is None:
            return None
        if not model.is_loaded:
            profile = await asyncio.to_thread(MemoryProfile.capture)
            if profile.available_memory_mb < model.memory_required_mb:
                await self._evict_idle_models(
                    model.memory_required_mb - profile.available_memory_mb)
            success = await model.load()
            if success:
                async with self.lock:
                    self.loaded_models.add(model_id)
                model.is_loaded = True
                model.load_time = time.time()
        model.last_used = time.time()
        model.inference_count += 1
        return model

    async def _evict_idle_models(self, required_mb: float) -> int:
        async with self.lock:
            candidates = [(m_id, self.models[m_id])
                          for m_id in self.loaded_models]
        candidates.sort(key=lambda kv: kv[1].idle_time, reverse=True)
        freed = 0.0
        evicted = 0
        for model_id, model in candidates:
            if freed >= required_mb:
                break
            try:
                await model.unload()
                model.is_loaded = False
                async with self.lock:
                    self.loaded_models.discard(model_id)
                freed += model.memory_required_mb
                evicted += 1
                logger.warning("evicted_model id=%s freed_mb=%.1f",
                               model_id, model.memory_required_mb)
            except Exception as e:
                logger.error("evict_failed id=%s err=%s", model_id, e)
        if evicted:
            await asyncio.to_thread(gc.collect)
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
        return evicted

    async def _profiling_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self.shutdown_event.wait(), timeout=30.0)
                return
            except asyncio.TimeoutError:
                pass
            try:
                profile = await asyncio.to_thread(MemoryProfile.capture)
                if profile.memory_pressure > self.memory_pressure_threshold:
                    target = 0.7
                    need_mb = ((profile.memory_pressure - target)
                               * profile.total_system_memory_mb)
                    logger.warning("memory_pressure pressure=%.2f need_mb=%.0f",
                                   profile.memory_pressure, need_mb)
                    await self._evict_idle_models(need_mb)
            except Exception as e:
                logger.error("profiling_loop_error err=%s", e)

    async def shutdown(self) -> None:
        self.shutdown_event.set()
        if self.profiling_task is not None and not self.profiling_task.done():
            self.profiling_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.profiling_task
        self.profiling_task = None
        # Unload every loaded model — releases the RAM.
        async with self.lock:
            loaded_ids = list(self.loaded_models)
        for mid in loaded_ids:
            model = self.models.get(mid)
            if model is not None:
                with contextlib.suppress(Exception):
                    await model.unload()
                model.is_loaded = False
        async with self.lock:
            self.loaded_models.clear()
            self.models.clear()


_registry: Optional[ModelRegistry] = None


def get_registry() -> ModelRegistry:
    global _registry
    if _registry is None:
        _registry = ModelRegistry()
    return _registry


async def init() -> None:
    registry = get_registry()
    if registry.profiling_task is None or registry.profiling_task.done():
        registry.shutdown_event.clear()
        registry.profiling_task = asyncio.create_task(
            registry._profiling_loop())
    logger.info("ai_genesis_model_layer_initialized")


async def shutdown() -> None:
    global _registry
    if _registry is not None:
        await _registry.shutdown()
        _registry = None
    logger.info("ai_genesis_model_layer_shutdown")


def health() -> Dict[str, Any]:
    if _registry is None:
        return {"ok": False, "details": {"reason": "not_initialized",
                                         "version": __version__}}
    return {
        "ok": True,
        "details": {
            "version": __version__,
            "registered_models": list(_registry.models),
            "loaded_models": list(_registry.loaded_models),
            "torch_available": _torch_available(),
        },
    }