"""Check that the bridge host is ready. Run: python bridge/doctor.py"""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from vox_stick.claude.usage import resolve_token, usage_enabled  # noqa: E402
from vox_stick.config.dotenv import load_env  # noqa: E402
from vox_stick.config.paths import APP_SUPPORT_DIR, RECORDINGS_DIR  # noqa: E402
from vox_stick.hostenv.process import command_lines  # noqa: E402
from vox_stick.paste.input_injector import PasteInjector  # noqa: E402
from vox_stick.providers.claude import PROJECTS_DIR, observe_claude  # noqa: E402
from vox_stick.codex.local_observer import SESSIONS_DIR  # noqa: E402
from vox_stick.providers.codex import observe_codex  # noqa: E402

PORT = int(os.environ.get("VOX_STICK_PORT", "8765"))

failures = 0
warnings = 0


def check(required: bool, ok: bool, label: str, detail: str = "") -> None:
    global failures, warnings
    if ok:
        mark = "PASS"
    elif required:
        mark = "FAIL"
        failures += 1
    else:
        mark = "WARN"
        warnings += 1
    print(f"[{mark}] {label}" + (f" - {detail}" if detail else ""))


def lan_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("223.5.5.5", 80))
        return probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()


def main() -> int:
    env_file = load_env()
    print(f"VoxStick doctor  python={sys.version.split()[0]}  platform={sys.platform}")
    print(f"config: {env_file or 'no .env found (using process environment)'}")
    print(f"state:  {APP_SUPPORT_DIR}")
    print()

    check(True, sys.version_info >= (3, 11), "Python 3.11+", sys.version.split()[0])

    token = os.environ.get("VOX_STICK_BRIDGE_TOKEN", "").strip()
    check(True, len(token) >= 16, "VOX_STICK_BRIDGE_TOKEN set", f"{len(token)} chars")

    secret_id = os.environ.get("VOX_STICK_TENCENT_SECRET_ID") or os.environ.get("TENCENTCLOUD_SECRET_ID") or ""
    secret_key = os.environ.get("VOX_STICK_TENCENT_SECRET_KEY") or os.environ.get("TENCENTCLOUD_SECRET_KEY") or ""
    transcribe_cmd = os.environ.get("VOX_STICK_TRANSCRIBE_CMD", "").strip()
    check(
        True,
        bool((secret_id and secret_key) or transcribe_cmd),
        "ASR configured",
        "local command" if transcribe_cmd else ("Tencent SentenceRecognition" if secret_id else "missing"),
    )

    injector = PasteInjector()
    check(True, injector.backend_name != "none", "Paste backend", injector.backend_name)

    address = lan_ip()
    check(True, bool(address), "LAN address", address or "no route found")
    if address:
        print(f"       -> put bridge_host={address} in /voxstick.ini on the TF card")

    try:
        # A configured HTTP_PROXY would otherwise swallow the loopback probe and
        # report a healthy bridge as down.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{PORT}/health", timeout=2) as response:
            health = json.loads(response.read(10_000).decode("utf-8"))
        check(False, bool(health.get("ok")), "Bridge responding", f"v{health.get('bridge_version')}")
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        check(False, False, "Bridge responding", f"nothing on 127.0.0.1:{PORT}")

    check(False, bool(command_lines()), "Process probe", f"{len(command_lines())} processes visible")

    check(False, SESSIONS_DIR.is_dir(), "Codex sessions", str(SESSIONS_DIR))
    check(False, PROJECTS_DIR.is_dir(), "Claude projects", str(PROJECTS_DIR))

    root = Path(os.environ.get("VOX_STICK_PROJECT_ROOT") or Path.cwd())
    codex = observe_codex(root)
    claude = observe_claude(root)
    print(f"       codex  status={codex.status.value} online={codex.online}")
    print(f"       claude status={claude.status.value} online={claude.online}")

    if usage_enabled():
        check(False, resolve_token() is not None, "Claude usage credential", "run `claude` then /login")
    else:
        print("[SKIP] Claude usage disabled (VOX_STICK_CLAUDE_USAGE=off)")

    try:
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        probe = RECORDINGS_DIR / ".doctor-probe"
        probe.write_bytes(b"ok")
        probe.unlink()
        check(True, True, "Recordings directory writable", str(RECORDINGS_DIR))
    except OSError as exc:
        check(True, False, "Recordings directory writable", str(exc))

    print()
    print(f"{failures} failed, {warnings} warnings")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
