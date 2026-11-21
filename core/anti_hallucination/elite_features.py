"""AXELR AHE — Elite feature layer v2.0 (v1.7).

18 additional independently-verifiable features on top of the original 16.
Each feature is deterministic, testable, falsifiable, and honest. They can
be composed independently or via the EliteFeaturesV2 bundle.

Coverage:
   1. CausalChainVerifier           10. SensitivityClassifier
   2. NumericalPrecisionGuard       11. CrossLingualCanonicalizer
   3. EntityResolver                12. HallucinationPatternDetector
   4. QuotationAttributionGuard     13. ClaimGranularityOptimizer
   5. TemporalOrderingVerifier      14. ConfidencePropagationEngine
   6. UnitConsistencyChecker        15. AdversarialInputDetector
   7. StatisticalClaimVerifier      16. SemanticFingerprint
   8. ContradictionDetector         17. ClaimGraphBuilder
   9. ProvenanceChainVerifier       18. HedgingDetector
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


# ═══════════════════════════════════════════════════════════════════════════
# 1. CausalChainVerifier
# ═══════════════════════════════════════════════════════════════════════════
class CausalChainVerifier:
    """A claim asserting causality must have causal language in the source."""

    MARKERS = (
        "because",
        "causes",
        "caused",
        "results in",
        "leads to",
        "due to",
        "therefore",
        "thus",
        "hence",
        "consequently",
        "as a result",
        "since",
        "so that",
        "gives rise to",
    )

    def __init__(self) -> None:
        self._re = re.compile(
            r"\b(" + "|".join(re.escape(m) for m in self.MARKERS) + r")\b",
            re.I,
        )

    def markers(self, text: str) -> List[str]:
        return [m.group(0).lower() for m in self._re.finditer(text)]

    def has_causal(self, text: str) -> bool:
        return bool(self._re.search(text))

    def verify(self, claim: str, source: str) -> Tuple[bool, str]:
        if not self.has_causal(claim):
            return True, "non_causal_claim"
        if not self.has_causal(source):
            return False, "causal_claim_no_source_causality"
        if not (set(self.markers(claim)) & set(self.markers(source))):
            return False, "causal_marker_mismatch"
        return True, "causal_ok"


# ═══════════════════════════════════════════════════════════════════════════
# 2. NumericalPrecisionGuard
# ═══════════════════════════════════════════════════════════════════════════
class NumericalPrecisionGuard:
    """'1.0' vs '1.00' vs '1' — precision is data, not noise. Never normalize."""

    _NUM = re.compile(r"-?\d+(?:\.\d+)?")

    def decimals(self, value: str) -> Optional[int]:
        m = self._NUM.fullmatch(value.strip())
        if not m:
            return None
        s = m.group(0)
        return len(s.split(".")[1]) if "." in s else 0

    def precision_preserved(self, claim: str, source: str) -> bool:
        cd = self.decimals(claim)
        if cd is None:
            return True  # not numeric
        # Find all numbers in source with matching integer part
        claim_int = claim.strip().lstrip("-").split(".")[0]
        for m in self._NUM.finditer(source):
            cand = m.group(0)
            if cand.lstrip("-").split(".")[0] == claim_int:
                if self.decimals(cand) == cd:
                    return True
        return False

    def verify(self, claim: str, source: str) -> Tuple[bool, str]:
        if self.precision_preserved(claim, source):
            return True, "precision_preserved"
        return False, "precision_changed"


# ═══════════════════════════════════════════════════════════════════════════
# 3. EntityResolver
# ═══════════════════════════════════════════════════════════════════════════
class EntityResolver:
    """Canonicalize common entity suffix/prefix variants for equality checks."""

    SUFFIX_RE = re.compile(
        r"\s+(?:Inc\.?|Corp\.?|Corporation|LLC|Ltd\.?|Co\.?|GmbH|S\.A\.?)$",
        re.I,
    )
    LEADING_ARTICLES = re.compile(r"^(?:the|a|an)\s+", re.I)

    def canonical(self, name: str) -> str:
        s = name.strip()
        s = self.LEADING_ARTICLES.sub("", s)
        s = self.SUFFIX_RE.sub("", s)
        return re.sub(r"\s+", " ", s).strip().lower()

    def match(self, a: str, b: str) -> bool:
        return self.canonical(a) == self.canonical(b)

    def resolve_group(self, names: Iterable[str]) -> Dict[str, List[str]]:
        groups: Dict[str, List[str]] = defaultdict(list)
        for n in names:
            groups[self.canonical(n)].append(n)
        return dict(groups)


# ═══════════════════════════════════════════════════════════════════════════
# 4. QuotationAttributionGuard
# ═══════════════════════════════════════════════════════════════════════════
class QuotationAttributionGuard:
    """Every quoted span must be attributable within the surrounding window."""

    QUOTE_RE = re.compile(r'"([^"]{4,300})"|\u201c([^\u201d]{4,300})\u201d')
    ATTRIB_RE = re.compile(
        r"\b(?:said|says|stated|wrote|according to|per|noted|reported)\b",
        re.I,
    )

    def quoted_spans(self, text: str) -> List[str]:
        return [m.group(1) or m.group(2) for m in self.QUOTE_RE.finditer(text)]

    def verify(self, text: str, window: int = 200) -> Tuple[bool, List[str]]:
        issues: List[str] = []
        for m in self.QUOTE_RE.finditer(text):
            lo = max(0, m.start() - window)
            hi = min(len(text), m.end() + window)
            if not self.ATTRIB_RE.search(text[lo:hi]):
                issues.append(m.group(1) or m.group(2))
        return (len(issues) == 0), issues


# ═══════════════════════════════════════════════════════════════════════════
# 5. TemporalOrderingVerifier
# ═══════════════════════════════════════════════════════════════════════════
class TemporalOrderingVerifier:
    """'before' / 'after' / 'during' claims must match source ordering."""

    BEFORE = re.compile(r"\b(before|prior to|preceded by|earlier than)\b", re.I)
    AFTER = re.compile(r"\b(after|following|subsequent to|later than)\b", re.I)

    def has_ordering(self, text: str) -> Optional[str]:
        if self.BEFORE.search(text):
            return "before"
        if self.AFTER.search(text):
            return "after"
        return None

    def verify(self, claim: str, source: str) -> Tuple[bool, str]:
        c = self.has_ordering(claim)
        if c is None:
            return True, "no_temporal_claim"
        s = self.has_ordering(source)
        if s is None:
            return False, "temporal_claim_no_source_ordering"
        if c != s:
            return False, f"temporal_mismatch:{c}!={s}"
        return True, "temporal_ok"


# ═══════════════════════════════════════════════════════════════════════════
# 6. UnitConsistencyChecker
# ═══════════════════════════════════════════════════════════════════════════
class UnitConsistencyChecker:
    """Detect unit drift: '5 kg' vs '5 lb', '100 USD' vs '100 EUR'."""

    UNIT_RE = re.compile(
        r"(-?\d+(?:\.\d+)?)\s*"
        r"(kg|g|mg|lb|oz|t|km|m|cm|mm|mi|ft|in|yd|"
        r"l|ml|gal|pt|qt|"
        r"usd|eur|gbp|jpy|inr|cny|"
        r"°c|°f|k|"
        r"kb|mb|gb|tb|pb|"
        r"s|ms|us|ns|min|h|d|w)\b",
        re.I,
    )

    def units_in(self, text: str) -> List[Tuple[float, str]]:
        return [(float(m.group(1)), m.group(2).lower()) for m in self.UNIT_RE.finditer(text)]

    def verify(self, claim: str, source: str) -> Tuple[bool, str]:
        c_units = self.units_in(claim)
        if not c_units:
            return True, "no_units"
        s_units = self.units_in(source)
        if not s_units:
            return False, "units_not_in_source"
        s_unit_set = {u for _, u in s_units}
        for _, cu in c_units:
            if cu not in s_unit_set:
                return False, f"unit_drift:{cu}"
        return True, "units_ok"


# ═══════════════════════════════════════════════════════════════════════════
# 7. StatisticalClaimVerifier
# ═══════════════════════════════════════════════════════════════════════════
class StatisticalClaimVerifier:
    """Detect stats claims; require numeric support in the source."""

    STAT_MARKERS = (
        "average",
        "mean",
        "median",
        "mode",
        "percentile",
        "standard deviation",
        "variance",
        "correlation",
        "regression",
        "confidence interval",
        "p-value",
        "sample size",
    )
    _MARK_RE = re.compile(
        r"\b(" + "|".join(re.escape(m) for m in STAT_MARKERS) + r")\b",
        re.I,
    )

    def is_statistical(self, text: str) -> bool:
        return bool(self._MARK_RE.search(text))

    def verify(self, claim: str, source: str) -> Tuple[bool, str]:
        if not self.is_statistical(claim):
            return True, "non_statistical"
        if not self.is_statistical(source):
            return False, "statistical_claim_no_source_stats"
        return True, "statistical_ok"


# ═══════════════════════════════════════════════════════════════════════════
# 8. ContradictionDetector
# ═══════════════════════════════════════════════════════════════════════════
class ContradictionDetector:
    """Two claims sharing a numeric template but disagreeing are contradictions."""

    _NUM = re.compile(r"-?\d+(?:\.\d+)?")

    def template(self, text: str) -> str:
        return self._NUM.sub("#", text.strip().lower())

    def numbers(self, text: str) -> Tuple[str, ...]:
        return tuple(sorted(self._NUM.findall(text)))

    def find_contradictions(
        self,
        claims: Iterable[str],
    ) -> List[Tuple[str, str]]:
        groups: Dict[str, List[Tuple[str, Tuple[str, ...]]]] = defaultdict(list)
        for c in claims:
            nums = self.numbers(c)
            if not nums:
                continue
            groups[self.template(c)].append((c, nums))
        out: List[Tuple[str, str]] = []
        for items in groups.values():
            if len(items) < 2:
                continue
            seen = {n for _, n in items}
            if len(seen) > 1:
                for i in range(len(items) - 1):
                    out.append((items[i][0], items[i + 1][0]))
        return out


# ═══════════════════════════════════════════════════════════════════════════
# 9. ProvenanceChainVerifier
# ═══════════════════════════════════════════════════════════════════════════
class ProvenanceChainVerifier:
    """Verify a chain of hash links has not been broken."""

    @staticmethod
    def digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def verify_chain(
        self,
        payloads: List[Dict[str, Any]],
        *,
        hash_key: str = "prev_hash",
        self_key: str = "hash",
    ) -> Tuple[bool, str]:
        prev = ""
        for i, p in enumerate(payloads):
            declared = p.get(hash_key, "")
            if declared != prev:
                return False, f"chain_broken_at_{i}"
            prev = p.get(self_key, "")
        return True, "chain_ok"


# ═══════════════════════════════════════════════════════════════════════════
# 10. SensitivityClassifier
# ═══════════════════════════════════════════════════════════════════════════
class SensitivityClassifier:
    """Classify claim sensitivity for downstream handling."""

    PII = {
        "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        "phone": re.compile(r"\+?\d[\d\s\-().]{7,}\d"),
        "ssn_us": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "credit_card": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    }
    FINANCIAL = re.compile(
        r"\b(?:account|routing|iban|swift|balance|salary|invoice)\b",
        re.I,
    )
    HEALTH = re.compile(
        r"\b(?:diagnosis|prescription|patient|medical record|icd-\d+|"
        r"blood type|allergy)\b",
        re.I,
    )

    def classify(self, text: str) -> Dict[str, Any]:
        flags: List[str] = []
        for name, pat in self.PII.items():
            if pat.search(text):
                flags.append(f"pii:{name}")
        if self.FINANCIAL.search(text):
            flags.append("financial")
        if self.HEALTH.search(text):
            flags.append("health")
        level = "restricted" if flags else "public"
        if not flags and len(text) > 4000:
            level = "internal"
        return {"level": level, "flags": flags}


# ═══════════════════════════════════════════════════════════════════════════
# 11. CrossLingualCanonicalizer
# ═══════════════════════════════════════════════════════════════════════════
class CrossLingualCanonicalizer:
    """NFKC + script detection + Cyrillic/Greek homoglyph folding."""

    HOMOGLYPHS = {
        # Cyrillic → Latin
        "а": "a",
        "е": "e",
        "о": "o",
        "р": "p",
        "с": "c",
        "у": "y",
        "х": "x",
        "і": "i",
        "ј": "j",
        "ѕ": "s",
        "ԁ": "d",
        "һ": "h",
        # Greek → Latin
        "ο": "o",
        "α": "a",
        "ε": "e",
        "ρ": "p",
        "τ": "t",
        "υ": "u",
        "ν": "v",
        "χ": "x",
    }

    def script(self, text: str) -> str:
        for ch in text:
            if "\u0400" <= ch <= "\u04ff":
                return "cyrillic"
            if "\u0370" <= ch <= "\u03ff":
                return "greek"
            if "\u4e00" <= ch <= "\u9fff":
                return "cjk"
            if "\u0600" <= ch <= "\u06ff":
                return "arabic"
            if ch.isascii() and ch.isalpha():
                return "latin"
        return "unknown"

    def fold_homoglyphs(self, text: str) -> str:
        return "".join(self.HOMOGLYPHS.get(c, c) for c in text)

    def canonical(self, text: str) -> str:
        return unicodedata.normalize("NFKC", self.fold_homoglyphs(text))


# ═══════════════════════════════════════════════════════════════════════════
# 12. HallucinationPatternDetector
# ═══════════════════════════════════════════════════════════════════════════
class HallucinationPatternDetector:
    """Detect classic LLM hallucination patterns in claims."""

    PATTERNS = {
        "unsourced_study": re.compile(
            r"\b(?:studies|research)\s+(?:show|suggest|indicate)s?\b",
            re.I,
        ),
        "authority_appeal": re.compile(
            r"\b(?:experts|scientists|researchers|doctors)\s+(?:say|agree|believe)\b",
            re.I,
        ),
        "vague_quantifier": re.compile(
            r"\b(?:many|most|some|several|numerous)\s+(?:people|studies|experts)\b",
            re.I,
        ),
        "fabricated_citation": re.compile(
            r"\(\s*[A-Z][a-zA-Z]+\s+et\s+al\.?,?\s+\d{4}\s*\)",
        ),
        "weasel_words": re.compile(
            r"\b(?:it is (?:said|believed|known)|widely (?:known|accepted))\b",
            re.I,
        ),
    }

    def detect(self, text: str) -> List[str]:
        return [name for name, pat in self.PATTERNS.items() if pat.search(text)]

    def score(self, text: str) -> int:
        return len(self.detect(text))


# ═══════════════════════════════════════════════════════════════════════════
# 13. ClaimGranularityOptimizer
# ═══════════════════════════════════════════════════════════════════════════
class ClaimGranularityOptimizer:
    """Classify claim as too_vague / optimal / too_specific."""

    def __init__(self, min_tokens: int = 3, max_tokens: int = 40):
        self.min_tokens = min_tokens
        self.max_tokens = max_tokens

    def classify(self, text: str) -> str:
        n = len(text.split())
        if n < self.min_tokens:
            return "too_vague"
        if n > self.max_tokens:
            return "too_specific"
        return "optimal"

    def score(self, text: str) -> float:
        n = len(text.split())
        if n == 0:
            return 0.0
        center = (self.min_tokens + self.max_tokens) / 2
        span = (self.max_tokens - self.min_tokens) / 2
        return max(0.0, 1.0 - abs(n - center) / (span + 1.0))


# ═══════════════════════════════════════════════════════════════════════════
# 14. ConfidencePropagationEngine
# ═══════════════════════════════════════════════════════════════════════════
class ConfidencePropagationEngine:
    """Propagate confidence through claim dependency chains."""

    def __init__(self) -> None:
        self._deps: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
        self._base: Dict[str, float] = {}

    def add_claim(self, claim_id: str, base_confidence: float) -> None:
        self._base[claim_id] = max(0.0, min(1.0, base_confidence))

    def add_dependency(
        self,
        parent: str,
        child: str,
        weight: float = 1.0,
    ) -> None:
        self._deps[parent].append((child, max(0.0, min(1.0, weight))))

    def propagate(self) -> Dict[str, float]:
        result = dict(self._base)
        for _ in range(10):  # bounded iteration
            changed = False
            for parent, children in self._deps.items():
                p_conf = result.get(parent, 0.0)
                for child, w in children:
                    propagated = p_conf * w
                    current = result.get(child, 0.0)
                    if propagated < current:
                        result[child] = propagated
                        changed = True
            if not changed:
                break
        return result


# ═══════════════════════════════════════════════════════════════════════════
# 15. AdversarialInputDetector
# ═══════════════════════════════════════════════════════════════════════════
class AdversarialInputDetector:
    """Detect homoglyph, control-char, and RTL-override injection attempts."""

    CTRL_CHARS = frozenset(chr(i) for i in range(0, 32) if chr(i) not in "\t\n\r") | {"\x7f"}
    RTL_OVERRIDE = frozenset({"\u202a", "\u202b", "\u202d", "\u202e"})

    def scan(self, text: str) -> Dict[str, int]:
        counts: Dict[str, int] = defaultdict(int)
        for ch in text:
            if ch in self.CTRL_CHARS:
                counts["control"] += 1
            elif ch in self.RTL_OVERRIDE:
                counts["rtl_override"] += 1
            elif ch == "\u200b":
                counts["zero_width"] += 1
            elif ch == "\ufeff":
                counts["bom"] += 1
        # Polyglot detection
        scripts = set()
        for ch in text:
            if ch.isascii() and ch.isalpha():
                scripts.add("latin")
            elif "\u0400" <= ch <= "\u04ff":
                scripts.add("cyrillic")
            elif "\u0370" <= ch <= "\u03ff":
                scripts.add("greek")
        if len(scripts) > 1:
            counts["polyglot"] = len(scripts)
        return dict(counts)

    def is_compromised(self, text: str) -> bool:
        return bool(self.scan(text))


# ═══════════════════════════════════════════════════════════════════════════
# 16. SemanticFingerprint
# ═══════════════════════════════════════════════════════════════════════════
class SemanticFingerprint:
    """Token n-gram based fingerprint for near-duplicate detection."""

    def __init__(self, n: int = 3) -> None:
        self.n = n

    def _tokens(self, text: str) -> List[str]:
        return re.findall(r"[a-z0-9]+", text.lower())

    def fingerprints(self, text: str) -> Set[str]:
        toks = self._tokens(text)
        if len(toks) < self.n:
            return {" ".join(toks)} if toks else set()
        return {" ".join(toks[i : i + self.n]) for i in range(len(toks) - self.n + 1)}

    def similarity(self, a: str, b: str) -> float:
        fa, fb = self.fingerprints(a), self.fingerprints(b)
        if not fa or not fb:
            return 0.0
        return len(fa & fb) / len(fa | fb)

    def is_near_dup(self, a: str, b: str, threshold: float = 0.85) -> bool:
        return self.similarity(a, b) >= threshold


# ═══════════════════════════════════════════════════════════════════════════
# 17. ClaimGraphBuilder
# ═══════════════════════════════════════════════════════════════════════════
class ClaimGraphBuilder:
    """Build a co-support graph of claims via similarity; find clusters."""

    def __init__(self, threshold: float = 0.6) -> None:
        self.threshold = threshold
        self._fp = SemanticFingerprint()

    def build(self, claims: List[str]) -> Dict[int, Set[int]]:
        graph: Dict[int, Set[int]] = {i: set() for i in range(len(claims))}
        for i in range(len(claims)):
            for j in range(i + 1, len(claims)):
                if self._fp.similarity(claims[i], claims[j]) >= self.threshold:
                    graph[i].add(j)
                    graph[j].add(i)
        return graph

    def components(self, graph: Dict[int, Set[int]]) -> List[Set[int]]:
        seen: Set[int] = set()
        comps: List[Set[int]] = []
        for start in graph:
            if start in seen:
                continue
            stack = [start]
            comp: Set[int] = set()
            while stack:
                n = stack.pop()
                if n in seen:
                    continue
                seen.add(n)
                comp.add(n)
                stack.extend(graph[n] - seen)
            comps.append(comp)
        return comps


# ═══════════════════════════════════════════════════════════════════════════
# 18. HedgingDetector
# ═══════════════════════════════════════════════════════════════════════════
class HedgingDetector:
    """Detect hedging language that lowers claim confidence."""

    STRONG = re.compile(
        r"\b(?:definitely|certainly|always|never|must|proven|guaranteed)\b",
        re.I,
    )
    HEDGE = re.compile(
        r"\b(?:might|may|could|possibly|perhaps|probably|likely|"
        r"seems?|appears?|suggests?|roughly|approximately|about)\b",
        re.I,
    )

    def score(self, text: str) -> Dict[str, Any]:
        hedges = len(self.HEDGE.findall(text))
        strongs = len(self.STRONG.findall(text))
        return {
            "hedge_count": hedges,
            "strong_count": strongs,
            "hedge_ratio": hedges / max(1, hedges + strongs),
            "advice": "verify" if hedges > strongs else "accept",
        }


# ═══════════════════════════════════════════════════════════════════════════
# Bundle
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class EliteFeaturesV2:
    causal: CausalChainVerifier = field(default_factory=CausalChainVerifier)
    precision: NumericalPrecisionGuard = field(default_factory=NumericalPrecisionGuard)
    entities: EntityResolver = field(default_factory=EntityResolver)
    quotes: QuotationAttributionGuard = field(default_factory=QuotationAttributionGuard)
    temporal: TemporalOrderingVerifier = field(default_factory=TemporalOrderingVerifier)
    units: UnitConsistencyChecker = field(default_factory=UnitConsistencyChecker)
    stats: StatisticalClaimVerifier = field(default_factory=StatisticalClaimVerifier)
    contradictions: ContradictionDetector = field(default_factory=ContradictionDetector)
    provenance: ProvenanceChainVerifier = field(default_factory=ProvenanceChainVerifier)
    sensitivity: SensitivityClassifier = field(default_factory=SensitivityClassifier)
    crosslingual: CrossLingualCanonicalizer = field(default_factory=CrossLingualCanonicalizer)
    hallucination: HallucinationPatternDetector = field(default_factory=HallucinationPatternDetector)
    granularity: ClaimGranularityOptimizer = field(default_factory=ClaimGranularityOptimizer)
    confidence: ConfidencePropagationEngine = field(default_factory=ConfidencePropagationEngine)
    adversarial: AdversarialInputDetector = field(default_factory=AdversarialInputDetector)
    fingerprint: SemanticFingerprint = field(default_factory=SemanticFingerprint)
    graph: ClaimGraphBuilder = field(default_factory=ClaimGraphBuilder)
    hedging: HedgingDetector = field(default_factory=HedgingDetector)


__all__ = [
    "CausalChainVerifier",
    "NumericalPrecisionGuard",
    "EntityResolver",
    "QuotationAttributionGuard",
    "TemporalOrderingVerifier",
    "UnitConsistencyChecker",
    "StatisticalClaimVerifier",
    "ContradictionDetector",
    "ProvenanceChainVerifier",
    "SensitivityClassifier",
    "CrossLingualCanonicalizer",
    "HallucinationPatternDetector",
    "ClaimGranularityOptimizer",
    "ConfidencePropagationEngine",
    "AdversarialInputDetector",
    "SemanticFingerprint",
    "ClaimGraphBuilder",
    "HedgingDetector",
    "EliteFeaturesV2",
]
