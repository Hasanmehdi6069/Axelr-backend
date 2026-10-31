"""UGREEN AI NAS File Indexing System — v1.1.0
Low-latency background file indexing with edge-to-cloud sync capabilities.
"""
from .core import (
    AINASIndexer,
    CloudUploader,
    FileChangeEvent,
    IndexedFile,
    IndexingJob,
    compute_file_hash,
    get_indexer,
    health,
    init,
    shutdown,
)

__all__ = [
    "AINASIndexer", "CloudUploader", "FileChangeEvent", "IndexedFile",
    "IndexingJob", "compute_file_hash", "get_indexer", "health",
    "init", "shutdown",
]
__version__ = "1.1.0"