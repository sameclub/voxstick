# VoxStick

[中文](README.zh-CN.md)

![VoxStick screens](docs/preview/overview.png)

A desk terminal for local coding agents, running on the S3AI Game (ESP32-S3)
handheld. It shows whether Codex or Claude Code is working, how much of your
5H / 7D quota is left, and beeps when the state changes. Hold the AI key, talk,
and a bridge on your computer transcribes it and drops the text into the
matching chat box.

Ported from [GaryGaryyy/VibeStick](https://github.com/GaryGaryyy/VibeStick)
(MIT, see [LICENSE-VibeStick](LICENSE-VibeStick)). The original targets M5Stack
StickS3 and macOS; this one targets the S3AI Game (240x240 display, MSM261S
microphone, MAX98357A amplifier) on Windows, macOS and Linux.

**The device never talks to a cloud service.** It only speaks HTTP to the
bridge on your LAN. Speech recognition, quota reads and paste injection all
happen on the computer.

## Layout

```
src/                  firmware (Arduino + Arduino_GFX)
  main.cpp            UI, keys, sleep
  vox_audio.*         16 kHz capture and tone synthesis
  vox_bridge.*        /voxstick.ini parsing, HTTP, JSON
bridge/               cross-platform Python service, standard library only
  src/vox_stick/      the service
  tests/              117 unit tests
  doctor.py           environment self-check
  .env.example        configuration template
voxstick.ini.example  TF card template
```

## 1. Requirements

- An S3AI Game, a TF card, and 2.4 GHz Wi-Fi (the ESP32-S3 has no 5 GHz radio)
- Python 3.11+ on the computer
- A Tencent Cloud account with **Automatic Speech Recognition** enabled, for the
  SecretId / SecretKey. Or skip it and wire your own recogniser, see
  [Transcription](#transcription).
- Optional: to show Claude's 5H / 7D usage, run `claude` once and `/login`

## 2. Start the bridge

```bash
cd bridge
cp .env.example .env     # then fill in the values
PYTHONPATH=src python -m vox_stick
```

Add `--hud` for a small always-on-top status window. `--host` and `--port`
override the defaults.

Check the environment first if anything looks wrong:

```bash
cd bridge
PYTHONPATH=src python doctor.py
```

`doctor.py` prints the LAN address to put in `bridge_host` on the TF card.

### Configuration

`bridge/.env`, all keys prefixed `VOX_STICK_`:

| Key | Meaning |
|---|---|
| `BRIDGE_TOKEN` | Shared secret; the same value goes in `voxstick.ini` |
| `TENCENT_SECRET_ID` / `TENCENT_SECRET_KEY` | Tencent Cloud ASR credentials |
| `ASR_ENGINE` | `16k_zh` (default), `16k_zh_dialect`, `16k_en`, `16k_yue`, `16k_ja` … |
| `PROVIDER` | `auto` (most recently active), `codex`, `claude` |
| `CLAUDE_USAGE` | `on` enables the Claude 5H / 7D read; off by default |
| `AUTO_ENTER` | `1` presses Enter after pasting |
| `BRIDGE_HOST` / `BRIDGE_PORT` | Listen address, default `0.0.0.0:8765` |
| `HUD` | `1` opens the desktop HUD |
| `TRANSCRIBE_CMD` | External recogniser; takes precedence over Tencent |

## 3. Configure the TF card

Copy `voxstick.ini.example` to the root of the card as `voxstick.ini`:

```ini
bridge_host=192.168.1.20      # the computer IP doctor.py printed
bridge_port=8765
bridge_token=<same value as in .env>
auto_sleep_seconds=0          # 0 keeps the screen on as a desk monitor; 10 sleeps
```

## 4. Flash

```bash
pio run -e s3ai-voxstick
python package.py
```

Install `dist/VoxStick-v1.2.bin` with the Launcher SD file browser, or write it
straight to the app partition:

```bash
python -m esptool --chip esp32s3 --port COM3 --baud 921600 \
  write_flash 0x10000 dist/VoxStick-v1.2.bin
```

## 5. Provisioning

Press **START**. Every S3AI app raises the same hotspot:

| Name | Password |
|---|---|
| `samestick` | `samestick` |

Join it from a phone, the page opens by itself (or browse to `192.168.4.1`),
pick a network, enter the password, save. Credentials live in the NVS namespace
`voxstick-net`.

Press START any time to switch networks, even while connected. The device drops
the hotspot to try the new credentials (`TRYING NEW WIFI` on screen); on failure
it restores the previous network and reopens the hotspot under the same name, so
the phone rejoins and shows the error. A failed attempt never destroys working
credentials.

If a saved network stays unreachable for 60 s, the device opens provisioning by
itself on the next boot.

## Keys

| Key | Action |
|---|---|
| AI (GPIO0) | Hold to talk; release to upload, transcribe and paste |
| Left / Right | Home: switch Codex / Claude. Diagnostics: page |
| Y | Refresh quota now (`POST /quota/refresh`) |
| A | Clear the current alert (`POST /event` `button_short`) |
| SELECT | Diagnostics, two pages: LINK (network, errors) and DEVICE (hardware) |
| START | Wi-Fi provisioning on / off |
| APP (GPIO10) | Sleep / wake |

The top right shows link state: `LINK` / `NO BRIDGE` / `NO WIFI` / `AP` /
`WIFI TEST`. The dot beside a provider name marks the bridge's active provider.
Quota turns red at 20% remaining or less; the bar keeps the provider colour
(Claude orange, Codex white). Stale data is flagged only on the diagnostics
QUOTA row.

Home carries agent information only: name, state, project, quota, alerts.
Config errors, HTTP codes, microphone and memory live on the two SELECT pages.

## Alert sounds

Only agent state changes make a sound; recording never does. Matches the
upstream `STATES_AND_SOUNDS.md`:

| State | Sound |
|---|---|
| DONE | 880 Hz 80 ms, 40 ms gap, 1320 Hz 120 ms |
| ERROR | 240 Hz 100 ms, 60 ms gap, three times |
| APPROVAL | 600 Hz 100 ms, 60 ms gap, 800 Hz 100 ms |

One sound per `alert.event_id`; without an event_id it falls back to detecting
state transitions. Alerts that arrive while recording are dropped, not queued.

## Transcription

Tencent Cloud **SentenceRecognition**
([docs](https://cloud.tencent.com/document/api/1093/35646)), TC3-HMAC-SHA256
signing implemented against the standard library, no Tencent SDK.

- The device records 16 kHz / 16-bit mono PCM, capped at **56 s** (the API
  allows 60 s and 3 MB after base64)
- The bridge writes a WAV, then checks for silence and speech windows, and skips
  anything too short or empty rather than spending a call
- On success the bridge focuses the desktop app for the selected provider,
  locates the chat box, pastes with Ctrl+V and restores the clipboard. If the
  target is unavailable the transcript is kept and the failure reported.

To go offline or use another service, set `VOX_STICK_TRANSCRIBE_CMD`. The
command receives the recording session JSON on stdin (including `audio_file`)
and prints the text to stdout. It takes precedence over Tencent.

## Where the agent state comes from

- **Codex**: reads `~/.codex/sessions/**/*.jsonl`. Quota comes from the
  `rate_limits` field of `token_count` events (`window_minutes` 300 maps to 5H,
  10080 to 7D). This is not an official quota API.
- **Claude**: reads `~/.claude/projects/**/*.jsonl` for RUNNING / DONE /
  APPROVAL / ERROR. The 5H / 7D usage read is **off by default**; enabling it
  with `VOX_STICK_CLAUDE_USAGE=on` calls an undocumented endpoint using Claude
  Code's local OAuth credentials and may break at any time. While off, the
  device shows `--%`.

Both check for a running process first, then fall back to whether the session
file was written in the last 10 minutes, because on Windows both CLIs run as
`node.exe` children and cannot be identified by process name.

`/state` always carries both the `codex` and `claude` blocks regardless of
`VOX_STICK_PROVIDER`; `active_provider` only drives the dot on the device.

## HTTP API

The device polls `GET /state` every 2 s; everything else is POST. `/event`,
`/quota/refresh`, `/recording/start`, `/recording/audio` and `/recording/stop`
require the `X-Vox-Stick-Token` header. The protocol is compatible with upstream
v0.1.2, except `audio_source` is now `device_pcm`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `NO /voxstick.ini` on screen | Card not seated, or the file is not in the root |
| `NO WIFI` | Press START to reprovision; confirm the network is 2.4 GHz |
| `NO BRIDGE` | Check the HTTP code on the diagnostics page: 401 is a token mismatch, anything else is usually a firewall or a changed IP |
| `UPLOAD FAILED HTTP 413` | Recording too long, or `VOX_STICK_MAX_RECORDING_AUDIO_BYTES` set too low |
| ASR returns `AuthFailure.SignatureFailure` | Wrong SecretId / SecretKey |
| ASR returns `UnauthorizedOperation` | ASR is not enabled on the Tencent account |
| Recognised but never pasted | The focused window runs as administrator; unprivileged SendInput cannot reach it |
| `Cannot listen` on start | Port taken, or another bridge is already running |
| Claude column stuck at `--%` | Usage read is off; set `VOX_STICK_CLAUDE_USAGE=on` |

## Development

```bash
cd bridge
PYTHONPATH=src python -m unittest discover -s tests   # 117 tests
PYTHONPATH=src python doctor.py

pio run -e s3ai-voxstick
python preview.py        # regenerate docs/preview, after one pio run
```

Dependencies resolve from the PlatformIO registry, and the board definition is
vendored under `boards/` (see [boards/NOTICE.md](boards/NOTICE.md)), so a clean
clone builds without any other checkout.

On Windows, enable long paths before the first build or the toolchain fails to
unpack:

```powershell
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force
```

Run PlatformIO from PowerShell or cmd, not Git Bash or MSYS. The platform
installs its compiler with `idf_tools.py`, which refuses to run under
MSys/Mingw and leaves the build failing on a missing `xtensa-esp32s3-elf-g++`.

## Security

- `.env` and `voxstick.ini` hold secrets. Both are gitignored; keep it that way.
- Cloud ASR sends your audio off the machine.
- The bridge listens on the LAN only. Tokens are compared with
  `hmac.compare_digest`, and a token is mandatory when bound to a non-loopback
  address.
- Recordings stay in `%APPDATA%\VoxStick\Recordings` (macOS
  `~/Library/Application Support/VoxStick`, Linux `~/.local/share/VoxStick`) and
  are never cleaned up automatically.

## Status

The firmware builds and packages, and the bridge's 117 unit tests pass. Audio
quality, sleep current and long-run stability have not been measured on
hardware.

## Related

- [wifi-portal](https://github.com/sameclub/wifi-portal) — the shared provisioning library
- [DotMic](https://github.com/sameclub/dotmic) — push-to-talk USB microphone and clock

## Licence

MIT, see [LICENSE](LICENSE). Ported from
[GaryGaryyy/VibeStick](https://github.com/GaryGaryyy/VibeStick), MIT, see
[LICENSE-VibeStick](LICENSE-VibeStick).
