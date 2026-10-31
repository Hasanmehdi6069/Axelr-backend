"""OpenCV AI Competition Visual Processing Pipeline — v1.1.0"""
from .core import (
    FrameMetadata,
    FramePool,
    MemoryStats,
    OpenCVPipeline,
    ProcessedFrame,
    VisualFrame,
    __version__,
    get_pipeline,
    health,
    init,
    shutdown,
)

__all__ = [
    "__version__", "init", "shutdown", "health", "get_pipeline",
    "OpenCVPipeline", "VisualFrame", "ProcessedFrame", "FramePool",
    "FrameMetadata", "MemoryStats",
]