"""Smoke tests for AI Core.

Doesn't hit the live Anthropic API. Mocks urlopen and current_app to
exercise placeholder resolution, the LLM-call request shape, and the
error paths (missing key, HTTP error, malformed response).
"""

from __future__ import annotations

import importlib.util
import io
import json

import pytest
from datetime import datetime, timedelta
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


def test_tz_now_with_iana_zone_returns_aware_dt_in_that_zone() -> None:
    """When app.timezone is a real IANA name, _tz_now reads from that
    zone — regression test for v0.1.x / v0.2.0 reading the wrong
    section name ('server' instead of 'app') and silently falling
    back to UTC."""
    with _fake_app(timezone="Australia/Melbourne"):
        now = server._tz_now()
    assert now.tzinfo is not None
    # zoneinfo lookups return a ZoneInfo object whose str() is the IANA name.
    assert str(now.tzinfo) == "Australia/Melbourne"


def test_tz_now_with_system_sentinel_returns_host_local() -> None:
    """``'system'`` (Tesserae's "no override, use the host's wall
    clock" sentinel) falls back to ``datetime.now().astimezone()`` so
    the returned dt is timezone-aware but in the host's zone."""
    with _fake_app(timezone="system"):
        now = server._tz_now()
    assert now.tzinfo is not None
    assert str(now.tzinfo) != "UTC"  # asserts not the broken UTC fallback


def test_tz_now_with_unknown_zone_falls_back_to_host_local() -> None:
    """An invalid IANA name doesn't crash; falls back to host-local
    same as 'system'."""
    with _fake_app(timezone="Not/A/Real/Zone"):
        now = server._tz_now()
    assert now.tzinfo is not None


# -- usage log + cost helpers --------------------------------------------


def test_anthropic_cost_uses_correct_per_mtok_rate() -> None:
    # Haiku is $1/Mtok in, $5/Mtok out.
    cost = server._anthropic_cost("claude-haiku-4-5", 300, 150)
    assert abs(cost - (300 / 1_000_000 * 1.00 + 150 / 1_000_000 * 5.00)) < 1e-9


def test_anthropic_cost_unknown_model_falls_back_to_haiku() -> None:
    cost = server._anthropic_cost("some-future-model", 1_000_000, 0)
    assert cost == 1.00  # Haiku input rate fallback


def test_fal_cost_lookup() -> None:
    assert server._fal_cost("fal-ai/flux/schnell") == 0.003
    assert server._fal_cost("fal-ai/recraft-v3") == 0.04


def test_fal_cost_unknown_model_falls_back_to_cheapest() -> None:
    assert server._fal_cost("not-a-real-model") == 0.003


def test_log_usage_writes_jsonl_with_timestamp(tmp_path: Path) -> None:
    """``_log_usage`` should append a single JSON line to
    ``data_dir/usage.jsonl`` with an ISO-8601 ``ts`` field stamped at
    write time."""
    fake_plugin = type("P", (), {"data_dir": tmp_path})()
    fake = MagicMock()
    fake.config = {"PLUGIN_REGISTRY": {"ai_core": fake_plugin}}
    with patch.object(server, "current_app", fake):
        server._log_usage({"provider": "anthropic", "model": "x", "cost_usd": 0.001})
    log = tmp_path / "usage.jsonl"
    assert log.exists()
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["provider"] == "anthropic"
    assert rows[0]["cost_usd"] == 0.001
    assert "T" in rows[0]["ts"] and rows[0]["ts"].endswith("Z")


def test_log_usage_silent_when_data_dir_missing() -> None:
    """No data_dir → log call is a no-op. Live render path must not
    raise on a fresh install before settings_store wires the plugin."""
    fake = MagicMock()
    fake.config = {"PLUGIN_REGISTRY": {}}
    with patch.object(server, "current_app", fake):
        server._log_usage({"provider": "anthropic", "model": "x", "cost_usd": 0.001})
    # No exception raised, nothing else to assert.


