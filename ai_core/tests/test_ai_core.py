"""Smoke tests for AI Core.

Doesn't hit the live Anthropic API. Mocks urlopen and current_app to
exercise placeholder resolution, the LLM-call request shape, and the
error paths (missing key, HTTP error, malformed response).
"""

from __future__ import annotations

import importlib.util
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

# Load this widget's server.py under a unique module name so the
# ai_brief sibling's server.py (also named server.py) doesn't collide
# in sys.modules when both run in the same pytest session.
_SPEC = importlib.util.spec_from_file_location(
    "ai_core_server", Path(__file__).resolve().parent.parent / "server.py"
)
assert _SPEC is not None and _SPEC.loader is not None
server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(server)


# -- placeholder resolver ---------------------------------------------------


def test_resolve_unknown_root_returns_unknown() -> None:
    with _fake_app(timezone="UTC"):
        out, debug = server.resolve_placeholders("hi {bogus.path}")
    assert out == "hi unknown"
    assert debug == {"bogus.path": "unknown"}


def test_resolve_time_placeholders() -> None:
    fixed = datetime(2026, 6, 13, 9, 30, tzinfo=ZoneInfo("UTC"))
    with _fake_app(timezone="UTC"), patch.object(server, "_tz_now", return_value=fixed):
        out, debug = server.resolve_placeholders(
            "{time.day_of_week} {time.hour}:00 ({time.am_pm}) morning={time.is_morning}"
        )
    assert out == "Saturday 09:00 (AM) morning=true"
    assert debug["time.is_morning"] == "true"


def test_resolve_weather_nested_dict() -> None:
    weather = {"now": {"condition": "partly cloudy", "temp_c": "14"}}
    with _fake_app(timezone="UTC"):
        out, _ = server.resolve_placeholders(
            "{weather.now.condition} {weather.now.temp_c}C",
            weather=weather,
        )
    assert out == "partly cloudy 14C"


def test_resolve_missing_weather_data_returns_unknown() -> None:
    with _fake_app(timezone="UTC"):
        out, _ = server.resolve_placeholders("{weather.now.temp_c}")
    assert out == "unknown"


def test_resolve_ha_entity_with_allow_list() -> None:
    """Allow-listed entity reads via the ha_core peer's get_state."""
    fake_ha_core = MagicMock()
    fake_ha_core.get_state.return_value = {"state": "21.5", "attributes": {}}
    with _fake_app(timezone="UTC", peers={"ha_core": fake_ha_core}):
        out, _ = server.resolve_placeholders(
            "{ha.entity.sensor.living_room_temp.state}",
            ha_allow=["sensor.living_room_temp"],
        )
    assert out == "21.5"
    fake_ha_core.get_state.assert_called_once_with("sensor.living_room_temp")


def test_resolve_ha_entity_not_in_allow_list_returns_unknown() -> None:
    """Same lookup with empty allow list never reaches ha_core."""
    fake_ha_core = MagicMock()
    with _fake_app(timezone="UTC", peers={"ha_core": fake_ha_core}):
        out, _ = server.resolve_placeholders(
            "{ha.entity.sensor.living_room_temp.state}",
            ha_allow=[],
        )
    assert out == "unknown"
    fake_ha_core.get_state.assert_not_called()


# -- call_llm -----------------------------------------------------------


def test_call_llm_without_api_key_returns_error() -> None:
    with _fake_app(timezone="UTC", settings={}):
        out = server.call_llm("hello")
    assert out["error"].startswith("Anthropic API key not set")


def test_call_llm_happy_path_returns_text() -> None:
    body = json.dumps(
        {
            "content": [{"type": "text", "text": "Hello back."}],
            "model": "claude-haiku-4-5-20251001",
        }
    ).encode("utf-8")
    with (
        _fake_app(timezone="UTC", settings={"api_key_secret": "sk-test"}),
        patch.object(server.urllib.request, "urlopen", return_value=_fake_resp(body)),
    ):
        out = server.call_llm("hello", max_tokens=50)
    assert out["text"] == "Hello back."
    assert out["model"] == "claude-haiku-4-5-20251001"


def test_call_llm_http_error_returns_error() -> None:
    import urllib.error

    err = urllib.error.HTTPError(
        url=server.ENDPOINT,
        code=429,
        msg="Too Many Requests",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(
            json.dumps({"error": {"message": "rate_limit_error"}}).encode("utf-8")
        ),
    )
    with (
        _fake_app(timezone="UTC", settings={"api_key_secret": "sk-test"}),
        patch.object(server.urllib.request, "urlopen", side_effect=err),
    ):
        out = server.call_llm("hello")
    assert "429" in out["error"]
    assert "rate_limit_error" in out["error"]


def test_call_llm_empty_response_returns_error() -> None:
    body = json.dumps({"content": []}).encode("utf-8")
    with (
        _fake_app(timezone="UTC", settings={"api_key_secret": "sk-test"}),
        patch.object(server.urllib.request, "urlopen", return_value=_fake_resp(body)),
    ):
        out = server.call_llm("hello")
    assert out["error"] == "Empty response from model"


# -- helpers ----------------------------------------------------------------


class _FakeResp:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def read(self) -> bytes:
        return self._payload


def _fake_resp(body: bytes) -> _FakeResp:
    return _FakeResp(body)


class _FakeStore:
    def __init__(self, sections: dict[str, dict[str, Any]]) -> None:
        self._sections = sections

    def get_section(self, name: str) -> dict[str, Any]:
        return self._sections.get(name, {})


def _fake_app(
    *,
    timezone: str = "UTC",
    settings: dict[str, Any] | None = None,
    peers: dict[str, Any] | None = None,
):
    """Context manager that fakes Flask's current_app for unit tests.

    Patches ``server.current_app.config`` so the test doesn't need a
    real Flask app or app_context push.
    """
    sections = {
        "server": {"timezone": timezone},
        "plugins": {"ai_core": settings or {}},
    }
    registry = {
        plugin_id: type("FakePlugin", (), {"server_module": module})()
        for plugin_id, module in (peers or {}).items()
    }
    config = {
        "SETTINGS_STORE": _FakeStore(sections),
        "PLUGIN_REGISTRY": registry,
    }
    fake_current_app = MagicMock()
    fake_current_app.config = config
    return patch.object(server, "current_app", fake_current_app)
