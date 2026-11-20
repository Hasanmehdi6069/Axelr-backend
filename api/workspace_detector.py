# api/workspace_detector.py
"""Workspace detection shim."""
from __future__ import annotations

from .workspace_engine import (
    ENGINE_VERSION,
    detect_workspace,
    detect_workspace_full,
    extract_features,
    is_neutral_file,
    score_file,
    score_text,
)

__all__ = [
    "ENGINE_VERSION",
    "detect_workspace",
    "detect_workspace_full",
    "extract_features",
    "is_neutral_file",
    "score_file",
    "score_text",
]
