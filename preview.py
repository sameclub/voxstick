"""Software layout preview for the VoxStick screens.

Mirrors the coordinates and scales in src/main.cpp using the firmware's own
5x7 glyph table, so text that would run off the 240x240 panel fails here
instead of on the device. It is a layout check, not an LCD emulation: colour
order, timing and the real panel are out of scope.

Run: python voxstick/preview.py
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "preview"

def _glcdfont() -> Path:
    """Locate Arduino_GFX's 5x7 glyph table.

    Prefer the copy PlatformIO installs under .pio/libdeps (present after one
    `pio run`), and fall back to a sibling Launcher checkout for the shared
    development layout.
    """
    candidates = list(ROOT.glob(".pio/libdeps/*/GFX Library for Arduino/src/font/glcdfont.h"))
    candidates.append(ROOT.parent / "Launcher" / "lib" / "Arduino_GFX" / "src" / "font" / "glcdfont.h")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SystemExit(
        "glcdfont.h not found. Run `pio run` once so PlatformIO fetches "
        "Arduino_GFX, then run this script again."
    )


font_source = _glcdfont().read_text()
font_source = font_source.split("font[] PROGMEM = {", 1)[1].split("};", 1)[0]
font_source = re.sub(r"//[^\n]*", "", font_source)
FONT = bytes(int(value, 16) for value in re.findall(r"0x([0-9A-Fa-f]{2})", font_source))


def rgb(value: int) -> tuple[int, int, int]:
    """RGB565 as written in the firmware -> RGB888 for the preview."""
    red, green, blue = (value >> 11) & 0x1F, (value >> 5) & 0x3F, value & 0x1F
    return red * 255 // 31, green * 255 // 63, blue * 255 // 31


BG = rgb(0x0000)
WHITE = rgb(0xFFFF)
MID = rgb(0x8410)
DIM = rgb(0x4208)
GREEN = rgb(0x07E6)
RED = rgb(0xF986)
YELLOW = rgb(0xFEA0)
CYAN = rgb(0x07FF)
ACCENT_CLAUDE = rgb(0xDBAA)
ACCENT_CODEX = rgb(0xFFFF)

STATUS_COLORS = {
    "RUNNING": GREEN,
    "DONE": CYAN,
    "ERROR": RED,
    "APPROVAL": YELLOW,
    "IDLE": MID,
}


def status_color(status: str) -> tuple[int, int, int]:
    return STATUS_COLORS.get(status, DIM)


def text_width(value: str, scale: int = 1) -> int:
    return len(value) * 6 * scale


def text(image: Image.Image, x: int, y: int, value: str, color=WHITE, scale: int = 1) -> None:
    """Adafruit GFX print(): 6*scale advance, 8*scale line height, top-left origin."""
    assert 0 <= x, (x, value)
    assert x + text_width(value, scale) <= image.width + 6 * scale, ("overflows width", x, value)
    assert 0 <= y and y + 8 * scale <= image.height, ("overflows height", y, value)
    draw = ImageDraw.Draw(image)
    for char in value:
        assert 32 <= ord(char) < 127, char
        for column, bits in enumerate(FONT[ord(char) * 5:ord(char) * 5 + 5]):
            for row in range(8):
                if bits & (1 << row):
                    px, py = x + column * scale, y + row * scale
                    draw.rectangle((px, py, px + scale - 1, py + scale - 1), fill=color)
        x += 6 * scale


def blank() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (240, 240), BG)
    return image, ImageDraw.Draw(image)


def quota_row(image, draw, y: int, label: str, value, accent, stale: bool = False) -> None:
    text(image, 10, y + 4, label, MID)
    percent = f"{value}%" if value is not None else "--%"
    color = DIM if value is None else RED if value <= 20 else WHITE
    text(image, 230 - len(percent) * 12, y, percent, color, 2)
    draw.rectangle((10, y + 22, 10 + 220 - 1, y + 22 + 12 - 1), outline=DIM)
    if value is not None:
        width = 218 * max(0, min(100, value)) // 100
        if width:
            draw.rectangle((11, y + 23, 11 + width - 1, y + 23 + 10 - 1), fill=accent)


def status_bar(image, clock: str, right: str, healthy: bool) -> None:
    text(image, 10, 8, clock, WHITE, 2)
    text(image, 232 - len(right) * 6, 12, right, MID if healthy else YELLOW)


def home(
    *,
    name: str,
    accent,
    status: str,
    project: str,
    quota_5h,
    quota_7d,
    active: bool = True,
    stale: bool = False,
    clock: str = "13:01",
    right: str = "LINK 86%",
    healthy: bool = True,
    message: tuple[str, tuple[int, int, int]] | None = None,
    portal: tuple[str, str] | None = None,
) -> Image.Image:
    image, draw = blank()
    status_bar(image, clock, right, healthy)
    draw.line((10, 30, 229, 30), fill=DIM)

    text(image, 10, 40, name.upper(), accent, 4)
    if active:
        draw.ellipse((222, 52, 230, 60), fill=accent)

    draw.ellipse((10, 83, 18, 91), fill=status_color(status))
    text(image, 26, 84, status, status_color(status))
    shown = project[:22]
    text(image, 230 - len(shown) * 6, 84, shown, DIM)

    quota_row(image, draw, 104, "5H", quota_5h, accent, stale)
    quota_row(image, draw, 150, "7D", quota_7d, accent, stale)

    # START raises the hotspot and the two bottom lines carry it, exactly as
    # DotMic does; pressing START again drops back to the hint line.
    if portal:
        text(image, 10, 202, portal[0], accent)
        text(image, 10, 224, portal[1], accent if portal[0].startswith("AP") else DIM)
        return image

    if message:
        text(image, 10, 202, message[0][:38], message[1])
    text(image, 10, 224, "AI TALK  <>SWAP  Y SYNC  SEL DIAG", DIM)
    return image


def diag_row(image, y: int, label: str, value: str, color=MID) -> None:
    text(image, 10, y, label, DIM)
    text(image, 64, y, value[:29], color)


def link_page() -> Image.Image:
    image, _ = blank()
    text(image, 10, 8, "VOXSTICK / LINK", ACCENT_CLAUDE)
    diag_row(image, 30, "WIFI", "Home-2G", MID)
    diag_row(image, 46, "IP", "192.168.1.108")
    diag_row(image, 62, "BRIDGE", "192.168.1.20:8765", GREEN)
    diag_row(image, 78, "TOKEN", "SET")
    diag_row(image, 94, "CFG", "CONFIG LOADED")
    diag_row(image, 110, "INI", "SIZE 144 LINES 4 KEYS 4")
    diag_row(image, 126, "HTTP", "200")
    diag_row(image, 142, "ERROR", "NONE")
    diag_row(image, 158, "ALERT", "APPROVAL Claude is waiting", YELLOW)
    diag_row(image, 174, "QUOTA", "13:01")
    text(image, 10, 224, "<> PAGE 1/2   SELECT EXIT", DIM)
    return image


def device_page() -> Image.Image:
    image, _ = blank()
    text(image, 10, 8, "VOXSTICK / DEVICE", ACCENT_CLAUDE)
    diag_row(image, 30, "FW", "v1.2")
    diag_row(image, 46, "BRIDGE", "0.3.0")
    diag_row(image, 62, "TF", "MOUNTED")
    diag_row(image, 78, "MIC", "OK  SLOT 0  ERR 0")
    diag_row(image, 94, "RMS", "L 214  R 7")
    diag_row(image, 110, "TAKE", "0 KB")
    diag_row(image, 126, "HEAP", "212K  PSRAM 7936K")
    diag_row(image, 142, "BATT", "4.02V  86%")
    diag_row(image, 158, "UPTIME", "1843s")
    diag_row(image, 174, "SLEEP", "DISABLED")
    text(image, 10, 224, "<> PAGE 2/2   SELECT EXIT", DIM)
    return image


def recording(title: str, accent, level: int, caption: str, hint: str) -> Image.Image:
    image, draw = blank()
    text(image, 120 - len(title) * 3 * 3, 40, title, accent, 3)
    weights = (45, 75, 100, 75, 45)
    for index, weight in enumerate(weights):
        height = max(8, min(84, 8 + level * weight // 100))
        x = 48 + index * 30
        draw.rectangle((x, 140 - height // 2, x + 16 - 1, 140 - height // 2 + height - 1), fill=accent)
    text(image, 120 - len(caption) * 3, 196, caption, MID)
    text(image, 120 - len(hint) * 3, 214, hint, DIM)
    return image


def save_png(image: Image.Image, path: Path) -> None:
    """Write via a temporary file: an indexer or viewer can hold the target open
    on Windows, and a half-written preview is worse than a retry."""
    temporary = path.with_suffix(".tmp.png")
    for attempt in range(5):
        try:
            image.save(temporary)
            os.replace(temporary, path)
            return
        except OSError:
            time.sleep(0.3 * (attempt + 1))
    raise SystemExit(f"could not write {path}; close anything viewing {path.parent}")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    pages = {
        "home-claude": home(
            name="Claude", accent=ACCENT_CLAUDE, status="RUNNING", project="voxstick",
            quota_5h=39, quota_7d=62,
        ),
        "home-codex": home(
            name="Codex", accent=ACCENT_CODEX, status="IDLE", project="esp32-launcher",
            quota_5h=20, quota_7d=72, active=False, stale=True,
        ),
        "home-approval": home(
            name="Claude", accent=ACCENT_CLAUDE, status="APPROVAL", project="voxstick",
            quota_5h=39, quota_7d=62,
            message=("Claude is waiting for approval", YELLOW),
        ),
        "home-offline": home(
            name="Claude", accent=ACCENT_CLAUDE, status="OFFLINE", project="-",
            quota_5h=None, quota_7d=None, active=False, clock="--:--",
            right="NO BRIDGE", healthy=False,
            message=("SELECT FOR DIAGNOSTICS", DIM),
        ),
        "home-portal": home(
            name="Claude", accent=ACCENT_CLAUDE, status="OFFLINE", project="-",
            quota_5h=None, quota_7d=None, active=False, right="AP", healthy=False,
            portal=("AP: samestick", "PW: samestick"),
        ),
        "diag-link": link_page(),
        "diag-device": device_page(),
        "recording": recording("LISTENING", ACCENT_CLAUDE, 72, "3.4s / 56s", "RELEASE AI TO SEND"),
    }
    from PIL import ImageDraw, ImageFont
    title_font = ImageFont.load_default(size=26)
    label_font = ImageFont.load_default(size=16)
    small_font = ImageFont.load_default(size=14)
    muted = (108, 108, 98)

    columns = 3
    rows = (len(pages) + columns - 1) // columns
    overview = Image.new("RGB", (20 + columns * 500, 150 + rows * 530), (22, 24, 24))
    draw = ImageDraw.Draw(overview)
    draw.text((20, 18), "VOXSTICK / UI LAYOUT", fill=WHITE, font=title_font)
    draw.text((20, 50), "Sample data, not a device capture. Each screen is 240 x 240.",
              fill=muted, font=small_font)

    for index, (name, image) in enumerate(pages.items()):
        numbered = f"{index + 1:02d}-{name}"
        save_png(image, OUT / f"{numbered}.png")
        x, y = 20 + (index % columns) * 500, 84 + (index // columns) * 530
        draw.text((x, y), numbered.replace("-", " / ", 1).upper(), fill=ACCENT_CLAUDE, font=label_font)
        draw.rectangle((x - 1, y + 25, x + 480, y + 506), outline=muted)
        overview.paste(image.resize((480, 480), Image.Resampling.NEAREST), (x, y + 26))

    draw.text((20, 106 + rows * 530),
              "HOLD AI TALK   |   LEFT/RIGHT PROVIDER   |   SELECT DIAGNOSTICS   |   START WI-FI   |   APP SLEEP",
              fill=muted, font=small_font)
    save_png(overview, OUT / "overview.png")
    print(f"{len(pages)} pages + overview rendered to {OUT}; every string fit inside 240x240")


if __name__ == "__main__":
    main()
