"""Merged deterministic verifiers (v1.5).

Adds: verify_invariants, stable_tie_break, self-diagnostic helpers.
Preserves every v1.4 primitive.
"""
from __future__ import annotations

import ast as _pyast
import hashlib
import hmac as _hmac
import json
import re
import sys
from collections import defaultdict, deque
from fractions import Fraction
from typing import Iterable, Iterator, Optional

try:
    import orjson as _fast_json  # type: ignore
except ImportError:
    _fast_json = None


REASON_OK = sys.intern("ok")
REASON_CHUNK_NOT_FOUND = sys.intern("chunk_not_found")
REASON_EMPTY_SNIPPET = sys.intern("empty_snippet")
REASON_SHORT_SNIPPET = sys.intern("short_snippet_mismatch")
REASON_NOT_SUBSTRING = sys.intern("snippet_not_substring")
REASON_VALUE_NOT_DERIVABLE = sys.intern("value_not_derivable")
REASON_STRICT_THRESHOLD = sys.intern("strict_threshold")
REASON_WEAK_VALUE = sys.intern("weak_value")
REASON_QUERY_ECHO = sys.intern("query_echo")
REASON_QUERY_SIMILAR = sys.intern("query_similar")
REASON_NEGATED = sys.intern("source_negated")
REASON_DETERMINISTIC = sys.intern("deterministic")
REASON_VERBATIM = sys.intern("verbatim_match")
REASON_SHAPE_MISMATCH = sys.intern("shape_mismatch")


_M32 = 0xFFFFFFFF
_FNV_OFFSET = 0x811c9dc5
_FNV_PRIME = 0x01000193
_P31 = 2147483647


def fnv1a32(data: bytes) -> int:
    h = _FNV_OFFSET
    for b in data:
        h = ((h ^ b) * _FNV_PRIME) & _M32
    return h


class MinimalPerfectHash:
    __slots__ = ("_n", "_a", "_p", "_indexes")

    def __init__(self, keys: Iterable) -> None:
        keys = tuple(k.encode() if isinstance(k, str) else k for k in keys)
        self._n = len(keys)
        if self._n == 0:
            self._a, self._p = 1, _P31
            self._indexes: tuple = ()
            return
        hashes = [fnv1a32(k) for k in keys]
        for a in range(1, 4096):
            seen = bytearray(self._n)
            ok = True
            for h in hashes:
                idx = ((a * h) % _P31) % self._n
                if seen[idx]:
                    ok = False; break
                seen[idx] = 1
            if ok:
                self._a, self._p = a, _P31
                buckets: list = [None] * self._n
                for k, h in zip(keys, hashes):
                    buckets[((a * h) % _P31) % self._n] = k
                self._indexes = tuple(buckets)
                return
        raise RuntimeError("MinimalPerfectHash: no displacement found")

    def __contains__(self, key: object) -> bool:
        return self.index(key) is not None

    def index(self, key: object) -> Optional[int]:
        if not isinstance(key, (bytes, str)) or self._n == 0:
            return None
        k = key.encode() if isinstance(key, str) else key
        idx = ((self._a * fnv1a32(k)) % self._p) % self._n
        return idx if self._indexes[idx] == k else None


IDENT, NUM, STR, OP, PUNCT, WS, OTHER = range(7)


class ByteTokenizer:
    __slots__ = ("_b", "_n")

    def __init__(self, data) -> None:
        self._b = memoryview(data)
        self._n = len(self._b)

    def __iter__(self) -> Iterator[tuple]:
        b, n, i = self._b, self._n, 0
        while i < n:
            c = b[i]
            if 48 <= c <= 57:
                s = i
                while i < n and (48 <= b[i] <= 57 or b[i] == 46):
                    i += 1
                yield (NUM, s, i)
            elif (65 <= c <= 90) or (97 <= c <= 122) or c == 95:
                s = i
                while i < n:
                    d = b[i]
                    if (65 <= d <= 90) or (97 <= d <= 122) or (48 <= d <= 57) or d == 95:
                        i += 1
                    else:
                        break
                yield (IDENT, s, i)
            elif c in (34, 39):
                q = c; s = i; i += 1
                while i < n:
                    if b[i] == 92:
                        i += 2; continue
                    if b[i] == q:
                        i += 1; break
                    i += 1
                yield (STR, s, i)
            elif c in b"+-*/%=<>!&|^~":
                yield (OP, i, i + 1); i += 1
            elif c in b"()[]{};:,.":
                yield (PUNCT, i, i + 1); i += 1
            elif c in (32, 9, 10, 13):
                s = i
                while i < n and b[i] in (32, 9, 10, 13):
                    i += 1
                yield (WS, s, i)
            else:
                yield (OTHER, i, i + 1); i += 1


