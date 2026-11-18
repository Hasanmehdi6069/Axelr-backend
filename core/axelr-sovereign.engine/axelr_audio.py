"""
axelr_audio.py — DSP front-end (AFS #8, DAFC #12), VAD State Machine (#17),
permissions (ZTPS #4), hotkeys (SHIS #5), capture pipeline with endpointing.

Memory: ~2.4 MB (log-mel 960 KB + frame scratch 800 KB + filterbank 130 KB + misc).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Set, Tuple

import numpy as np

from axelr_config import HardwareProfile, VadConfig
from axelr_ring import WatermarkState, ZeroCopyRingBuffer
from axelr_utils import get_logger

logger = get_logger(__name__)

try:
    import ctypes
    import ctypes.wintypes
    _IS_WINDOWS = True
except Exception:
    ctypes = None  # type: ignore[assignment]
    _IS_WINDOWS = False
    logger.warning("ctypes unavailable; SHIS hotkey capture disabled on this platform")


# ---------- VAD State Machine (#17) -------------------------------------

class VadState(Enum):
    SILENCE = "silence"
    TRANSITION = "transition"
    SPEECH = "speech"


class VadStateMachine:
    """
    #17: 3-state VAD with adaptive noise floor and hysteresis.

    States: SILENCE → (enter_frames of speech energy) → SPEECH
    SPEECH → (exit_frames of silence) → SILENCE.
    TRANSITION prevents flapping on borderline frames.

    Endpoint signal fires when SPEECH → SILENCE and silence persists ≥ endpoint_ms.

    Memory: ~8 KB (a few scalars + config).
    """

    __slots__ = (
        "config", "_state", "_noise_floor", "_speech_run", "_silence_run",
        "_last_change_t", "_endpoint_emitted",
    )

    def __init__(self, config: VadConfig) -> None:
        self.config = config
        self._state = VadState.SILENCE
        self._noise_floor = config.init_noise_floor
        self._speech_run = 0
        self._silence_run = 0
        self._last_change_t = time.monotonic()
        self._endpoint_emitted = False

    @property
    def state(self) -> VadState:
        return self._state

    @property
    def noise_floor(self) -> float:
        return self._noise_floor

    def process_energy(self, energy: float) -> VadState:
        """Feed a frame's RMS² energy. Returns current state."""
        # Adaptive noise floor: only update during SILENCE (avoid locking onto speech)
        if self._state == VadState.SILENCE:
            a = self.config.noise_alpha
            self._noise_floor = (1 - a) * self._noise_floor + a * max(energy, 1e-9)

        is_speechy = energy > self._noise_floor * self.config.speech_multiplier

        if is_speechy:
            self._speech_run += 1
            self._silence_run = 0
            if self._state != VadState.SPEECH and self._speech_run >= self.config.enter_frames:
                self._state = VadState.SPEECH
                self._last_change_t = time.monotonic()
                self._endpoint_emitted = False
        else:
            self._silence_run += 1
            self._speech_run = 0
            if self._state == VadState.SPEECH:
                if self._silence_run >= self.config.exit_frames:
                    self._state = VadState.SILENCE
                    self._last_change_t = time.monotonic()
        return self._state

    def endpoint_fired(self) -> bool:
        """True exactly once per utterance endpoint transition."""
        if self._state != VadState.SILENCE or self._endpoint_emitted:
            return False
        elapsed_ms = (time.monotonic() - self._last_change_t) * 1000.0
        if elapsed_ms >= self.config.endpoint_ms:
            self._endpoint_emitted = True
            return True
        return False

    def reset(self) -> None:
        self._state = VadState.SILENCE
        self._noise_floor = self.config.init_noise_floor
        self._speech_run = 0
        self._silence_run = 0
        self._endpoint_emitted = False


# ---------- DSP config / pipeline ---------------------------------------

@dataclass(frozen=True, slots=True)
class DSPConfig:
    sample_rate: int = 16000
    frame_size_ms: int = 25
    frame_shift_ms: int = 10
    n_mels: int = 80
    min_frequency: float = 0.0
    max_frequency: float = 8000.0
    silence_threshold: float = 0.005
    low_velocity_threshold: float = 0.3
    target_compaction_ratio: float = 0.5
    max_log_mel_frames: int = 3000


class AcousticFeatureSubsampler:
    """#8 AFS: drop silent frames. Uses VAD when available; energy fallback."""

    __slots__ = ("_threshold",)

    def __init__(self, threshold: float) -> None:
        self._threshold = float(threshold)

    def filter(self, frames: np.ndarray) -> np.ndarray:
        if frames.size == 0:
            return frames
        energies = np.mean(frames * frames, axis=1)
        return frames[energies > self._threshold]


