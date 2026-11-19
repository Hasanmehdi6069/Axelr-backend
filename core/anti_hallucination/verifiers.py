"""AXELR AHE — domain verifiers: research + design.
Each verifier declares catches / does_not_catch. Registered via engine.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from .primitives import balance_tags, pda_balanced

# ═══════════════════════════════════════════════════════════════════
# RESEARCH
# ═══════════════════════════════════════════════════════════════════
_CITATION_INLINE = re.compile(r"\[(\d+|[A-Z][A-Za-z]+(?:\s+et\s+al\.?)?\s*\d{4})\]")
_CITATION_MARK = re.compile(r"\(([^)]+,\s*\d{4})\)")
_NUMERIC = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:%|percent|x|ms|s|kb|mb|gb|tb)?\b", re.I)
_VAGUE_OPENERS = re.compile(
    r"^(?:studies show|research suggests|experts say|many believe|"
    r"it is (?:known|clear|widely accepted)|some say|generally|"
    r"in general|as we all know)\b", re.I)
_QUOTED = re.compile(r'"([^"]{8,200})"|\u201c([^\u201d]{8,200})\u201d')


@dataclass(frozen=True, slots=True)
class ResearchSource:
    id: str
    url: str = ""
    title: str = ""
    author: str = ""
    published_ts: float = 0.0
    authority: float = 0.5

    def age_days(self) -> float:
        if not self.published_ts:
            return 0.0
        return max(0.0, (datetime.now(timezone.utc).timestamp() - self.published_ts) / 86400.0)


@dataclass(slots=True)
class ResearchClaim:
    text: str
    cited_ids: list[str] = field(default_factory=list)
    is_numeric: bool = False
    stake: str = "low"


@dataclass(frozen=True, slots=True)
class ResearchDefect:
    code: str
    severity: str
    message: str
    claim_idx: int = -1

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity,
                "message": self.message, "claim_idx": self.claim_idx}


@dataclass(slots=True)
class ResearchVerdict:
    passed: bool
    defects: list[ResearchDefect] = field(default_factory=list)
    checks_run: int = 0

    def to_dict(self) -> dict:
        return {"passed": self.passed,
                "defects": [d.to_dict() for d in self.defects],
                "checks_run": self.checks_run,
                "errors": sum(1 for d in self.defects if d.severity == "error"),
                "warnings": sum(1 for d in self.defects if d.severity == "warning")}


def extract_citations(text: str) -> list[str]:
    out = [m.group(1) for m in _CITATION_INLINE.finditer(text)]
    out.extend(m.group(1).replace(" ", "") for m in _CITATION_MARK.finditer(text))
    return out


def is_vague(text: str) -> bool:
    return bool(_VAGUE_OPENERS.match(text.strip()))


def is_specific(text: str) -> bool:
    if _NUMERIC.search(text):
        return True
    if re.search(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b", text):
        return True
    if re.search(r"\b[A-Z]{2,}\b", text):
        return True
    return False


def verify_research(claims: Iterable[ResearchClaim],
                    sources: dict[str, ResearchSource],
                    *, authority_map: dict[str, float] | None = None,
                    min_authority: float = 0.4,
                    max_age_days: float = 730.0,
                    require_corroboration_for: str = "high",
                    check_quotes: bool = True,
                    check_consensus: bool = True) -> ResearchVerdict:
    v = ResearchVerdict(passed=True)
    authority_map = authority_map or {}
    claims = list(claims)
    seen_numeric: dict[str, list[float]] = {}

    for i, claim in enumerate(claims):
        text = claim.text or ""

        v.checks_run += 1
        cited = claim.cited_ids or extract_citations(text)
        if not cited:
            v.defects.append(ResearchDefect(
                code="uncited_claim", severity="error",
                message=f"Factual claim without citation: {text[:80]!r}",
                claim_idx=i))
            continue

        v.checks_run += 1
        for cid in cited:
            if cid not in sources:
                v.defects.append(ResearchDefect(
                    code="citation_not_found", severity="error",
                    message=f"Citation [{cid}] does not resolve to a source",
                    claim_idx=i))

        v.checks_run += 1
        source_authorities = []
        for cid in cited:
            src = sources.get(cid)
            if src is None:
                continue
            a = authority_map.get(src.url, authority_map.get(cid, src.authority))
            source_authorities.append(a)
        if source_authorities and max(source_authorities) < min_authority:
            v.defects.append(ResearchDefect(
                code="low_authority_source", severity="error",
                message=f"Best authority {max(source_authorities):.2f} < "
                        f"required {min_authority:.2f}",
                claim_idx=i))

        v.checks_run += 1
        if is_vague(text) and not is_specific(text):
            v.defects.append(ResearchDefect(
                code="vague_claim", severity="error",
                message=f"Vague claim with no specificity: {text[:80]!r}",
                claim_idx=i))

        v.checks_run += 1
        for cid in cited:
            src = sources.get(cid)
            if src is not None and src.published_ts:
                age = src.age_days()
                if age > max_age_days:
                    v.defects.append(ResearchDefect(
                        code="stale_source", severity="warning",
                        message=f"Source [{cid}] is {age:.0f} days old "
                                f"(max {max_age_days:.0f})",
                        claim_idx=i))
                    break

        v.checks_run += 1
        if claim.stake == require_corroboration_for and claim.is_numeric:
            distinct_sources = {cid for cid in cited if cid in sources}
            if len(distinct_sources) < 2:
                v.defects.append(ResearchDefect(
                    code="insufficient_corroboration", severity="error",
                    message=f"High-stakes numeric claim cites "
                            f"{len(distinct_sources)} source(s); ≥2 required",
                    claim_idx=i))

        if check_quotes:
            v.checks_run += 1
            for frag in [m.group(1) or m.group(2) for m in _QUOTED.finditer(text)]:
                frag_norm = re.sub(r"\s+", " ", frag.strip().lower())
                if not frag_norm:
                    continue
                hit = False
                for cid in cited:
                    src = sources.get(cid)
                    if src is None:
                        continue
                    for val in (src.title, src.author, src.url):
                        if frag_norm in val.lower():
                            hit = True; break
                    if hit:
                        break
                if not hit:
                    v.defects.append(ResearchDefect(
                        code="unverifiable_quote", severity="warning",
                        message=f"Quoted fragment not found in cited source "
                                f"metadata: {frag[:60]!r}",
                        claim_idx=i))

        if check_consensus and claim.is_numeric:
            v.checks_run += 1
            nums = [float(x.replace(",", "")) for x in _NUMERIC.findall(text)
                    if any(c.isdigit() for c in x)]
            if nums:
                topic = re.sub(r"[^a-z ]+", " ", text.lower())[:40].strip()
                seen_numeric.setdefault(topic, []).extend(nums)

    if check_consensus:
        for topic, nums in seen_numeric.items():
            if len(nums) >= 2:
                v.checks_run += 1
                lo, hi = min(nums), max(nums)
                if hi > 0 and (hi - lo) / hi > 0.05:
                    v.defects.append(ResearchDefect(
                        code="numeric_disagreement", severity="warning",
                        message=f"Topic {topic!r} conflicting values "
                                f"{lo}..{hi} (>5% spread)",
                        claim_idx=-1))

    v.passed = not any(d.severity == "error" for d in v.defects)
    return v


# ═══════════════════════════════════════════════════════════════════
# DESIGN
# ═══════════════════════════════════════════════════════════════════
_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+\-]*)\n([\s\S]*?)```")
_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
_INPUT_RE = re.compile(r"<(?:input|select|textarea)\b[^>]*>", re.I)
_LABEL_FOR_RE = re.compile(r'<label[^>]*\bfor\s*=\s*["\']([^"\']+)["\']', re.I)
_ARIA_ROLE_RE = re.compile(r'role\s*=\s*["\']([a-z]+)["\']', re.I)
_ARIA_LABELLEDBY_RE = re.compile(r'aria-labelledby\s*=\s*["\']([^"\']+)["\']', re.I)
_ID_ATTR_RE = re.compile(r'\bid\s*=\s*["\']([^"\']+)["\']', re.I)
_VUE_DIRECTIVE_RE = re.compile(r"\bv-(?:if|for|model|show|bind|on|slot)\b")
_REACT_HOOK_RE = re.compile(r"\buse(?:State|Effect|Memo|Callback|Ref|Context)\s*\(")
_INLINE_HANDLER_RE = re.compile(r'\bon(?:click|change|input|submit|focus|blur|keydown)\s*=\s*["\']', re.I)
_HARDCODED_HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_MEDIA_QUERY_RE = re.compile(r"@media\b", re.I)
_VIEWPORT_RE = re.compile(r'<meta[^>]+name\s*=\s*["\']viewport["\']', re.I)
_LANG_RE = re.compile(r'<html[^>]+\blang\s*=\s*["\']([^"\']+)["\']', re.I)
_HEADING_RE = re.compile(r"<h([1-6])\b", re.I)
_CSS_VAR_RE = re.compile(r"--[a-z-]+\s*:")
_BUTTON_RE = re.compile(r"<button\b[^>]*>", re.I)
_TABLE_RE = re.compile(r"<table\b", re.I)
_CAPTION_RE = re.compile(r"<caption\b", re.I)
_TH_SCOPE_RE = re.compile(r'<th\b[^>]*\bscope\s*=\s*["\'](row|col)["\']', re.I)
_REQUIRED_TAGS = ("nav", "main")

_VALID_ARIA_ROLES = frozenset({
    "alert", "alertdialog", "application", "article", "banner", "button",
    "cell", "checkbox", "columnheader", "combobox", "complementary",
    "contentinfo", "definition", "dialog", "directory", "document",
    "feed", "figure", "form", "grid", "gridcell", "group", "heading",
    "img", "link", "list", "listbox", "listitem", "log", "main",
    "marquee", "math", "menu", "menubar", "menuitem", "menuitemcheckbox",
    "menuitemradio", "navigation", "none", "note", "option", "presentation",
    "progressbar", "radio", "radiogroup", "region", "row", "rowgroup",
    "rowheader", "scrollbar", "search", "searchbox", "separator", "slider",
    "spinbutton", "status", "switch", "tab", "table", "tablist", "tabpanel",
    "term", "textbox", "timer", "toolbar", "tooltip", "tree", "treegrid",
    "treeitem"})


@dataclass(frozen=True, slots=True)
class DesignDefect:
    code: str
    severity: str
    message: str
    line: int = 0

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity,
                "message": self.message, "line": self.line}


@dataclass(slots=True)
class DesignVerdict:
    passed: bool
    defects: list[DesignDefect] = field(default_factory=list)
    checks_run: int = 0

    def to_dict(self) -> dict:
        return {"passed": self.passed,
                "defects": [d.to_dict() for d in self.defects],
                "checks_run": self.checks_run,
                "errors": sum(1 for d in self.defects if d.severity == "error"),
                "warnings": sum(1 for d in self.defects if d.severity == "warning")}


def _strip_fences(source: str) -> tuple[str, int]:
    fences = list(_FENCE_RE.finditer(source))
    if not fences:
        return source, 0
    return fences[0].group(2), fences[0].start()


def verify_design(framework: str, source: str,
                  *, requirements: dict | None = None) -> DesignVerdict:
    requirements = requirements or {}
    v = DesignVerdict(passed=True)

    fences = list(_FENCE_RE.finditer(source))
    if len(fences) > 1:
        v.defects.append(DesignDefect(
            code="multiple_fences", severity="error",
            message=f"Expected exactly 1 fenced block; found {len(fences)}"))
    body, offset = _strip_fences(source)
    tag = fences[0].group(1).lower() if fences else ""

    if tag and tag not in (framework, "html", "jsx", "tsx", "vue", "svelte", "css"):
        v.defects.append(DesignDefect(
            code="framework_tag_mismatch", severity="warning",
            message=f"Fence tag '{tag}' differs from framework '{framework}'"))

    v.checks_run += 1
    ok, why = pda_balanced(body.encode("utf-8", "replace"))
    if not ok:
        v.defects.append(DesignDefect(
            code="unbalanced_brackets", severity="error",
            message=f"bracket mismatch: {why}"))
    if framework in ("html", "jsx", "tsx", "vue", "svelte") and "```" in source:
        ok2, why2 = balance_tags(body.encode("utf-8", "replace"))
        if not ok2:
            v.defects.append(DesignDefect(
                code="unbalanced_tags", severity="error",
                message=f"tag mismatch: {why2}"))

    if framework in ("html", "jsx", "tsx", "vue", "svelte"):
        v.checks_run += 1
        for m in _IMG_RE.finditer(body):
            block = m.group(0).lower()
            if "alt=" not in block and "aria-hidden" not in block:
                v.defects.append(DesignDefect(
                    code="img_missing_alt", severity="error",
                    message=f"<img> without alt: {m.group(0)[:80]}",
                    line=body[:m.start()].count("\n") + 1))

        v.checks_run += 1
        label_fors = {m.group(1) for m in _LABEL_FOR_RE.finditer(body)}
        for m in _INPUT_RE.finditer(body):
            block = m.group(0)
            if 'type="hidden"' in block.lower():
                continue
            id_m = re.search(r'\bid\s*=\s*["\']([^"\']+)["\']', block)
            has_label = (id_m and id_m.group(1) in label_fors)
            if "aria-label" in block.lower() or "aria-labelledby" in block.lower():
                has_label = True
            if not has_label:
                v.defects.append(DesignDefect(
                    code="input_missing_label", severity="warning",
                    message=f"<input/select/textarea> without label: {block[:80]}",
                    line=body[:m.start()].count("\n") + 1))

        v.checks_run += 1
        for m in _ARIA_ROLE_RE.finditer(body):
            role = m.group(1).lower()
            if role not in _VALID_ARIA_ROLES:
                v.defects.append(DesignDefect(
                    code="invalid_aria_role", severity="error",
                    message=f"role='{role}' is not a valid ARIA role",
                    line=body[:m.start()].count("\n") + 1))

        v.checks_run += 1
        all_ids = {m.group(1) for m in _ID_ATTR_RE.finditer(body)}
        for m in _ARIA_LABELLEDBY_RE.finditer(body):
            for ref in m.group(1).split():
                if ref not in all_ids:
                    v.defects.append(DesignDefect(
                        code="dangling_aria_labelledby", severity="warning",
                        message=f"aria-labelledby='{ref}' does not match any id",
                        line=body[:m.start()].count("\n") + 1))

        v.checks_run += 1
        seen_ids = {}
        for m in _ID_ATTR_RE.finditer(body):
            seen_ids[m.group(1)] = seen_ids.get(m.group(1), 0) + 1
        for id_val, count in seen_ids.items():
            if count > 1:
                v.defects.append(DesignDefect(
                    code="duplicate_id", severity="warning",
                    message=f"id='{id_val}' appears {count} times"))

        v.checks_run += 1
        levels = [int(m.group(1)) for m in _HEADING_RE.finditer(body)]
        prev = 0
        for h in levels:
            if prev and h > prev + 1:
                v.defects.append(DesignDefect(
                    code="heading_skip", severity="warning",
                    message=f"heading jumps from h{prev} to h{h}"))
            prev = h

        if re.search(r"<html\b", body, re.I):
            v.checks_run += 1
            if not _LANG_RE.search(body):
                v.defects.append(DesignDefect(
                    code="html_missing_lang", severity="warning",
                    message="<html> missing lang attribute"))

        v.checks_run += 1
        for m in _BUTTON_RE.finditer(body):
            close = body.find("</button>", m.end())
            if close < 0:
                continue
            inner = re.sub(r"<[^>]+>", "", body[m.end():close]).strip()
            block = m.group(0).lower()
            has_aria = "aria-label" in block or "aria-labelledby" in block
            if not inner and not has_aria and not re.search(r"<img\b",
                                                              body[m.end():close], re.I):
                v.defects.append(DesignDefect(
                    code="empty_button", severity="warning",
                    message="<button> has no accessible text or aria-label",
                    line=body[:m.start()].count("\n") + 1))

        if _TABLE_RE.search(body):
            v.checks_run += 1
            if not _CAPTION_RE.search(body):
                v.defects.append(DesignDefect(
                    code="table_missing_caption", severity="warning",
                    message="<table> without <caption>"))
            if "<th" in body.lower() and not _TH_SCOPE_RE.search(body):
                v.defects.append(DesignDefect(
                    code="th_missing_scope", severity="warning",
                    message="<th> without scope attribute"))

    v.checks_run += 1
    if framework in ("jsx", "tsx") and _VUE_DIRECTIVE_RE.search(body):
        v.defects.append(DesignDefect(
            code="framework_leak", severity="error",
            message="Vue directive (v-if/v-for/...) found in JSX/TSX"))
    if framework == "vue" and _REACT_HOOK_RE.search(body):
        v.defects.append(DesignDefect(
            code="framework_leak", severity="error",
            message="React hook (useState/...) found in Vue SFC"))

    required = list(requirements.get("required_elements") or _REQUIRED_TAGS)
    if framework in ("html", "jsx", "tsx", "vue", "svelte"):
        for t in required:
            v.checks_run += 1
            if not re.search(rf"<{re.escape(t)}\b", body, re.I):
                v.defects.append(DesignDefect(
                    code="missing_required_element", severity="error",
                    message=f"Required element <{t}> not found"))

    if requirements.get("responsive"):
        v.checks_run += 1
        if not (_MEDIA_QUERY_RE.search(body) or "sm:" in body or "md:" in body
                or _VIEWPORT_RE.search(body)):
            v.defects.append(DesignDefect(
                code="not_responsive", severity="warning",
                message="Responsive required but no @media / Tailwind / viewport"))

    if requirements.get("no_inline_handlers"):
        v.checks_run += 1
        if _INLINE_HANDLER_RE.search(body):
            v.defects.append(DesignDefect(
                code="inline_handler_found", severity="error",
                message="Inline event handler (on{event}=\"...\") is forbidden"))

    if requirements.get("theme") in ("light", "dark"):
        v.checks_run += 1
        hexes = _HARDCODED_HEX_RE.findall(body)
        if len(hexes) > 3 and not _CSS_VAR_RE.search(body):
            v.defects.append(DesignDefect(
                code="hardcoded_colors_no_tokens", severity="warning",
                message=f"{len(hexes)} hardcoded colors and no CSS custom properties"))

    v.passed = not any(d.severity == "error" for d in v.defects)
    return v


__all__ = ["ResearchSource", "ResearchClaim", "ResearchDefect", "ResearchVerdict",
           "extract_citations", "is_vague", "is_specific", "verify_research",
           "DesignDefect", "DesignVerdict", "verify_design"]