_VOID_TAGS = frozenset({
    b"area", b"base", b"br", b"col", b"embed", b"hr", b"img",
    b"input", b"link", b"meta", b"param", b"source", b"track", b"wbr",
})


def balance_tags(data: bytes) -> tuple:
    stack = []
    i, n = 0, len(data)
    while i < n:
        if data.startswith(b"<!--", i):
            j = data.find(b"-->", i + 4)
            if j < 0:
                return False, "unterminated comment"
            i = j + 3; continue
        if data[i] != 0x3C:
            i += 1; continue
        j = i + 1
        if j < n and data[j] == 0x2F:
            k = j + 1
            while k < n and (48 <= data[k] <= 57 or 65 <= data[k] <= 90 or 97 <= data[k] <= 122):
                k += 1
            name = data[j + 1:k]
            if not stack or stack[-1] != name:
                return False, f"unexpected </{name.decode('ascii', 'replace')}>"
            stack.pop(); i = k
            end = data.find(b">", i)
            if end < 0:
                return False, "unterminated close tag"
            i = end + 1; continue
        k = j
        while k < n and (48 <= data[k] <= 57 or 65 <= data[k] <= 90 or 97 <= data[k] <= 122):
            k += 1
        name = data[j:k]
        q = 0
        while k < n:
            c = data[k]
            if q:
                if c == q: q = 0
            elif c in (0x22, 0x27):
                q = c
            elif c == 0x3E:
                break
            k += 1
        if k >= n:
            return False, f"unterminated <{name.decode('ascii', 'replace')}>"
        self_closing = data[k - 1] == 0x2F
        i = k + 1
        if self_closing or name in _VOID_TAGS or not name:
            continue
        stack.append(name)
    if stack:
        return False, f"unclosed: {[s.decode('ascii', 'replace') for s in stack]}"
    return True, "balanced"


_SHAPES = {
    "INT":       re.compile(r"^-?\d+$"),
    "FLOAT":     re.compile(r"^-?\d+\.\d+$"),
    "AMOUNT":    re.compile(r"^-?\d{1,3}(?:,\d{3})*(?:\.\d{1,4})?$"),
    "PERCENT":   re.compile(r"^-?\d+(?:\.\d+)?\s*%$"),
    "DATE_ISO":  re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "DATE_US":   re.compile(r"^\d{2}/\d{2}/\d{4}$"),
    "EMAIL":     re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"),
    "URL":       re.compile(r"^https?://[^\s]+$"),
    "UUID":      re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"),
}


def matches_shape(value: str, shape: str) -> bool:
    p = _SHAPES.get(shape)
    return p is not None and p.match(value) is not None


_OPEN = {0x28: 0x29, 0x5B: 0x5D, 0x7B: 0x7D}
_CLOSE = {v: k for k, v in _OPEN.items()}


def pda_balanced(data: bytes) -> tuple:
    stack = []
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        if c in (0x22, 0x27):
            q = c; i += 1
            while i < n:
                if data[i] == 0x5C:
                    i += 2; continue
                if data[i] == q:
                    i += 1; break
                i += 1
            continue
        if c in _OPEN:
            stack.append(_OPEN[c]); i += 1
        elif c in _CLOSE:
            if not stack or stack[-1] != c:
                return False, f"unbalanced close at byte {i}"
            stack.pop(); i += 1
        else:
            i += 1
    if stack:
        return False, f"{len(stack)} unclosed bracket(s)"
    return True, "balanced"


def json_fsa_valid(data: bytes) -> tuple:
    try:
        if _fast_json is not None:
            _fast_json.loads(data)
        else:
            json.loads(data)
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, f"json: {getattr(e, 'msg', str(e))} at {getattr(e, 'pos', '?')}"


