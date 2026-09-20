"""HTTP surface: a dispatch table and one thin handler.

Endpoints are declared as data so the protected set and the routing table
cannot drift apart. The handler reaches the session through the server object
rather than a closure, which keeps it importable and testable on its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import parse_qs, urlparse

from vox_stick import __version__ as BRIDGE_VERSION
from vox_stick.server.settings import BRIDGE_NAME, BridgeSettings

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from vox_stick.server.app import BridgeSession

PCM_DEFAULTS = {"sample_rate": 16000, "channels": 1, "bits_per_sample": 16}


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    handler: str
    protected: bool = False


ROUTES: tuple[Route, ...] = (
    Route("GET", "/state", "state"),
    Route("GET", "/health", "health"),
    Route("POST", "/event", "event", protected=True),
    Route("POST", "/quota/refresh", "refresh_quota", protected=True),
    Route("POST", "/recording/start", "recording_start", protected=True),
    Route("POST", "/recording/audio", "recording_audio", protected=True),
    Route("POST", "/recording/stop", "recording_stop", protected=True),
)

_TABLE = {(route.method, route.path): route for route in ROUTES}

def sole(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return values[0] if values else ""


def positive_header(raw: str | None, fallback: int) -> int:
    try:
        value = int(raw or "")
    except ValueError:
        return fallback
    return value if value > 0 else fallback


def stamped(payload: dict[str, Any]) -> dict[str, Any]:
    payload["bridge_name"] = BRIDGE_NAME
    payload["bridge_version"] = BRIDGE_VERSION
    return payload


class BridgeServer(ThreadingHTTPServer):
    """Carries the session and settings so the handler needs no closure."""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        session: "BridgeSession",
        settings: BridgeSettings,
    ) -> None:
        self.session = session
        self.settings = settings
        super().__init__(address, BridgeRequestHandler)


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server_version = "VoxStick/0.1"

    # -- dispatch ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        route = _TABLE.get((method, parsed.path))
        if route is None:
            self._fail(HTTPStatus.NOT_FOUND, "Unknown endpoint")
            return
        if route.protected and self._unauthorized():
            self._fail(HTTPStatus.UNAUTHORIZED, "Unauthorized")
            return
        action: Callable[[Any], None] = getattr(self, f"_on_{route.handler}")
        action(parsed)

    # -- endpoints --------------------------------------------------------

    def _on_state(self, _parsed: Any) -> None:
        self._reply(stamped(self._session.get_state().to_jsonable()))

    def _on_health(self, _parsed: Any) -> None:
        self._reply({"ok": True, "bridge_name": BRIDGE_NAME, "bridge_version": BRIDGE_VERSION})

    def _on_event(self, _parsed: Any) -> None:
        self._reply(self._session.update_from_event(self._json_body()).to_jsonable())

    def _on_refresh_quota(self, _parsed: Any) -> None:
        state = self._session.refresh_quota()
        self._reply({"refreshed": True, "state": state.to_jsonable()})

    def _on_recording_start(self, _parsed: Any) -> None:
        self._reply(self._session.start_recording(self._json_body()))

    def _on_recording_stop(self, _parsed: Any) -> None:
        self._reply(self._session.stop_recording(self._json_body()))

    def _on_recording_audio(self, parsed: Any) -> None:
        limit = self._settings.recording_byte_limit
        declared = self._declared_length()
        if declared > limit:
            self._fail(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"Recording audio exceeds {limit} bytes",
            )
            return
        pcm = self.rfile.read(declared) if declared > 0 else b""
        self._reply(
            self._session.upload_recording_audio(
                pcm,
                session_id=sole(parse_qs(parsed.query), "session_id"),
                sample_rate=positive_header(
                    self.headers.get("X-Vox-Stick-Sample-Rate"), PCM_DEFAULTS["sample_rate"]
                ),
                channels=positive_header(
                    self.headers.get("X-Vox-Stick-Channels"), PCM_DEFAULTS["channels"]
                ),
                bits_per_sample=positive_header(
                    self.headers.get("X-Vox-Stick-Bits-Per-Sample"), PCM_DEFAULTS["bits_per_sample"]
                ),
            )
        )

    # -- plumbing ---------------------------------------------------------

    @property
    def _session(self) -> "BridgeSession":
        return self.server.session  # type: ignore[attr-defined]

    @property
    def _settings(self) -> BridgeSettings:
        return self.server.settings  # type: ignore[attr-defined]

    def _unauthorized(self) -> bool:
        return self._settings.rejects(self.headers.get("X-Vox-Stick-Token", ""))

    def _declared_length(self) -> int:
        try:
            return max(0, int(self.headers.get("Content-Length", "0") or "0"))
        except ValueError:
            return 0

    def _json_body(self) -> dict[str, Any]:
        length = self._declared_length()
        if length == 0:
            return {}
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return body if isinstance(body, dict) else {}

    def _reply(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1")
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, status: HTTPStatus, message: str) -> None:
        self._reply({"error": message}, status=status)

    def log_message(self, fmt: str, *args: object) -> None:
        name = self.headers.get("X-Vox-Stick-Firmware-Name", "-")
        version = self.headers.get("X-Vox-Stick-Firmware-Version", "-")
        transport = self.headers.get("X-Vox-Stick-Firmware-Transport", "-")
        print(
            f"{self.address_string()} - {fmt % args} "
            f"firmware={name}/{version} transport={transport}",
            flush=True,
        )
