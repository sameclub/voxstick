"""Paste a transcript into whatever window currently has focus.

Every backend follows the same shape: remember the clipboard, put the
transcript on it, synthesise the platform's paste chord, then put the old
clipboard back. Windows goes through ctypes so the bridge keeps its
standard-library-only dependency footprint.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

CLIPBOARD_RESTORE_DELAY_SECONDS = 0.2
ENTER_DELAY_SECONDS = 0.12


@dataclass
class PasteResult:
    success: bool
    message: str


class PasteInjector:
    """Dispatches to the backend for the host platform."""

    def __init__(self) -> None:
        self._backend = _select_backend()

    @property
    def backend_name(self) -> str:
        return self._backend.name if self._backend else "none"

    def paste(self, text: str, press_enter: bool = False, target_provider: str = "") -> PasteResult:
        text = text.strip()
        if not text:
            return PasteResult(False, "No text to paste")
        if self._backend is None:
            return PasteResult(False, f"Automatic paste is not supported on {sys.platform}")

        target_window = None
        if target_provider:
            from vox_stick.paste.app_target import focus_composer
            try:
                target_window = focus_composer(target_provider)
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                return PasteResult(False, str(exc))

        previous_text = self._backend.read_clipboard()
        write_result = self._backend.write_clipboard(text)
        if not write_result.success:
            return write_result

        if target_window:
            from vox_stick.paste.app_target import is_foreground
            if not is_foreground(target_window):
                if previous_text is not None:
                    self._backend.write_clipboard(previous_text)
                return PasteResult(False, "Destination app lost focus; transcript was not pasted")
        result = self._backend.send_paste(press_enter=press_enter)
        time.sleep(CLIPBOARD_RESTORE_DELAY_SECONDS)
        if previous_text is not None:
            self._backend.write_clipboard(previous_text)
        return result


class _WindowsBackend:
    name = "windows"

    # https://learn.microsoft.com/windows/win32/dataxchg/standard-clipboard-formats
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    VK_CONTROL = 0x11
    VK_RETURN = 0x0D
    VK_V = 0x56

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        ulong_ptr = wintypes.WPARAM

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ulong_ptr),
            ]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ulong_ptr),
            ]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [
                ("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD),
            ]

        class INPUTUNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("value",)
            _fields_ = [("type", wintypes.DWORD), ("value", INPUTUNION)]

        self._INPUT = INPUT
        self._KEYBDINPUT = KEYBDINPUT

        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        self._user32.SendInput.restype = wintypes.UINT
        self._user32.OpenClipboard.argtypes = (wintypes.HWND,)
        self._user32.GetClipboardData.argtypes = (wintypes.UINT,)
        self._user32.GetClipboardData.restype = wintypes.HANDLE
        self._user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
        self._user32.SetClipboardData.restype = wintypes.HANDLE
        self._kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
        self._kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        self._kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
        self._kernel32.GlobalLock.restype = ctypes.c_void_p
        self._kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
        self._kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)
        self._kernel32.GlobalFree.restype = wintypes.HGLOBAL

    def read_clipboard(self) -> str | None:
        if not self._open_clipboard():
            return None
        try:
            handle = self._user32.GetClipboardData(self.CF_UNICODETEXT)
            if not handle:
                return None
            pointer = self._kernel32.GlobalLock(handle)
            if not pointer:
                return None
            try:
                return self._ctypes.wstring_at(pointer)
            finally:
                self._kernel32.GlobalUnlock(handle)
        finally:
            self._user32.CloseClipboard()

    def write_clipboard(self, text: str) -> PasteResult:
        payload = text.encode("utf-16-le") + b"\x00\x00"
        handle = self._kernel32.GlobalAlloc(self.GMEM_MOVEABLE, len(payload))
        if not handle:
            return PasteResult(False, "Clipboard write failed: GlobalAlloc")
        pointer = self._kernel32.GlobalLock(handle)
        if not pointer:
            self._kernel32.GlobalFree(handle)
            return PasteResult(False, "Clipboard write failed: GlobalLock")
        self._ctypes.memmove(pointer, payload, len(payload))
        self._kernel32.GlobalUnlock(handle)

        if not self._open_clipboard():
            self._kernel32.GlobalFree(handle)
            return PasteResult(False, "Clipboard write failed: another app holds the clipboard")
        try:
            self._user32.EmptyClipboard()
            # Ownership of the block transfers to the system on success only.
            if not self._user32.SetClipboardData(self.CF_UNICODETEXT, handle):
                self._kernel32.GlobalFree(handle)
                return PasteResult(False, "Clipboard write failed: SetClipboardData")
        finally:
            self._user32.CloseClipboard()
        return PasteResult(True, "Clipboard updated")

    def send_paste(self, *, press_enter: bool) -> PasteResult:
        destination = self._user32.GetForegroundWindow()
        chord = [
            (self.VK_CONTROL, False),
            (self.VK_V, False),
            (self.VK_V, True),
            (self.VK_CONTROL, True),
        ]
        if not self._send_keys(chord):
            return PasteResult(False, "Paste failed: SendInput was blocked")
        if press_enter:
            time.sleep(ENTER_DELAY_SECONDS)
            if self._user32.GetForegroundWindow() != destination:
                return PasteResult(False, "Text pasted but not sent: destination lost focus")
            if not self._send_keys([(self.VK_RETURN, False), (self.VK_RETURN, True)]):
                return PasteResult(False, "Text pasted but send failed: SendInput was blocked")
        return PasteResult(True, "Pasted and Enter pressed" if press_enter else "Pasted into the focused app")

    def _send_keys(self, keys: list[tuple[int, bool]]) -> bool:
        events = (self._INPUT * len(keys))()
        for index, (key, key_up) in enumerate(keys):
            events[index].type = self.INPUT_KEYBOARD
            events[index].ki = self._KEYBDINPUT(
                wVk=key,
                wScan=0,
                dwFlags=self.KEYEVENTF_KEYUP if key_up else 0,
                time=0,
                dwExtraInfo=0,
            )
        sent = self._user32.SendInput(len(keys), events, self._ctypes.sizeof(self._INPUT))
        return sent == len(keys)

    def _open_clipboard(self) -> bool:
        # The clipboard is a single global lock; another app may hold it briefly.
        for attempt in range(5):
            if self._user32.OpenClipboard(None):
                return True
            time.sleep(0.02 * (attempt + 1))
        return False


class _MacBackend:
    name = "macos"

    def read_clipboard(self) -> str | None:
        result = _run(["pbpaste"], timeout=2)
        if result is None or result.returncode != 0:
            return None
        return result.stdout

    def write_clipboard(self, text: str) -> PasteResult:
        result = _run(["pbcopy"], input_text=text, timeout=2)
        if result is None:
            return PasteResult(False, "Clipboard write failed: pbcopy is unavailable")
        if result.returncode != 0:
            return PasteResult(False, (result.stderr or "Clipboard write failed").strip())
        return PasteResult(True, "Clipboard updated")

    def send_paste(self, *, press_enter: bool) -> PasteResult:
        script = ['tell application "System Events" to keystroke "v" using command down']
        if press_enter:
            script.extend(["delay 0.12", 'tell application "System Events" to key code 36'])
        args = ["osascript"]
        for line in script:
            args.extend(["-e", line])
        result = _run(args, timeout=5)
        if result is None:
            return PasteResult(False, "Paste failed: osascript is unavailable")
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "macOS paste failed").strip()
            return PasteResult(False, message)
        return PasteResult(True, "Pasted into the focused app")


class _LinuxBackend:
    """Best effort: needs a clipboard tool and a key-synthesis tool on PATH."""

    name = "linux"

    def __init__(self) -> None:
        self.wayland = shutil.which("wl-copy") is not None

    def read_clipboard(self) -> str | None:
        command = ["wl-paste", "--no-newline"] if self.wayland else ["xclip", "-selection", "clipboard", "-o"]
        result = _run(command, timeout=2)
        if result is None or result.returncode != 0:
            return None
        return result.stdout

    def write_clipboard(self, text: str) -> PasteResult:
        command = ["wl-copy"] if self.wayland else ["xclip", "-selection", "clipboard"]
        result = _run(command, input_text=text, timeout=2)
        if result is None:
            return PasteResult(False, f"Clipboard write failed: install {command[0]}")
        if result.returncode != 0:
            return PasteResult(False, (result.stderr or "Clipboard write failed").strip())
        return PasteResult(True, "Clipboard updated")

    def send_paste(self, *, press_enter: bool) -> PasteResult:
        if shutil.which("xdotool"):
            command = ["xdotool", "key", "--clearmodifiers", "ctrl+v"]
            enter_command = ["xdotool", "key", "--clearmodifiers", "Return"]
        elif shutil.which("ydotool"):
            command = ["ydotool", "key", "29:1", "47:1", "47:0", "29:0"]
            enter_command = ["ydotool", "key", "28:1", "28:0"]
        else:
            return PasteResult(False, "Paste failed: install xdotool or ydotool")
        result = _run(command, timeout=5)
        if result is None or result.returncode != 0:
            message = (result.stderr if result else "").strip() or "Linux paste failed"
            return PasteResult(False, message)
        if press_enter:
            time.sleep(ENTER_DELAY_SECONDS)
            _run(enter_command, timeout=5)
        return PasteResult(True, "Pasted into the focused app")


def _select_backend() -> _WindowsBackend | _MacBackend | _LinuxBackend | None:
    if sys.platform == "win32":
        try:
            return _WindowsBackend()
        except (OSError, AttributeError):
            return None
    if sys.platform == "darwin":
        return _MacBackend()
    if sys.platform.startswith("linux"):
        return _LinuxBackend()
    return None


def _run(
    args: list[str],
    *,
    input_text: str | None = None,
    timeout: int,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            args,
            input=input_text,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