def py_undefined_names(source: str) -> list:
    try:
        tree = _pyast.parse(source)
    except SyntaxError:
        return ["<syntax-error>"]
    bound = set(dir(__builtins__)) if not isinstance(__builtins__, dict) else set(__builtins__)
    for node in _pyast.walk(tree):
        if isinstance(node, _pyast.Name) and isinstance(node.ctx, (_pyast.Store, _pyast.Del)):
            bound.add(node.id)
        elif isinstance(node, (_pyast.FunctionDef, _pyast.AsyncFunctionDef, _pyast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, _pyast.arg):
            bound.add(node.arg)
        elif isinstance(node, (_pyast.Import, _pyast.ImportFrom)):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, _pyast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (_pyast.Global, _pyast.Nonlocal)):
            bound.update(node.names)
    return sorted({
        n.id for n in _pyast.walk(tree)
        if isinstance(n, _pyast.Name) and isinstance(n.ctx, _pyast.Load) and n.id not in bound
    })


_RESERVED = {
    "python": frozenset({
        "False","None","True","and","as","assert","async","await","break",
        "class","continue","def","del","elif","else","except","finally","for",
        "from","global","if","import","in","is","lambda","nonlocal","not","or",
        "pass","raise","return","try","while","with","yield",
    }),
    "javascript": frozenset({
        "await","break","case","catch","class","const","continue","debugger",
        "default","delete","do","else","export","extends","finally","for",
        "function","if","import","in","instanceof","new","return","static",
        "super","switch","this","throw","try","typeof","var","void","while",
        "with","yield","let",
    }),
}


def cross_language_leak(tokens: Iterable, target: str) -> list:
    target_set = _RESERVED.get(target, frozenset())
    leaked = []
    for t in tokens:
        s = t.decode("ascii", "ignore")
        if s in _RESERVED["python"] or s in _RESERVED["javascript"]:
            if s not in target_set:
                leaked.append(t)
    return leaked


_ARITH = re.compile(
    r"(?<![\d.])(-?\d+(?:\.\d+)?)\s*([+\-*/])\s*(-?\d+(?:\.\d+)?)"
    r"\s*=\s*(-?\d+(?:\.\d+)?)(?![\d.])"
)
_OPS = {
    "+": lambda a, b: a + b,
    "-": lambda a, b: a - b,
    "*": lambda a, b: a * b,
    "/": lambda a, b: a / b if b != 0 else None,
}


def check_arithmetic(text: str) -> list:
    out = []
    for m in _ARITH.finditer(text):
        a_s, op, b_s, c_s = m.groups()
        try:
            a, b, c = Fraction(a_s), Fraction(b_s), Fraction(c_s)
            expected = _OPS[op](a, b)
            if expected is None:
                out.append((False, m.group(0), "divide by zero")); continue
            ok = expected == c
            out.append((ok, m.group(0), "ok" if ok else f"expected {expected}"))
        except Exception as e:  # noqa: BLE001
            out.append((False, m.group(0), f"parse: {e}"))
    return out


class AhoCorasick:
    __slots__ = ("_goto", "_fail", "_out")

    def __init__(self) -> None:
        self._goto = [{}]
        self._fail = [0]
        self._out = [[]]

    def add(self, pattern: str) -> None:
        s = 0
        for ch in pattern:
            ns = self._goto[s].get(ch)
            if ns is None:
                ns = len(self._goto)
                self._goto[s][ch] = ns
                self._goto.append({}); self._fail.append(0); self._out.append([])
            s = ns
        self._out[s].append(pattern)

    def build(self) -> None:
        q = deque(self._goto[0].values())
        while q:
            s = q.popleft()
            for ch, ns in self._goto[s].items():
                f = self._fail[s]
                while f and ch not in self._goto[f]:
                    f = self._fail[f]
                if ch in self._goto[f] and self._goto[f][ch] != ns:
                    self._fail[ns] = self._goto[f][ch]
                else:
                    self._fail[ns] = 0
                self._out[ns] = self._out[ns] + self._out[self._fail[ns]]
                q.append(ns)

    def find_iter(self, text: str) -> Iterator[tuple]:
        s = 0
        for i, ch in enumerate(text):
            while s and ch not in self._goto[s]:
                s = self._fail[s]
            s = self._goto[s].get(ch, 0)
            for pat in self._out[s]:
                yield (i - len(pat) + 1, i + 1, pat)


