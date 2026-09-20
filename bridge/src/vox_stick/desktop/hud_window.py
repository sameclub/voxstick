"""Floating status overlay: the cross-platform stand-in for the Swift HUD.

The bridge only writes `hud-state.json`; this process renders it. It never
takes keyboard focus, because the transcript is pasted into whatever window
the user was already typing in.

Run it with:  python -m vox_stick.desktop.hud_window
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import tkinter as tk
from typing import Any

from vox_stick.config.paths import HUD_STATE_PATH

POLL_MS = 80
FRAME_MS = 40
WIDTH = 240
HEIGHT = 84
BOTTOM_MARGIN = 120
BACKGROUND = "#141414"
FOREGROUND = "#f2f2f2"
BAR_COLOR = "#f2f2f2"
ERROR_COLOR = "#ff6b5e"
OFFSCREEN_Y = -4000
BAR_COUNT = 5
BAR_WIDTH = 5
BAR_GAP = 5
BAR_BASE_HEIGHTS = (14, 24, 34, 24, 14)
STATIC_STATUSES = {"failed", "unclear"}
PARENT_CHECK_MS = 2000


class Hud:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BACKGROUND)
        self.root.geometry(f"{WIDTH}x{HEIGHT}+0+{OFFSCREEN_Y}")

        self.canvas = tk.Canvas(
            self.root,
            width=WIDTH,
            height=HEIGHT,
            bg=BACKGROUND,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack()
        self.bars = [
            self.canvas.create_rectangle(0, 0, 0, 0, fill=BAR_COLOR, outline="")
            for _ in range(BAR_COUNT)
        ]
        self.label = self.canvas.create_text(
            WIDTH // 2,
            HEIGHT - 22,
            text="",
            fill=FOREGROUND,
            font=("Segoe UI", 12) if sys.platform == "win32" else ("Helvetica", 13),
        )

        self.visible = False
        self.status = "idle"
        self.animate = False
        self.phase = 0.0

        self.root.deiconify()
        self._make_non_activating()
        self._layout_bars(1.0)

    # -- window plumbing ---------------------------------------------------
    def _make_non_activating(self) -> None:
        """Keep the overlay from stealing focus from the user's editor."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.SetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_long)
            user32.SetWindowLongW.restype = ctypes.c_long

            self.root.update_idletasks()
            try:
                # `wm frame` reports the outermost window; Tk's own id is a child of it.
                raw_handle = int(self.root.frame(), 16)
            except (ValueError, tk.TclError):
                raw_handle = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            handle = wintypes.HWND(raw_handle)
            style = user32.GetWindowLongW(handle, GWL_EXSTYLE)
            user32.SetWindowLongW(handle, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except (OSError, ValueError, AttributeError, tk.TclError):
            # A focus-stealing HUD is still better than no HUD.
            pass

    def _show(self) -> None:
        if self.visible:
            return
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width - WIDTH) // 2
        y = screen_height - BOTTOM_MARGIN - HEIGHT
        self.root.geometry(f"{WIDTH}x{HEIGHT}+{x}+{y}")
        self.root.attributes("-topmost", True)
        self.visible = True

    def _hide(self) -> None:
        if not self.visible:
            return
        self.root.geometry(f"{WIDTH}x{HEIGHT}+0+{OFFSCREEN_Y}")
        self.visible = False

    # -- rendering ---------------------------------------------------------
    def _layout_bars(self, scale: float) -> None:
        total = BAR_COUNT * BAR_WIDTH + (BAR_COUNT - 1) * BAR_GAP
        left = (WIDTH - total) // 2
        middle = HEIGHT - 52
        for index, item in enumerate(self.bars):
            wobble = 1.0
            if self.animate:
                wobble = 0.45 + 0.55 * abs(math.sin(self.phase + index * 0.7))
            height = BAR_BASE_HEIGHTS[index] * scale * wobble
            x0 = left + index * (BAR_WIDTH + BAR_GAP)
            self.canvas.coords(item, x0, middle - height / 2, x0 + BAR_WIDTH, middle + height / 2)

    def _apply_state(self, state: dict[str, Any]) -> None:
        active = bool(state.get("active"))
        expires_at = state.get("expires_at_epoch")
        if active and isinstance(expires_at, (int, float)) and time.time() > expires_at:
            active = False

        self.status = str(state.get("status") or "idle")
        self.animate = active and self.status not in STATIC_STATUSES
        if not active:
            self._hide()
            return

        self.canvas.itemconfigure(
            self.label,
            text=str(state.get("text") or ""),
            fill=ERROR_COLOR if self.status in STATIC_STATUSES else FOREGROUND,
        )
        for item in self.bars:
            self.canvas.itemconfigure(item, fill=ERROR_COLOR if self.status in STATIC_STATUSES else BAR_COLOR)
        self._show()

    # -- loops -------------------------------------------------------------
    def poll(self) -> None:
        self._apply_state(_read_state())
        self.root.after(POLL_MS, self.poll)

    def watch_parent(self) -> None:
        # The launcher may be killed outright rather than closed; do not outlive it.
        if not _parent_alive():
            self.root.destroy()
            return
        self.root.after(PARENT_CHECK_MS, self.watch_parent)

    def tick(self) -> None:
        if self.visible:
            self.phase += 0.35
            self._layout_bars(1.0)
        self.root.after(FRAME_MS, self.tick)

    def run(self) -> None:
        self.poll()
        self.tick()
        if _parent_pid():
            self.watch_parent()
        self.root.mainloop()


def _parent_pid() -> int:
    try:
        return int(os.environ.get("VOX_STICK_PARENT_PID", "0"))
    except ValueError:
        return 0


def _parent_alive() -> bool:
    pid = _parent_pid()
    if pid <= 0:
        return True
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        SYNCHRONIZE = 0x00100000
        WAIT_OBJECT_0 = 0x0
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # A HANDLE is pointer-sized; the default c_int restype would truncate it.
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            # Signalled means the process has exited; os.kill is not usable here
            # because on Windows it terminates the target instead of probing it.
            return kernel32.WaitForSingleObject(handle, 0) != WAIT_OBJECT_0
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _read_state() -> dict[str, Any]:
    try:
        data = json.loads(HUD_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {"active": False}
    return data if isinstance(data, dict) else {"active": False}


def main() -> None:
    try:
        Hud().run()
    except tk.TclError as exc:
        print(f"HUD unavailable: {exc}", flush=True)


if __name__ == "__main__":
    main()
