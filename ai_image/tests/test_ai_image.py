"""Smoke tests for AI Image (heir to fal_image)."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

_SPEC = importlib.util.spec_from_file_location(
    "ai_image_server", Path(__file__).resolve().parent.parent / "server.py"
)
assert _SPEC is not None and _SPEC.loader is not None
server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(server)


def _ctx(tmp_path: Path) -> dict[str, Any]:
    return {"data_dir": str(tmp_path), "cell_w": 800, "cell_h": 480}


def _opts(**overrides: Any) -> dict[str, Any]:
    base = {
        "prompt": "a watercolor of mountains",
        "style": "none",
        "model": "fal-ai/flux/schnell",
        "negative_prompt": "",
        "eink_friendly": False,
        "refresh_hours": 6,
        "image_width": "",
        "image_height": "",
    }
    base.update(overrides)
    return base


def _mock_ai_core(*, error: str | None = None):
    mock = MagicMock()
    if error:
        mock.fal_call.return_value = {"error": error}
    else:
        mock.fal_call.return_value = {
            "image_url": "/plugins/ai_core/cache/abc.jpg",
            "model": "fal-ai/flux/schnell",
        }
    return mock


def _fake_app(ai_core: Any = None) -> Any:
    fake = MagicMock()
    registry: dict[str, Any] = {}
    if ai_core is not None:
        registry["ai_core"] = type("FakePlugin", (), {"server_module": ai_core})()
    fake.config = {"PLUGIN_REGISTRY": registry, "SETTINGS_STORE": MagicMock()}
    fake.config["SETTINGS_STORE"].get_section.return_value = {"timezone": "UTC"}
    return patch.object(server, "current_app", fake)


def test_empty_prompt_returns_error(tmp_path: Path) -> None:
    with _fake_app(_mock_ai_core()):
        out = server.fetch(_opts(prompt=""), {}, ctx=_ctx(tmp_path))
    assert out["error"] == "Prompt is empty."


def test_missing_ai_core_returns_error(tmp_path: Path) -> None:
    with _fake_app(ai_core=None):
        out = server.fetch(_opts(), {}, ctx=_ctx(tmp_path))
    assert "AI Core" in out["error"]


def test_happy_path_returns_image_url(tmp_path: Path) -> None:
    ai_core = _mock_ai_core()
    with _fake_app(ai_core):
        out = server.fetch(_opts(), {}, ctx=_ctx(tmp_path))
    assert out["image_url"] == "/plugins/ai_core/cache/abc.jpg"
    assert out["model"] == "fal-ai/flux/schnell"
    ai_core.fal_call.assert_called_once()


def test_style_preset_prepends_descriptor(tmp_path: Path) -> None:
    ai_core = _mock_ai_core()
    with _fake_app(ai_core):
        server.fetch(_opts(style="oil_painting"), {}, ctx=_ctx(tmp_path))
    sent_prompt = ai_core.fal_call.call_args[0][0]
    assert sent_prompt.startswith("an oil painting of")


def test_eink_friendly_appends_suffix(tmp_path: Path) -> None:
    ai_core = _mock_ai_core()
    with _fake_app(ai_core):
        server.fetch(_opts(eink_friendly=True), {}, ctx=_ctx(tmp_path))
    sent_prompt = ai_core.fal_call.call_args[0][0]
    assert "high contrast" in sent_prompt


def test_placeholder_substituted_for_time_of_day(tmp_path: Path) -> None:
    """``{time_of_day}`` resolves against the server clock — pick a
    fixed hour by patching _tz_now and assert it lands in the
    prompt."""
    ai_core = _mock_ai_core()
    fixed = datetime(2026, 6, 13, 8, 0, tzinfo=UTC)  # 08:00 -> morning
    with _fake_app(ai_core), patch.object(server, "_tz_now", return_value=fixed):
        server.fetch(
            _opts(prompt="a cat in the {time_of_day}"),
            {},
            ctx=_ctx(tmp_path),
        )
    sent_prompt = ai_core.fal_call.call_args[0][0]
    assert "in the morning" in sent_prompt


def test_multi_line_prompt_rotates_by_bucket(tmp_path: Path) -> None:
    """Two lines + a 6-hour cadence → adjacent 6h buckets should land
    on different lines."""
    ai_core = _mock_ai_core()
    prompt = "monday brief\ntuesday brief"
    with _fake_app(ai_core):
        with patch.object(server, "_bucket_from_refresh_hours", return_value=0):
            server.fetch(_opts(prompt=prompt), {}, ctx=_ctx(tmp_path))
        with patch.object(server, "_bucket_from_refresh_hours", return_value=1):
            server.fetch(_opts(prompt=prompt), {}, ctx=_ctx(tmp_path))
    sent_prompts = [call.args[0] for call in ai_core.fal_call.call_args_list]
    assert any("monday" in p for p in sent_prompts)
    assert any("tuesday" in p for p in sent_prompts)


def test_cache_hits_on_same_bucket(tmp_path: Path) -> None:
    ai_core = _mock_ai_core()
    with _fake_app(ai_core), patch.object(server, "_bucket_from_refresh_hours", return_value=0):
        first = server.fetch(_opts(), {}, ctx=_ctx(tmp_path))
        ai_core.fal_call.reset_mock()
        second = server.fetch(_opts(), {}, ctx=_ctx(tmp_path))
    assert first["from_cache"] is False
    assert second["from_cache"] is True
    assert second["image_url"] == first["image_url"]
    ai_core.fal_call.assert_not_called()


def test_fal_error_returns_with_resolved_prompt(tmp_path: Path) -> None:
    ai_core = _mock_ai_core(error="HTTP 429: rate limit")
    with _fake_app(ai_core):
        out = server.fetch(_opts(), {}, ctx=_ctx(tmp_path))
    assert "rate limit" in out["error"]
    assert "resolved_prompt" in out


def test_round_to_64_clamping() -> None:
    assert server._round_to_64(800) == 832
    assert server._round_to_64(480) == 512
    assert server._round_to_64(0) == 512
    assert server._round_to_64(3000) == 2048


def test_time_of_day_buckets() -> None:
    assert server._time_of_day(7) == "morning"
    assert server._time_of_day(12) == "midday"
    assert server._time_of_day(15) == "afternoon"
    assert server._time_of_day(18) == "evening"
    assert server._time_of_day(21) == "dusk"
    assert server._time_of_day(2) == "night"