class _TrieNode:
    __slots__ = ("children", "terminal")
    def __init__(self) -> None:
        self.children = {}
        self.terminal = False


class EntityTrie:
    __slots__ = ("_root",)
    def __init__(self) -> None:
        self._root = _TrieNode()
    def add(self, s: str) -> None:
        n = self._root
        for ch in s:
            n = n.children.setdefault(ch, _TrieNode())
        n.terminal = True
    def __contains__(self, s: object) -> bool:
        if not isinstance(s, str):
            return False
        n = self._root
        for ch in s:
            n = n.children.get(ch)
            if n is None:
                return False
        return n.terminal


_NEGATORS = frozenset({
    "not","no","never","none","nobody","nothing","neither","nor","without",
    "cannot","can't","won't","didn't","doesn't","isn't","aren't",
})
_SENT_BREAK = re.compile(r"[.!?\n]")


def negation_window(text: str, start: int, end: int, max_radius: int = 240) -> tuple:
    s0 = start
    while s0 > 0 and s0 > start - max_radius and not _SENT_BREAK.match(text[s0 - 1]):
        s0 -= 1
    s1 = end
    while s1 < len(text) and s1 < end + max_radius and not _SENT_BREAK.match(text[s1]):
        s1 += 1
    window = text[s0:s1].lower()
    neg = any(tok in _NEGATORS for tok in re.findall(r"[a-z']+", window))
    return s0, s1, neg


class LineageIndex:
    __slots__ = ("_map",)
    def __init__(self) -> None:
        self._map = defaultdict(list)
    def record(self, field: str, canon_value: str, doc_id: str,
               chunk_id: str, start: int, end: int) -> None:
        self._map[(field, canon_value)].append((doc_id, chunk_id, start, end))
    def evidence(self, field: str, canon_value: str) -> tuple:
        spans = self._map.get((field, canon_value), [])
        return len(spans), len({d for d, *_ in spans})
    def witnesses(self, field: str, canon_value: str) -> list:
        return self._map.get((field, canon_value), [])


def contradiction(canon_claim: str, source_window: str) -> Optional[str]:
    claim_tokens = re.findall(r"[a-z']+", canon_claim.lower())
    src_tokens = set(re.findall(r"[a-z']+", source_window.lower()))
    if (_NEGATORS & src_tokens) and not (_NEGATORS & set(claim_tokens)):
        return "source negated; claim dropped negator"
    return None


def tarjan_scc_iterative(graph: dict) -> list:
    index_counter = [0]
    stack = []
    lowlink = {}
    index = {}
    on_stack = set()
    result = []
    for start in list(graph):
        if start in index:
            continue
        work = [(start, 0)]
        while work:
            v, child_i = work[-1]
            if child_i == 0:
                index[v] = lowlink[v] = index_counter[0]
                index_counter[0] += 1
                stack.append(v); on_stack.add(v)
            children = list(graph.get(v, ()))
            if child_i < len(children):
                w = children[child_i]
                work[-1] = (v, child_i + 1)
                if w not in index:
                    work.append((w, 0))
                elif w in on_stack:
                    lowlink[v] = min(lowlink[v], index[w])
            else:
                work.pop()
                if work:
                    u = work[-1][0]
                    lowlink[u] = min(lowlink[u], lowlink[v])
                if lowlink[v] == index[v]:
                    comp = []
                    while True:
                        w = stack.pop(); on_stack.discard(w); comp.append(w)
                        if w == v: break
                    result.append(comp)
    return result


def has_cycle(graph: dict) -> bool:
    return any(len(c) > 1 for c in tarjan_scc_iterative(graph))


def request_fingerprint(*, document_bytes: bytes, query: str,
                        target_fields: Iterable[str], tier: str) -> str:
    h = hashlib.blake2b(digest_size=16)
    h.update(document_bytes); h.update(b"\x00")
    h.update(query.encode("utf-8")); h.update(b"\x00")
    h.update(("|".join(sorted(target_fields))).encode("utf-8")); h.update(b"\x00")
    h.update(tier.encode("utf-8"))
    return h.hexdigest()


