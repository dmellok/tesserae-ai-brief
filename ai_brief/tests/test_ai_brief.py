"""Smoke tests for AI Brief widget.

Mocks the ai_core peer and Open-Meteo to exercise the request-building,
caching, error-handling, and prompt-resolution flows without hitting
the live Anthropic API.
"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# Load ai_brief's server.py under a unique module name so the ai_core
# sibling's server.py (also named server.py) doesn't collide in
# sys.modules when both run in the same pytest session.
_SPEC = importlib.util.spec_from_file_location(
    "ai_brief_server", Path(__file__).resolve().parent.parent / "server.py"
)
assert _SPEC is not None and _SPEC.loader is not None
server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(server)


# -- helpers ----------------------------------------------------------------


def _ctx(tmp_path: Path) -> dict[str, Any]:
    return {"data_dir": str(tmp_path)}


def _opts(**overrides: Any) -> dict[str, Any]:
    base = {
        "prompt_template": "It is {time.day_of_week}. The weather is {weather.now.condition}.",
        "latitude": 0,
        "longitude": 0,
        "units": "metric",
        "include_calendar": False,
        "ha_entities": "",
        "refresh_minutes": 60,
        "max_tokens": 200,
        "model_override": "",
        "header_label": "BRIEF",
    }
    base.update(overrides)
    return base


def _mock_ai_core(
    *,
    text: str = "It is Saturday and partly cloudy.",
    model: str = "claude-haiku-4-5",
    error: str | None = None,
):
    mock = MagicMock()
    mock.resolve_placeholders.return_value = ("resolved prompt", {"time.day_of_week": "Saturday"})
    if error is not None:
        mock.call_llm.return_value = {"error": error}
    else:
        mock.call_llm.return_value = {"text": text, "model": model}
    return mock


def _fake_app(ai_core: Any = None) -> Any:
    fake = MagicMock()
    registry: dict[str, Any] = {}
    if ai_core is not None:
        registry["ai_core"] = type("FakePlugin", (), {"server_module": ai_core})()
    fake.config = {"PLUGIN_REGISTRY": registry}
    return patch.object(server, "current_app", fake)


# -- empty-input guards ---------------------------------------------------


def test_empty_template_returns_error(tmp_path: Path) -> None:
    with _fake_app(_mock_ai_core()):
        out = server.fetch(_opts(prompt_template=""), {}, ctx=_ctx(tmp_path))
    assert out["error"] == "Prompt template is empty."


def test_missing_ai_core_returns_error(tmp_path: Path) -> None:
    with _fake_app(ai_core=None):
        out = server.fetch(_opts(), {}, ctx=_ctx(tmp_path))
    assert "AI Core" in out["error"]


# -- happy path -----------------------------------------------------------


def test_happy_path_calls_llm_and_returns_brief(tmp_path: Path) -> None:
    ai_core = _mock_ai_core(text="It is sunny.")
    with _fake_app(ai_core):
        out = server.fetch(_opts(), {}, ctx=_ctx(tmp_path))
    assert out["brief"] == "It is sunny."
    assert out["model"] == "claude-haiku-4-5"
    assert out["from_cache"] is False
    ai_core.call_llm.assert_called_once()


def test_llm_error_propagates_with_debug(tmp_path: Path) -> None:
    ai_core = _mock_ai_core(error="HTTP 429: rate_limit_error")
    with _fake_app(ai_core):
        out = server.fetch(_opts(), {}, ctx=_ctx(tmp_path))
    assert out["error"] == "HTTP 429: rate_limit_error"
    assert out["resolved_prompt"] == "resolved prompt"


# -- caching --------------------------------------------------------------


def test_cache_hit_returns_from_cache(tmp_path: Path) -> None:
    ai_core = _mock_ai_core(text="First gen.")
    with _fake_app(ai_core):
        first = server.fetch(_opts(refresh_minutes=60), {}, ctx=_ctx(tmp_path))
        # Second call within the refresh window should hit the cache; the
        # mock would otherwise return the same text but we assert call count.
        ai_core.call_llm.reset_mock()
        second = server.fetch(_opts(refresh_minutes=60), {}, ctx=_ctx(tmp_path))
    assert first["from_cache"] is False
    assert second["from_cache"] is True
    assert second["brief"] == "First gen."
    ai_core.call_llm.assert_not_called()


def test_cache_miss_after_ttl_expiry(tmp_path: Path) -> None:
    ai_core = _mock_ai_core(text="Generated.")
    with _fake_app(ai_core):
        first = server.fetch(_opts(refresh_minutes=5), {}, ctx=_ctx(tmp_path))
        # Backdate the cache mtime past TTL to force a re-call.
        for cache_file in tmp_path.glob("brief_*.json"):
            old = time.time() - (10 * 60)
            import os
            os.utime(cache_file, (old, old))
        ai_core.call_llm.reset_mock()
        second = server.fetch(_opts(refresh_minutes=5), {}, ctx=_ctx(tmp_path))
    assert first["from_cache"] is False
    assert second["from_cache"] is False
    ai_core.call_llm.assert_called_once()


# -- parser ----------------------------------------------------------------


def test_parse_entity_list_handles_string_and_list() -> None:
    assert server._parse_entity_list("a, b\nc") == ["a", "b", "c"]
    assert server._parse_entity_list(["a", "b"]) == ["a", "b"]
    assert server._parse_entity_list(None) == []
    assert server._parse_entity_list("") == []
