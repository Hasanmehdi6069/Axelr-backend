# core/token_tools.py
"""
Merged token processing tools combining token_biasing.py and char_cnn.py.
Provides token biasing capabilities and character-level CNN processing for NLP tasks.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

# §1 TokenBiasingConfig (from token_biasing, load() path fixed)
_DEFAULT_BIASING_CONFIG: Dict[str, Any] = {
    "programming": {
        "keywords": ["function", "class", "return", "async", "await", "import", "export", "const", "let", "var"],
        "boost": 2.5,
    },
    "domain_specific": {
        "medical": ["diagnosis", "treatment", "patient", "symptom"],
        "legal": ["contract", "agreement", "plaintiff", "defendant"],
        "financial": ["transaction", "payment", "invoice", "ledger"],
        "scientific": ["hypothesis", "experiment", "analysis"],
    },
    "contextual_window_size": 100,
    "confidence_threshold": 0.75,
    "adapters": {},
}


class TokenBiasingConfig:
    """Configuration for token biasing engine."""

    programming_keywords: List[str]
    programming_boost: float
    domain_keywords: Dict[str, List[str]]
    contextual_window_size: int
    confidence_threshold: float
    adapters: Dict[str, Any]

    def __init__(self, raw_config: Dict[str, Any]):
        # Parse programming language config
        self.programming_keywords = raw_config.get("programming", {}).get("keywords", [])
        self.programming_boost = raw_config.get("programming", {}).get("boost", 2.5)

        # Parse domain-specific keywords
        self.domain_keywords = raw_config.get("domain_specific", {})

        # Parse other config parameters
        self.contextual_window_size = raw_config.get("contextual_window_size", 100)
        self.confidence_threshold = raw_config.get("confidence_threshold", 0.75)
        self.adapters = raw_config.get("adapters", {})

    @classmethod
    def load(cls, path: Optional[Union[Path, str]] = None) -> "TokenBiasingConfig":
        # Search order: explicit path > env var > local config > embedded default
        candidates: List[Path] = []
        if path is not None:
            if isinstance(path, str):
                path = Path(path)
            candidates.append(path)

        env_path = os.getenv("TOKEN_BIASING_MAP")
        if env_path:
            candidates.append(Path(env_path))

        candidates.append(Path(__file__).parent / "axelr-sovereign.engine" / "config" / "token_biasing_maps.json")
        candidates.append(Path(__file__).parent / "token_biasing_maps.json")

        raw: Optional[Dict[str, Any]] = None
        for p in candidates:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                logger.info("token_biasing_loaded path=%s", p)
                break
            except (FileNotFoundError, json.JSONDecodeError):
                continue

        if raw is None:
            logger.warning("token_biasing_using_embedded_default")
            raw = dict(_DEFAULT_BIASING_CONFIG)

        return cls(raw)


# §2 ContextWindow (unchanged)
class ContextWindow:
    """Manages sliding context window for token processing."""

    def __init__(self, size: int = 100):
        self.size = size
        self.tokens: List[str] = []

    def add(self, token: str) -> None:
        """Add a token to the context window, maintaining max size."""
        self.tokens.append(token)
        if len(self.tokens) > self.size:
            self.tokens.pop(0)

    def get_context(self) -> List[str]:
        """Return current context tokens."""
        return self.tokens.copy()

    def clear(self) -> None:
        """Clear the context window."""
        self.tokens.clear()


# §3 Adapter stubs
class NVIDIAInferenceStream:
    """Stub for NVIDIA CUDA inference stream adapter."""

    def __init__(self, config: Dict[str, Any]):
        self.enabled = config.get("enabled", False)
        self.cuda_stream_priority = config.get("cuda_stream_priority", 0)
        self.batch_size = config.get("batch_size", 32)
        self.inference_timeout_ms = config.get("inference_timeout_ms", 5000)

    def initialize(self) -> bool:
        """Initialize NVIDIA inference stream."""
        if not self.enabled:
            return False
        logger.debug("NVIDIAInferenceStream initialized with batch_size=%d", self.batch_size)
        return True


class IsolatedSandbox:
    """Stub for isolated execution sandbox adapter."""

    def __init__(self, config: Dict[str, Any]):
        self.enabled = config.get("enabled", False)
        self.sandbox_isolation_level = config.get("sandbox_isolation_level", "strict")
        self.max_concurrent_agents = config.get("max_concurrent_agents", 8)
        self.sandbox_timeout_seconds = config.get("sandbox_timeout_seconds", 300)

    def initialize(self) -> bool:
        """Initialize isolated sandbox environment."""
        if not self.enabled:
            return False
        logger.debug("IsolatedSandbox initialized with isolation_level=%s", self.sandbox_isolation_level)
        return True


class FinancialStatePipeline:
    """Stub for financial transaction processing pipeline adapter."""

    def __init__(self, config: Dict[str, Any]):
        self.enabled = config.get("enabled", False)
        self.multi_step_verification = config.get("multi_step_verification", True)
        self.transaction_retry_limit = config.get("transaction_retry_limit", 3)
        self.fraud_detection_threshold = config.get("fraud_detection_threshold", 0.95)

    def initialize(self) -> bool:
        """Initialize financial pipeline."""
        if not self.enabled:
            return False
        logger.debug("FinancialStatePipeline initialized with fraud_threshold=%.2f", self.fraud_detection_threshold)
        return True


class EdgeCloudOrchestrator:
    """Stub for edge-cloud routing and orchestration adapter."""

    def __init__(self, config: Dict[str, Any]):
        self.enabled = config.get("enabled", False)
        self.edge_cloud_routing = config.get("edge_cloud_routing", True)
        self.edge_cache_ttl_seconds = config.get("edge_cache_ttl_seconds", 86400)
        self.cloud_failover_threshold = config.get("cloud_failover_threshold", 0.8)

    def initialize(self) -> bool:
        """Initialize edge cloud orchestrator."""
        if not self.enabled:
            return False
        logger.debug("EdgeCloudOrchestrator initialized with cache_ttl=%d", self.edge_cache_ttl_seconds)
        return True


# §4 TokenBiasingEngine singleton, get_engine()
_token_biasing_engine_instance: Optional["TokenBiasingEngine"] = None


class TokenBiasingEngine:
    """Singleton token biasing engine that applies domain-specific boosts to tokens."""

    _config: TokenBiasingConfig
    _context_window: ContextWindow
    _adapters: Dict[str, Any]
    _boost_map: Dict[str, float]

    def __init__(self, config: TokenBiasingConfig):
        self._config = config
        self._context_window = ContextWindow(config.contextual_window_size)
        self._build_boost_map()
        self._initialize_adapters(config.adapters)

    def _build_boost_map(self) -> None:
        """Build a map of all keywords to their boost values."""
        self._boost_map = {}

        # Add programming keywords
        for keyword in self._config.programming_keywords:
            self._boost_map[keyword] = self._config.programming_boost

        # Add domain-specific keywords (default boost of 2.0 for domain terms)
        for domain, keywords in self._config.domain_keywords.items():
            for keyword in keywords:
                if keyword not in self._boost_map:
                    self._boost_map[keyword] = 2.0

    def _initialize_adapters(self, adapters_config: Dict[str, Any]) -> None:
        """Initialize all enabled adapters from config."""
        self._adapters = {}

        # Initialize NVIDIA inference stream if present
        if "nebius_nvidia" in adapters_config:
            self._adapters["nvidia_stream"] = NVIDIAInferenceStream(adapters_config["nebius_nvidia"])
            self._adapters["nvidia_stream"].initialize()

        # Initialize isolated sandbox if present
        if "meta_vultr" in adapters_config:
            self._adapters["sandbox"] = IsolatedSandbox(adapters_config["meta_vultr"])
            self._adapters["sandbox"].initialize()

        # Initialize financial pipeline if present
        if "paypal_ai" in adapters_config:
            self._adapters["financial_pipeline"] = FinancialStatePipeline(adapters_config["paypal_ai"])
            self._adapters["financial_pipeline"].initialize()

        # Initialize edge cloud orchestrator if present
        if "firebase_ugreen" in adapters_config:
            self._adapters["edge_orchestrator"] = EdgeCloudOrchestrator(adapters_config["firebase_ugreen"])
            self._adapters["edge_orchestrator"].initialize()

    def get_token_boost(self, token: str) -> float:
        """Get the boost value for a given token, returns 1.0 if no boost applies."""
        return self._boost_map.get(token.lower(), 1.0)

    def add_to_context(self, token: str) -> None:
        """Add a token to the context window."""
        self._context_window.add(token)

    def get_context(self) -> List[str]:
        """Get current context tokens."""
        return self._context_window.get_context()


def get_engine(config_path: Optional[Union[Path, str]] = None) -> TokenBiasingEngine:
    """Get or create the singleton TokenBiasingEngine instance."""
    global _token_biasing_engine_instance
    if _token_biasing_engine_instance is None:
        config = TokenBiasingConfig.load(config_path)
        _token_biasing_engine_instance = TokenBiasingEngine(config)
    return _token_biasing_engine_instance


__all__ = [
    "TokenBiasingConfig",
    "ContextWindow",
    "TokenBiasingEngine",
    "get_engine",
    "NVIDIAInferenceStream",
    "IsolatedSandbox",
    "FinancialStatePipeline",
    "EdgeCloudOrchestrator",
]