def replay_key(*, request_fingerprint: str, engine_version: str,
               canon_version: str) -> str:
    h = hashlib.sha256()
    h.update(engine_version.encode()); h.update(b"\x1f")
    h.update(canon_version.encode());  h.update(b"\x1f")
    h.update(request_fingerprint.encode())
    return h.hexdigest()


def chunk_hash(text: str) -> str:
    return hashlib.blake2b(
        text.encode("utf-8", "replace"), digest_size=16,
    ).hexdigest()


def sign_root(merkle_root: str, workspace: str,
              secret: Optional[bytes] = None) -> str:
    key = secret if secret is not None else hashlib.sha256(
        f"axelr-ahe/1.5::{workspace}".encode()
    ).digest()
    return _hmac.new(key, merkle_root.encode(), hashlib.sha256).hexdigest()


def project_budget(*, max_chunks: int, max_claims: int,
                   avg_chunk_chars: int, per_claim_overhead: int = 2048) -> dict:
    chunk_bytes = max_chunks * avg_chunk_chars * 2
    claim_bytes = max_claims * per_claim_overhead
    return {
        "chunks_bytes": chunk_bytes,
        "claims_bytes": claim_bytes,
        "total_bytes": chunk_bytes + claim_bytes,
        "assumptions": {"bytes_per_char": 2, "per_claim_overhead": per_claim_overhead},
    }


def stable_tie_break(claims) -> list:
    """Deterministic order for hashing. Ensures bit-for-bit reproducibility."""
    return sorted(
        claims,
        key=lambda c: (c.field_name, c.coordinate.char_start,
                       c.coordinate.char_end, c.value),
    )


def verify_invariants(payload) -> list:
    """Post-build invariant check. Returns list of violations (empty = pass)."""
    violations: list = []
    for i, c in enumerate(payload.claims):
        if c.coordinate.char_end < c.coordinate.char_start:
            violations.append(f"claims[{i}]: char_end < char_start")
        if not c.verification.verified:
            violations.append(f"claims[{i}]: unverified claim in verified payload")
    if payload.claims:
        expected = hashlib.sha256()
        # Recompute a lightweight structural hash and compare against audit_hash.
        h = hashlib.sha256()
        h.update(payload.engine_version.encode("utf-8")); h.update(b"\x1f")
        h.update(payload.canon_version.encode("utf-8"));   h.update(b"\x1f")
        h.update(payload.document_id.encode("utf-8"));     h.update(b"\x1f")
        for c in stable_tie_break(payload.claims):
            h.update(c.field_name.encode("utf-8")); h.update(b"\x00")
            h.update(c.value.encode("utf-8"));      h.update(b"\x00")
            h.update(c.coordinate.chunk_id.encode("utf-8")); h.update(b"\x00")
            h.update(str(c.coordinate.char_start).encode("ascii")); h.update(b"-")
            h.update(str(c.coordinate.char_end).encode("ascii"));   h.update(b"\x1e")
        if h.hexdigest() != payload.audit_hash:
            violations.append("audit_hash mismatch")
    return violations


__all__ = [
    "fnv1a32","MinimalPerfectHash","ByteTokenizer","balance_tags","matches_shape",
    "IDENT","NUM","STR","OP","PUNCT","WS","OTHER",
    "pda_balanced","json_fsa_valid","py_undefined_names","cross_language_leak",
    "check_arithmetic","AhoCorasick","EntityTrie",
    "negation_window","LineageIndex","contradiction",
    "tarjan_scc_iterative","has_cycle",
    "request_fingerprint","replay_key","chunk_hash","sign_root","project_budget",
    "stable_tie_break","verify_invariants",
    "REASON_OK","REASON_CHUNK_NOT_FOUND","REASON_EMPTY_SNIPPET",
    "REASON_SHORT_SNIPPET","REASON_NOT_SUBSTRING","REASON_VALUE_NOT_DERIVABLE",
    "REASON_STRICT_THRESHOLD","REASON_WEAK_VALUE","REASON_QUERY_ECHO",
    "REASON_QUERY_SIMILAR","REASON_NEGATED","REASON_DETERMINISTIC",
    "REASON_VERBATIM","REASON_SHAPE_MISMATCH",
]