def test_iter_usage_records_filters_to_window(tmp_path: Path) -> None:
    log = tmp_path / "usage.jsonl"
    old_ts = (datetime(2026, 1, 1, tzinfo=server.UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_ts = datetime.now(server.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.write_text(
        f'{{"ts":"{old_ts}","provider":"anthropic","model":"x","cost_usd":0.5}}\n'
        f'{{"ts":"{new_ts}","provider":"fal","model":"y","cost_usd":0.04}}\n'
    )
    fake_plugin = type("P", (), {"data_dir": tmp_path})()
    fake = MagicMock()
    fake.config = {"PLUGIN_REGISTRY": {"ai_core": fake_plugin}}
    with patch.object(server, "current_app", fake):
        recent = server._iter_usage_records(days=30)
    assert [r["provider"] for r in recent] == ["fal"]  # old one filtered out


def test_iter_usage_records_drops_corrupt_lines(tmp_path: Path) -> None:
    log = tmp_path / "usage.jsonl"
    new_ts = datetime.now(server.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.write_text(
        "not json at all\n"
        f'{{"ts":"{new_ts}","provider":"anthropic","model":"x","cost_usd":0.001}}\n'
        '{"ts":"not-a-timestamp","provider":"fal","model":"y","cost_usd":0.04}\n'
    )
    fake_plugin = type("P", (), {"data_dir": tmp_path})()
    fake = MagicMock()
    fake.config = {"PLUGIN_REGISTRY": {"ai_core": fake_plugin}}
    with patch.object(server, "current_app", fake):
        recent = server._iter_usage_records(days=30)
    assert len(recent) == 1
    assert recent[0]["provider"] == "anthropic"


def test_aggregate_usage_totals_and_projection() -> None:
    """Synthesize three records and confirm totals + projection match."""
    now = datetime(2026, 6, 13, 12, 0, tzinfo=server.UTC)
    records = [
        {
            "_ts": now - timedelta(days=2),
            "provider": "anthropic",
            "model": "claude-haiku-4-5",
            "input_tokens": 300,
            "output_tokens": 150,
            "cost_usd": 0.001,
        },
        {
            "_ts": now - timedelta(hours=2),
            "provider": "anthropic",
            "model": "claude-haiku-4-5",
            "input_tokens": 300,
            "output_tokens": 150,
            "cost_usd": 0.001,
        },
        {
            "_ts": now - timedelta(hours=1),
            "provider": "fal",
            "model": "fal-ai/flux/schnell",
            "image_count": 1,
            "cost_usd": 0.003,
        },
    ]
    agg = server._aggregate_usage(records, now=now)
    assert agg["totals"]["spend_today"] == pytest.approx(0.004, abs=1e-6)
    assert agg["totals"]["calls_today"] == 2
    assert agg["totals"]["spend_month"] == pytest.approx(0.005, abs=1e-6)
    assert agg["totals"]["calls_month"] == 3
    # Projection: 7-day rate is 0.005 / 7 per day → 30 days = ~0.021.
    # ``projection_monthly`` is rounded to 2 decimal places for display,
    # so the assertion tolerance covers the rounding loss.
    assert agg["projection_monthly"] == pytest.approx(0.005 / 7 * 30, abs=0.005)
    # Per-model breakdown is sorted by spend desc. Flux Schnell ($0.003)
    # outspends the two Haiku calls ($0.001 each = $0.002).
    assert agg["model_breakdown"][0]["model"] == "fal-ai/flux/schnell"
    assert agg["model_breakdown"][0]["calls"] == 1
    haiku = next(m for m in agg["model_breakdown"] if m["model"] == "claude-haiku-4-5")
    assert haiku["calls"] == 2


def test_aggregate_usage_empty_log_returns_zero_projection() -> None:
    agg = server._aggregate_usage([], now=datetime(2026, 6, 13, tzinfo=server.UTC))
    assert agg["totals"]["spend_today"] == 0.0
    assert agg["projection_monthly"] == 0.0
    assert agg["model_breakdown"] == []
    # Daily series should still have 30 zero-filled days for the chart axis.
    assert len(agg["daily_series"]) == 30
    assert all(d["calls"] == 0 for d in agg["daily_series"])


def test_aggregate_daily_series_continuous_30_days() -> None:
    """Even if only one day has data, the chart axis spans 30 days
    with zero-filled buckets so Chart.js doesn't compress weeks of
    missing days into a single tick."""
    now = datetime(2026, 6, 13, tzinfo=server.UTC)
    records = [
        {
            "_ts": now - timedelta(days=15),
            "provider": "anthropic",
            "model": "claude-haiku-4-5",
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.01,
        }
    ]
    agg = server._aggregate_usage(records, now=now)
    assert len(agg["daily_series"]) == 30
    nonzero = [d for d in agg["daily_series"] if d["anthropic"] > 0]
    assert len(nonzero) == 1
    assert nonzero[0]["anthropic"] == 0.01


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


# -- fal_call -----------------------------------------------------------


def test_fal_call_without_key_returns_error() -> None:
    with _fake_app(timezone="UTC", settings={}):
        out = server.fal_call("a sunset")
    assert out["error"].startswith("Fal.ai API key not set")


def test_fal_call_happy_path_returns_image_url() -> None:
    body = json.dumps(
        {
            "images": [{"url": "https://v3.fal.media/files/x/scene.jpg"}],
            "seed": 42,
        }
    ).encode("utf-8")
    with (
        _fake_app(timezone="UTC", settings={"fal_api_key_secret": "fal-test"}),
        patch.object(server.urllib.request, "urlopen", return_value=_fake_resp(body)),
    ):
        out = server.fal_call("a sunset", model="fal-ai/flux/schnell", width=1024, height=1024)
    assert out["image_url"] == "https://v3.fal.media/files/x/scene.jpg"
    assert out["model"] == "fal-ai/flux/schnell"


def test_fal_call_http_error_returns_error() -> None:
    import urllib.error

    err = urllib.error.HTTPError(
        url=server.FAL_API_BASE,
        code=401,
        msg="Unauthorized",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(json.dumps({"detail": "Invalid API key"}).encode("utf-8")),
    )
    with (
        _fake_app(timezone="UTC", settings={"fal_api_key_secret": "fal-test"}),
        patch.object(server.urllib.request, "urlopen", side_effect=err),
    ):
        out = server.fal_call("hello")
    assert "401" in out["error"]
    assert "Invalid API key" in out["error"]


def test_fal_call_no_image_returns_error() -> None:
    body = json.dumps({"timings": {"inference": 0.5}}).encode("utf-8")
    with (
        _fake_app(timezone="UTC", settings={"fal_api_key_secret": "fal-test"}),
        patch.object(server.urllib.request, "urlopen", return_value=_fake_resp(body)),
    ):
        out = server.fal_call("hello")
    assert "did not contain an image URL" in out["error"]


def test_fal_call_legacy_single_image_shape() -> None:
    """Older Fal endpoints return ``{"image": {"url": "..."}}`` instead
    of an images list."""
    body = json.dumps(
        {"image": {"url": "https://v3.fal.media/legacy.jpg"}}
    ).encode("utf-8")
    with (
        _fake_app(timezone="UTC", settings={"fal_api_key_secret": "fal-test"}),
        patch.object(server.urllib.request, "urlopen", return_value=_fake_resp(body)),
    ):
        out = server.fal_call("hello")
    assert out["image_url"] == "https://v3.fal.media/legacy.jpg"


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
        "app": {"timezone": timezone},
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
