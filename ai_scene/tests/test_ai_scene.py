"""Smoke tests for AI Scene widget.

Mocks the ai_core peer (no live Fal.ai API call) and exercises the
request-building, caching, prompt-resolution, and error paths.
"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

_SPEC = importlib.util.spec_from_file_location(
    "ai_scene_server", Path(__file__).resolve().parent.parent / "server.py"
)
assert _SPEC is not None and _SPEC.loader is not None
server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(server)


# -- helpers ----------------------------------------------------------------


def _ctx(tmp_path: Path, *, home_lat: float = 0.0, home_lon: float = 0.0) -> dict[str, Any]:
    return {"data_dir": str(tmp_path), "home_lat": home_lat, "home_lon": home_lon}


def _opts(**overrides: Any) -> dict[str, Any]:
    base = {
        "prompt_template": "A watercolor of {weather.now.condition} at {time.am_pm}.",
        "model": "fal-ai/flux/schnell",
        "latitude": 0,
        "longitude": 0,
        "units": "metric",
        "include_calendar": False,
        "ha_entities": "",
        "negative_prompt": "",
        "refresh_minutes": 60,
        "image_width": 1024,
        "image_height": 1024,
    }
    base.update(overrides)
    return base


def _mock_ai_core(
    *,
    image_url: str = "https://v3.fal.media/files/abc/def.jpg",
    model: str = "fal-ai/flux/schnell",
    error: str | None = None,
):
    mock = MagicMock()
    mock.resolve_placeholders.return_value = (
        "A watercolor of partly cloudy at AM.",
        {"weather.now.condition": "partly cloudy", "time.am_pm": "AM"},
    )
    if error is not None:
        mock.fal_call.return_value = {"error": error}
    else:
        mock.fal_call.return_value = {"image_url": image_url, "model": model}
    return mock


def _fake_app(ai_core: Any = None) -> Any:
    fake = MagicMock()
    registry: dict[str, Any] = {}
    if ai_core is not None:
        registry["ai_core"] = type("FakePlugin", (), {"server_module": ai_core})()
    fake.config = {"PLUGIN_REGISTRY": registry}
    return patch.object(server, "current_app", fake)


# -- empty / missing-peer guards --------------------------------------------


def test_empty_template_returns_error(tmp_path: Path) -> None:
    with _fake_app(_mock_ai_core()):
        out = server.fetch(_opts(prompt_template=""), {}, ctx=_ctx(tmp_path))
    assert out["error"] == "Prompt template is empty."


def test_missing_ai_core_returns_error(tmp_path: Path) -> None:
    with _fake_app(ai_core=None):
        out = server.fetch(_opts(), {}, ctx=_ctx(tmp_path))
    assert "AI Core" in out["error"]


# -- happy path -------------------------------------------------------------


def test_happy_path_returns_image_url(tmp_path: Path) -> None:
    ai_core = _mock_ai_core(image_url="https://v3.fal.media/files/x/scene.jpg")
    with _fake_app(ai_core):
        out = server.fetch(_opts(), {}, ctx=_ctx(tmp_path))
    assert out["image_url"] == "https://v3.fal.media/files/x/scene.jpg"
    assert out["model"] == "fal-ai/flux/schnell"
    assert out["from_cache"] is False
    ai_core.fal_call.assert_called_once()


def test_fal_error_propagates_with_debug(tmp_path: Path) -> None:
    ai_core = _mock_ai_core(error="HTTP 401: Invalid API key")
    with _fake_app(ai_core):
        out = server.fetch(_opts(), {}, ctx=_ctx(tmp_path))
    assert out["error"] == "HTTP 401: Invalid API key"
    assert out["resolved_prompt"].startswith("A watercolor")
    assert "weather.now.condition" in out["debug_values"]


# -- home location fallback -------------------------------------------------


def test_home_lat_lon_fallback_when_cell_empty(tmp_path: Path) -> None:
    """When the cell's lat/lon are empty (0/0) and ctx provides
    home_lat / home_lon, the resolver should fall back to those."""
    ai_core = _mock_ai_core()
    with _fake_app(ai_core):
        server.fetch(
            _opts(latitude=0, longitude=0),
            {},
            ctx=_ctx(tmp_path, home_lat=-37.8136, home_lon=144.9631),
        )
    # The exact prompt the resolver receives isn't observable from the
    # outside; instead verify that ai_core.resolve_placeholders was
    # called (i.e. fetch made it past the lat/lon check + invoked the
    # data assembly).
    ai_core.resolve_placeholders.assert_called_once()


# -- caching ----------------------------------------------------------------


def test_cache_hit_returns_from_cache(tmp_path: Path) -> None:
    ai_core = _mock_ai_core()
    with _fake_app(ai_core):
        first = server.fetch(_opts(refresh_minutes=60), {}, ctx=_ctx(tmp_path))
        ai_core.fal_call.reset_mock()
        second = server.fetch(_opts(refresh_minutes=60), {}, ctx=_ctx(tmp_path))
    assert first["from_cache"] is False
    assert second["from_cache"] is True
    assert second["image_url"] == first["image_url"]
    ai_core.fal_call.assert_not_called()


def test_cache_miss_after_ttl_expiry(tmp_path: Path) -> None:
    ai_core = _mock_ai_core()
    with _fake_app(ai_core):
        first = server.fetch(_opts(refresh_minutes=5), {}, ctx=_ctx(tmp_path))
        import os
        for cache_file in tmp_path.glob("scene_*.json"):
            old = time.time() - (10 * 60)
            os.utime(cache_file, (old, old))
        ai_core.fal_call.reset_mock()
        second = server.fetch(_opts(refresh_minutes=5), {}, ctx=_ctx(tmp_path))
    assert first["from_cache"] is False
    assert second["from_cache"] is False
    ai_core.fal_call.assert_called_once()


def test_cache_key_changes_with_model(tmp_path: Path) -> None:
    """Switching the model produces a different cache key so the new
    model triggers a fresh fal_call instead of returning the previous
    model's image."""
    ai_core = _mock_ai_core(image_url="https://flux.example/img.jpg")
    with _fake_app(ai_core):
        first = server.fetch(_opts(model="fal-ai/flux/schnell"), {}, ctx=_ctx(tmp_path))
        ai_core.fal_call.return_value = {"image_url": "https://sdxl.example/img.jpg", "model": "fal-ai/fast-sdxl"}
        ai_core.fal_call.reset_mock()
        second = server.fetch(_opts(model="fal-ai/fast-sdxl"), {}, ctx=_ctx(tmp_path))
    assert first["image_url"] != second["image_url"]
    ai_core.fal_call.assert_called_once()