class DynamicFrameCompactor:
    """#12 DAFC: uniformly downsample during low-velocity speech."""

    __slots__ = ("_ratio",)

    def __init__(self, ratio: float) -> None:
        self._ratio = max(0.5, min(1.0, ratio))

    def compact(self, frames: np.ndarray) -> np.ndarray:
        n = frames.shape[0]
        if n < 2:
            return frames
        idx = np.linspace(0, n - 1, max(1, int(n * self._ratio)), dtype=np.int32)
        return frames[idx]


class DSPPipeline:
    """
    AFS #8 + DAFC #12 + VAD #17 chained. log-mel buffer pre-allocated once.

    Memory: ~1.9 MB (960 KB log-mel + 800 KB frame scratch + ~140 KB filterbank).
    """

    __slots__ = (
        "config", "vad", "_frame_size", "_frame_shift", "_afs", "_dafc",
        "_window", "_filterbank", "_log_mel", "_speech_frames", "_total_frames",
    )

    def __init__(self, config: DSPConfig, vad: VadStateMachine) -> None:
        self.config = config
        self.vad = vad
        self._frame_size = int(config.sample_rate * config.frame_size_ms / 1000)
        self._frame_shift = int(config.sample_rate * config.frame_shift_ms / 1000)
        self._afs = AcousticFeatureSubsampler(config.silence_threshold)
        self._dafc = DynamicFrameCompactor(config.target_compaction_ratio)
        self._window = np.hanning(self._frame_size).astype(np.float32)
        self._filterbank = self._build_mel_filterbank()
        self._log_mel = np.zeros((config.max_log_mel_frames, config.n_mels), dtype=np.float32)
        self._speech_frames = 0
        self._total_frames = 0

    def _build_mel_filterbank(self) -> np.ndarray:
        n_fft = self._frame_size
        mel_min = 2595.0 * np.log10(1.0 + self.config.min_frequency / 700.0)
        mel_max = 2595.0 * np.log10(1.0 + self.config.max_frequency / 700.0)
        mel_pts = np.linspace(mel_min, mel_max, self.config.n_mels + 2)
        hz = 700.0 * (10.0 ** (mel_pts / 2595.0) - 1.0)
        bins = np.floor((n_fft + 1) * hz / self.config.sample_rate).astype(int)
        fb = np.zeros((self.config.n_mels, n_fft // 2 + 1), dtype=np.float32)
        for m in range(1, self.config.n_mels + 1):
            lo, mid, hi = bins[m - 1], bins[m], bins[m + 1]
            for k in range(lo, mid):
                fb[m - 1, k] = (k - lo) / max(1, mid - lo)
            for k in range(mid, hi):
                fb[m - 1, k] = (hi - k) / max(1, hi - mid)
        return fb

    def _extract_frames(self, samples: np.ndarray) -> np.ndarray:
        n = samples.size
        if n < self._frame_size:
            return np.empty((0, self._frame_size), dtype=np.float32)
        n_frames = 1 + (n - self._frame_size) // self._frame_shift
        idx = (np.arange(self._frame_size)[None, :]
               + np.arange(n_frames)[:, None] * self._frame_shift)
        frames = samples[idx].astype(np.float32)
        frames *= self._window
        return frames

    def velocity(self) -> float:
        return 1.0 if self._total_frames == 0 else self._speech_frames / self._total_frames

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Full DSP chain. Returns slice of pre-allocated log-mel (caller must copy)."""
        frames = self._extract_frames(samples)
        if frames.shape[0] == 0:
            return self._log_mel[:0]
        self._total_frames += frames.shape[0]

        # Feed VAD the frame energies
        energies = np.mean(frames * frames, axis=1)
        for e in energies:
            self.vad.process_energy(float(e))

        speech = self._afs.filter(frames)
        self._speech_frames += speech.shape[0]
        if speech.shape[0] == 0:
            return self._log_mel[:0]

        if self.velocity() < self.config.low_velocity_threshold:
            speech = self._dafc.compact(speech)

        spec = np.abs(np.fft.rfft(speech, self._frame_size)) ** 2
        mel = spec @ self._filterbank.T
        np.clip(mel, 1e-10, None, out=mel)
        np.log(mel, out=mel)
        n = min(mel.shape[0], self.config.max_log_mel_frames)
        self._log_mel[:n] = mel[:n]
        return self._log_mel[:n]


# ---------- ZTPS #4 ------------------------------------------------------

class PermissionState(Enum):
    UNREQUESTED = "unrequested"
    GRANTED = "granted"
    DENIED = "denied"
    REVOKED = "revoked"


class PermissionType(Enum):
    MICROPHONE = "microphone"
    ACCESSIBILITY = "accessibility"
    CLIPBOARD = "clipboard"


@dataclass(slots=True)
class Permission:
    type: PermissionType
    state: PermissionState = PermissionState.UNREQUESTED
    granted_at: Optional[float] = None

    def is_active(self) -> bool:
        return self.state == PermissionState.GRANTED


class ZeroTrustPermissionManager:
    """#4 ZTPS. Platform _request_os / _verify_os are integration points."""

    __slots__ = ("_perms", "_listeners")

    def __init__(self) -> None:
        self._perms: Dict[PermissionType, Permission] = {
            p: Permission(p) for p in (
                PermissionType.MICROPHONE,
                PermissionType.ACCESSIBILITY,
                PermissionType.CLIPBOARD,
            )
        }
        self._listeners: List[Callable[[PermissionType, PermissionState], None]] = []

    def add_listener(self, cb: Callable[[PermissionType, PermissionState], None]) -> None:
        self._listeners.append(cb)

    def _notify(self, p: PermissionType, s: PermissionState) -> None:
        for cb in self._listeners:
            try:
                cb(p, s)
            except Exception:
                logger.exception("permission listener failed")

    def request(self, p: PermissionType) -> bool:
        perm = self._perms[p]
        ok = self._request_os(p)
        perm.state = PermissionState.GRANTED if ok else PermissionState.DENIED
        perm.granted_at = time.monotonic() if ok else None
        self._notify(p, perm.state)
        return ok

    def verify_all(self) -> Tuple[bool, List[PermissionType]]:
        missing: List[PermissionType] = []
        for pt, pm in self._perms.items():
            if not pm.is_active() or not self._verify_os(pt):
                pm.state = PermissionState.REVOKED
                missing.append(pt)
                self._notify(pt, pm.state)
        return len(missing) == 0, missing

    def _request_os(self, p: PermissionType) -> bool:
        try:
            import sys
            if sys.platform == "win32":
                if p == PermissionType.MICROPHONE:
                    try:
                        import winrt.windows.media.devices as media
                        return media.MediaDevice.GetAudioCaptureSelector() is not None
                    except Exception:
                        return False
                elif p == PermissionType.ACCESSIBILITY:
                    try:
                        import winrt.windows.ui.viewmanagement as ui
                        return ui.UIViewSettings.AreScreenReadersEnabled
                    except Exception:
                        return False
                elif p == PermissionType.CLIPBOARD:
                    try:
                        import winrt.windows.applicationmodel as app
                        return app.DataTransfer.Clipboard.IsClipboardHistoryEnabled()
                    except Exception:
                        return False
            elif sys.platform == "darwin":
                if p == PermissionType.MICROPHONE:
                    import AVFoundation
                    return AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_("AVMediaTypeAudio") == 3
                elif p == PermissionType.ACCESSIBILITY:
                    import ApplicationServices
                    return ApplicationServices.AXIsProcessTrusted()
            elif sys.platform.startswith("linux"):
                if p == PermissionType.MICROPHONE:
                    import pyaudio
                    pa = pyaudio.PyAudio()
                    return pa.get_device_count() > 0
            return False
        except Exception:
            return False

    def _verify_os(self, p: PermissionType) -> bool:
        return self._request_os(p)


# ---------- SHIS #5 ------------------------------------------------------

if _IS_WINDOWS:
    class _KBDLLHOOKSTRUCT(ctypes.Structure):  # type: ignore[name-defined]
        _fields_ = [
            ("vkCode", ctypes.wintypes.DWORD),
            ("scanCode", ctypes.wintypes.DWORD),
            ("flags", ctypes.wintypes.DWORD),
            ("time", ctypes.wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]
    _WH_KEYBOARD_LL = 13
    _WM_KEYDOWN = 0x0100
    _WM_SYSKEYDOWN = 0x0104

MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_ALT = 0x0001


@dataclass(frozen=True, slots=True)
class Hotkey:
    key_code: int
    modifiers: int
    name: str


class HotkeyInterceptor:
    """#5 SHIS: Windows low-level keyboard hook in a daemon thread. Re-armable."""

    __slots__ = ("_hotkeys", "_pressed", "_running", "_thread", "_hook",
                 "_proc_ref", "_user32", "_last_trigger", "_debounce_s")

    def __init__(self) -> None:
        self._hotkeys: Dict[int, Callable[[], None]] = {}
        self._pressed: Set[int] = set()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._hook = None
        self._proc_ref = None
        self._user32 = None
        self._last_trigger = 0.0
        self._debounce_s = 0.1

    def register(self, hk: Hotkey, cb: Callable[[], None]) -> None:
        self._hotkeys[(hk.modifiers << 16) | hk.key_code] = cb

    def start(self) -> bool:
        if not _IS_WINDOWS:
            logger.warning("HotkeyInterceptor.start ignored on non-Windows")
            return False
        if self._running:
            return True
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        if _IS_WINDOWS and self._hook and self._user32:
            try:
                self._user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                logger.exception("UnhookWindowsHookEx failed")
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        if not _IS_WINDOWS:
            return
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[union-attr]
        HOOKPROC = ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]
            ctypes.wintypes.LRESULT, ctypes.c_int,
            ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM)

        def _handler(n_code: int, w_param: int, l_param: int) -> int:
            if n_code >= 0 and w_param in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
                kb = ctypes.cast(l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents  # type: ignore[name-defined]
                self._pressed.add(int(kb.vkCode))
                self._maybe_fire(int(kb.vkCode))
            return self._user32.CallNextHookEx(self._hook, n_code, w_param, l_param)

        self._proc_ref = HOOKPROC(_handler)
        self._hook = self._user32.SetWindowsHookExW(
            _WH_KEYBOARD_LL, self._proc_ref,
            ctypes.windll.kernel32.GetModuleHandleW(None), 0)
        if not self._hook:
            logger.error("SetWindowsHookExW failed; hotkey thread exiting")
            self._running = False
            return
        msg = ctypes.wintypes.MSG()  # type: ignore[attr-defined]
        while self._running and self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self._user32.TranslateMessage(ctypes.byref(msg))
            self._user32.DispatchMessageW(ctypes.byref(msg))

    def _mods(self) -> int:
        m = 0
        if {0xA2, 0xA3} & self._pressed:
            m |= MOD_CONTROL
        if {0xA0, 0xA1} & self._pressed:
            m |= MOD_SHIFT
        if {0xA4, 0xA5} & self._pressed:
            m |= MOD_ALT
        return m

    def _maybe_fire(self, vk: int) -> None:
        now = time.monotonic()
        if now - self._last_trigger < self._debounce_s:
            return
        cb = self._hotkeys.get((self._mods() << 16) | vk)
        if cb is None:
            return
        self._last_trigger = now
        try:
            cb()
        except Exception:
            logger.exception("hotkey callback failed")


# ---------- Capture pipeline -------------------------------------------

class AudioCapturePipeline:
    """Ring → DSP → log-mel, with VAD endpoint signal + backpressure marker."""

    __slots__ = ("ring", "dsp", "vad", "_read_chunk", "_last_watermark")

    def __init__(self, profile: HardwareProfile, ring: ZeroCopyRingBuffer,
                 dsp_config: DSPConfig) -> None:
        self.ring = ring
        self.vad = VadStateMachine(profile.vad)
        self.dsp = DSPPipeline(dsp_config, self.vad)
        self._read_chunk = profile.buffer_frames
        self._last_watermark = WatermarkState.NORMAL

    async def read_batch(self) -> Optional[np.ndarray]:
        samples = self.ring.read(self._read_chunk)
        if samples is None or samples.size == 0:
            return None
        self._last_watermark = self.ring.watermark()
        return self.dsp.process(samples)

    def endpoint_fired(self) -> bool:
        return self.vad.endpoint_fired()

    def backpressure(self) -> WatermarkState:
        return self._last_watermark

# ── Elite: MicrophoneSource ──────────────────────────────────────────
class MicrophoneSource:
    """Elite: real microphone capture via sounddevice + ZTPS."""
    __slots__ = (
        "_perm_mgr", "_stream", "_ring", "_running", "_thread",
        "_device_id", "_sample_rate", "_chunk_size",
    )

    def __init__(self, ring: ZeroCopyRingBuffer, device_id: int = -1,
                 sample_rate: int = 16000, chunk_size: int = 1024) -> None:
        self._perm_mgr = ZeroTrustPermissionManager()
        self._stream = None
        self._ring = ring
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._device_id = device_id
        self._sample_rate = sample_rate
        self._chunk_size = chunk_size

    def start(self) -> bool:
        if not self._perm_mgr.request(PermissionType.MICROPHONE):
            logger.error("microphone_permission_denied")
            return False
        try:
            import sounddevice as sd
        except ImportError:
            logger.error("sounddevice_not_installed")
            return False

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        import sounddevice as sd
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        def callback(indata, frames, time, status):
            if status:
                logger.warning("mic_stream_status: %s", status)
            self._ring.write(indata[:, 0])

        try:
            with sd.InputStream(
                samplerate=self._sample_rate,
                channels=1,
                dtype='float32',
                blocksize=self._chunk_size,
                device=self._device_id if self._device_id != -1 else None,
                callback=callback
            ):
                while self._running:
                    time.sleep(0.1)
        except Exception:
            logger.exception("mic_stream_failed")

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None