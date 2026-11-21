"""
System Utilities Module
Provides ultra-fast IPC, clipboard operations, text normalizers, and logging setup
"""

import ctypes
import ctypes.wintypes
import logging
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Configure root logger
log_dir = Path.home() / ".axelr" / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(log_dir / "axelr.log"), logging.StreamHandler(sys.stdout)],
)


@dataclass(slots=True)
class ClipboardWindows:
    """Windows native clipboard access for zero-overhead text operations"""

    user32: ctypes.WinDLL = None

    def __post_init__(self):
        if platform.system() == "Windows":
            self.user32 = ctypes.WinDLL("user32", use_last_error=True)
            self.user32.OpenClipboard.restype = ctypes.wintypes.BOOL
            self.user32.CloseClipboard.restype = ctypes.wintypes.BOOL
            self.user32.SetClipboardData.restype = ctypes.wintypes.HANDLE
            self.user32.GetClipboardData.restype = ctypes.wintypes.HANDLE

    def set_text(self, text: str) -> bool:
        """Set text to clipboard using native Windows APIs"""
        if not self.user32:
            return False

        if not self.user32.OpenClipboard(None):
            return False

        try:
            self.user32.EmptyClipboard()
            text_bytes = text.encode("utf-16le")
            h_mem = ctypes.windll.kernel32.GlobalAlloc(0x0042, len(text_bytes) + 2)
            if not h_mem:
                return False

            buf = ctypes.windll.kernel32.GlobalLock(h_mem)
            ctypes.windll.msvcrt.memcpy(buf, text_bytes, len(text_bytes))
            ctypes.windll.kernel32.GlobalUnlock(h_mem)
            self.user32.SetClipboardData(13, h_mem)  # CF_UNICODETEXT
            return True
        finally:
            self.user32.CloseClipboard()

    def get_text(self) -> Optional[str]:
        """Get text from clipboard using native Windows APIs"""
        if not self.user32:
            return None

        if not self.user32.OpenClipboard(None):
            return None

        try:
            if not self.user32.IsClipboardFormatAvailable(13):
                return None

            h_mem = self.user32.GetClipboardData(13)
            if not h_mem:
                return None

            buf = ctypes.windll.kernel32.GlobalLock(h_mem)
            if not buf:
                return None

            text = ctypes.wstring_at(buf)
            ctypes.windll.kernel32.GlobalUnlock(h_mem)
            return text
        finally:
            self.user32.CloseClipboard()


@dataclass(slots=True)
class TextNormalizer:
    """Ultra-fast text normalization for transcribed output"""

    REPLACEMENTS = {
        # Common dictation corrections
        "period": ".",
        "comma": ",",
        "exclamation mark": "!",
        "question mark": "?",
        "colon": ":",
        "semicolon": ";",
        "new line": "\n",
        "new paragraph": "\n\n",
        "space": " ",
        "underline": "_",
        "dash": "-",
        "hyphen": "-",
        "dot": ".",
        "at": "@",
        "hash": "#",
        "dollar": "$",
        "percent": "%",
        "ampersand": "&",
        "asterisk": "*",
    }

    # Programming-specific expansions
    CODE_EXPANSIONS = {
        "left paren": "(",
        "right paren": ")",
        "left bracket": "[",
        "right bracket": "]",
        "left brace": "{",
        "right brace": "}",
        "equals": "=",
        "plus": "+",
        "minus": "-",
        "multiply": "*",
        "divide": "/",
        "less than": "<",
        "greater than": ">",
        "quote": '"',
        "single quote": "'",
        "backslash": "\\",
        "forward slash": "/",
        "pipe": "|",
    }

    def normalize(self, text: str, is_code: bool = False) -> str:
        """Normalize transcribed text with appropriate expansions"""
        # First apply general replacements
        normalized = text
        for spoken, symbol in self.REPLACEMENTS.items():
            normalized = normalized.replace(f" {spoken} ", symbol)
            normalized = normalized.replace(f"^{spoken} ", symbol)
            normalized = normalized.replace(f" {spoken}$", symbol)

        # Apply code-specific expansions if in a programming context
        if is_code:
            for spoken, symbol in self.CODE_EXPANSIONS.items():
                normalized = normalized.replace(f" {spoken} ", symbol)

        # Clean up extra whitespace
        normalized = " ".join(normalized.split())
        return normalized

    def normalize_code(self, text: str) -> str:
        """Normalize specifically for programming context"""
        return self.normalize(text, is_code=True)


class SharedMemoryIPC:
    """Zero-copy IPC between engine components using Windows named shared memory"""

    def __init__(self, name: str, size: int = 1024 * 1024):  # 1MB default
        self.name = name
        self.size = size
        self.map_handle = None
        self.mapped_addr = None

        if platform.system() == "Windows":
            self._win32_init()

    def _win32_init(self) -> None:
        """Initialize Windows named shared memory"""
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateFileMappingW.restype = ctypes.wintypes.HANDLE
        self.kernel32.MapViewOfFile.restype = ctypes.wintypes.LPVOID

        PAGE_READWRITE = 0x04
        FILE_MAP_ALL_ACCESS = 0xF001F

        self.map_handle = self.kernel32.CreateFileMappingW(
            ctypes.wintypes.HANDLE(-1),  # Invalid handle for page file
            None,
            PAGE_READWRITE,
            0,
            self.size,
            self.name,
        )

        if not self.map_handle:
            raise RuntimeError(f"Failed to create shared memory: {self.name}")

        self.mapped_addr = self.kernel32.MapViewOfFile(self.map_handle, FILE_MAP_ALL_ACCESS, 0, 0, self.size)

        if not self.mapped_addr:
            raise RuntimeError(f"Failed to map shared memory view: {self.name}")

    def write(self, data: bytes) -> int:
        """Write bytes to shared memory"""
        if len(data) > self.size - 4:  # Reserve first 4 bytes for length
            raise ValueError("Data too large for shared memory buffer")

        # Write length first
        length = len(data).to_bytes(4, byteorder="little")
        ctypes.windll.msvcrt.memcpy(self.mapped_addr, length, 4)
        # Write data
        ctypes.windll.msvcrt.memcpy(ctypes.c_void_p(self.mapped_addr.value + 4), data, len(data))
        return len(data)

    def read(self) -> Optional[bytes]:
        """Read bytes from shared memory"""
        # Read length first
        length_bytes = ctypes.create_string_buffer(4)
        ctypes.windll.msvcrt.memcpy(length_bytes, self.mapped_addr, 4)
        length = int.from_bytes(length_bytes.raw, byteorder="little")
        if length == 0 or length > self.size - 4:
            return None

        # Read data
        data = ctypes.create_string_buffer(length)
        ctypes.windll.msvcrt.memcpy(data, ctypes.c_void_p(self.mapped_addr.value + 4), length)
        return data.raw

    def __del__(self):
        """Clean up shared memory resources"""
        if platform.system() == "Windows" and hasattr(self, "kernel32"):
            if self.mapped_addr:
                self.kernel32.UnmapViewOfFile(self.mapped_addr)
            if self.map_handle:
                self.kernel32.CloseHandle(self.map_handle)


# Global utility instances
clipboard = ClipboardWindows()
text_normalizer = TextNormalizer()
