# axelr_ring.py
"""
axelr_ring.py — ZCMR (#11) + Watermark backpressure (#25) + selftest.

Byte layout MUST match JS `axelr-audio.js`:
    16-byte header: <u32 head><u32 tail><u32 pad><u32 pad>
    Payload: capacity_samples * 2 B (int16 LE).

Adds selftest(), integrity_check(), stats() over previous version.

Memory: 16 B + capacity_samples * 2 B. LOW: 64 KB.
"""
from __future__ import annotations

import mmap
import struct
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple

import numpy as np
from axelr_utils import get_logger

logger = get_logger(__name__)

_HEADER_FMT = "<IIII"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)
_BYTES_PER_SAMPLE = 2


class WatermarkState(Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class RingBufferConfig:
    capacity_samples: int
    sample_rate: int = 16000
    bit_depth: int = 16
    low_watermark: float = 0.25
    high_watermark: float = 0.75

    def __post_init__(self) -> None:
        if self.capacity_samples <= 0:
            raise ValueError("capacity_samples must be > 0")
        if (self.capacity_samples & (self.capacity_samples - 1)) != 0:
            raise ValueError("capacity_samples must be a power of two")
        if self.bit_depth != 16:
            raise ValueError("only int16 payload supported")


class ZeroCopyRingBuffer:
    """Lock-protected MPSC ring buffer over anonymous mmap.

    Honest notes:
    * "zero-copy": read_view() returns a numpy view aliasing the mmap. Caller
      must consume before next write. read() copies exactly once.
    * "SPSC": header is protected by a Python threading.Lock, not C11
      atomics. Python's GIL does not guarantee atomicity across mmap.write
      calls (which release the GIL). This is correct, not lock-free.
    """
    __slots__ = ("config", "_mask", "_total_size", "_mmap", "_lock")

    HEADER_SIZE: int = _HEADER_SIZE

    def __init__(self, config: RingBufferConfig) -> None:
        self.config = config
        self._mask = config.capacity_samples - 1
        self._total_size = _HEADER_SIZE + config.capacity_samples * _BYTES_PER_SAMPLE
        self._mmap = mmap.mmap(-1, self._total_size)
        self._lock = threading.Lock()
        self._write_header(0, 0)

    def _write_header(self, head: int, tail: int) -> None:
        self._mmap.seek(0)
        self._mmap.write(struct.pack(_HEADER_FMT, head, tail, 0, 0))

    def _read_header(self) -> Tuple[int, int]:
        self._mmap.seek(0)
        head, tail, _, _ = struct.unpack(_HEADER_FMT, self._mmap.read(_HEADER_SIZE))
        return head, tail

    def _watermark(self, used: int) -> WatermarkState:
        frac = used / self.config.capacity_samples
        if frac >= self.config.high_watermark:
            return WatermarkState.HIGH
        if frac <= self.config.low_watermark:
            return WatermarkState.LOW
        return WatermarkState.NORMAL

    def _payload_offset(self, index: int) -> int:
        return _HEADER_SIZE + index * _BYTES_PER_SAMPLE

    def _write_slice(self, start: int, data: np.ndarray) -> None:
        cap = self.config.capacity_samples
        first_len = min(len(data), cap - start)
        self._mmap.seek(self._payload_offset(start))
        self._mmap.write(data[:first_len].tobytes())
        if first_len < len(data):
            self._mmap.seek(_HEADER_SIZE)
            self._mmap.write(data[first_len:].tobytes())

    def _read_slice_view(self, start: int, length: int) -> np.ndarray:
        cap = self.config.capacity_samples
        first_len = min(length, cap - start)
        first = np.frombuffer(
            self._mmap, dtype=np.int16, count=first_len,
            offset=self._payload_offset(start)).copy()
        if first_len == length:
            return first
        second = np.frombuffer(
            self._mmap, dtype=np.int16, count=length - first_len,
            offset=_HEADER_SIZE).copy()
        return np.concatenate([first, second])

    def write(self, samples: np.ndarray) -> Tuple[int, WatermarkState]:
        if samples.dtype != np.int16:
            samples = samples.astype(np.int16)
        n = int(samples.size)
        if n == 0:
            return 0, self._watermark(self.available())
        cap = self.config.capacity_samples
        if n > cap:
            samples = samples[-cap:]
            n = cap
        with self._lock:
            head, tail = self._read_header()
            used = head - tail
            free = cap - used
            drop = max(0, n - free)
            start = head & self._mask
            self._write_slice(start, samples)
            head += n
            tail += drop
            self._write_header(head, tail)
            self._mmap.flush()
            return n, self._watermark(head - tail)

    write_view = write

    def read(self, max_samples: int) -> Optional[np.ndarray]:
        with self._lock:
            head, tail = self._read_header()
            used = head - tail
            if used <= 0:
                return None
            take = min(used, max_samples)
            start = tail & self._mask
            out = self._read_slice_view(start, take)
            tail += take
            self._write_header(head, tail)
            self._mmap.flush()
            return out

    def read_view(self, max_samples: int) -> Optional[np.ndarray]:
        """Zero-copy view; caller must consume before next write."""
        with self._lock:
            head, tail = self._read_header()
            used = head - tail
            if used <= 0:
                return None
            take = min(used, max_samples)
            start = tail & self._mask
            cap = self.config.capacity_samples
            first_len = min(take, cap - start)
            first = np.frombuffer(self._mmap, dtype=np.int16,
                                  count=first_len, offset=self._payload_offset(start))
            if first_len == take:
                view = first
            else:
                second = np.frombuffer(self._mmap, dtype=np.int16,
                                       count=take - first_len, offset=_HEADER_SIZE)
                view = np.concatenate([first, second])
            tail += take
            self._write_header(head, tail)
            return view

    def peek(self, max_samples: int) -> Optional[np.ndarray]:
        """Peek at samples without consuming them (correct implementation)."""
        with self._lock:
            head, tail = self._read_header()
            used = head - tail
            if used <= 0:
                return None
            take = min(used, max_samples)
            start = tail & self._mask
            cap = self.config.capacity_samples
            # Fix: handle wrap-around correctly in peek() to match read() behavior
            first_len = min(take, cap - start)
            first = np.frombuffer(
                self._mmap, dtype=np.int16, count=first_len,
                offset=self._payload_offset(start)).copy()
            if first_len == take:
                return first
            second = np.frombuffer(
                self._mmap, dtype=np.int16, count=take - first_len,
                offset=_HEADER_SIZE).copy()
            return np.concatenate([first, second])

    def available(self) -> int:
        with self._lock:
            h, t = self._read_header()
            return h - t

    def level(self) -> float:
        return self.available() / self.config.capacity_samples

    def watermark(self) -> WatermarkState:
        return self._watermark(self.available())

    def reset(self) -> None:
        with self._lock:
            self._write_header(0, 0)
            self._mmap.flush()

    def dispose(self) -> None:
        with self._lock:
            self._mmap.close()

    # ── Elite: selftest + integrity_check + stats ─────────────────
    def integrity_check(self) -> Tuple[bool, str]:
        """Verify head/tail invariants without modifying state."""
        with self._lock:
            head, tail = self._read_header()
        if head < tail:
            return False, f"head({head}) < tail({tail})"
        used = head - tail
        if used > self.config.capacity_samples:
            return False, f"used({used}) > capacity({self.config.capacity_samples})"
        if head < 0 or tail < 0:
            return False, "negative head/tail"
        return True, "ok"

    def selftest(self) -> Dict[str, Any]:
        """Run a full write/read cycle. Returns {ok, reason, samples}."""
        try:
            self.reset()
            test_data = np.arange(100, dtype=np.int16)
            written, _wm = self.write(test_data)
            if written != 100:
                return {"ok": False, "reason": f"write returned {written}"}
            read_data = self.read(100)
            if read_data is None or not np.array_equal(read_data, test_data):
                return {"ok": False, "reason": "roundtrip mismatch"}
            ok, msg = self.integrity_check()
            if not ok:
                return {"ok": False, "reason": msg}
            self.reset()
            return {"ok": True, "samples": 100}
        except Exception as e:
            return {"ok": False, "reason": str(e)}

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            head, tail = self._read_header()
        return {
            "capacity": self.config.capacity_samples,
            "head": head,
            "tail": tail,
            "available": head - tail,
            "level": (head - tail) / self.config.capacity_samples,
            "watermark": self._watermark(head - tail).value,
            "total_bytes": self._total_size,
        }


__all__ = ["WatermarkState", "RingBufferConfig", "ZeroCopyRingBuffer"]
