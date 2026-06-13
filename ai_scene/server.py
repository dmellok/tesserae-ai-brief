"""ai_scene, data-aware Fal.ai image generation.

Builds the same context snapshot ai_brief uses (Open-Meteo weather,
calendar_core events, ha_core entity state, server clock), resolves
{placeholders} in the user's prompt template via ai_core, then sends
the resolved prompt to Fal.ai. The returned image URL is cached per
``(resolved_prompt, model, dimensions)`` so the same prompt + same
context doesn't burn a fresh generation on every render.

The widget renders full-bleed (``render.full_bleed: true``), so the
renderer treats the cell as a single image surface. ``dither:
floyd-steinberg`` carries the cell through Tesserae's quantize step
for e-ink panels.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from flask import current_app

WEATHER_CACHE_TTL_S = 600
HTTP_TIMEOUT_S = 15
USER_AGENT = "tesserae/0.1 (+ai_scene)"


# ---------------------------------------------------------------- helpers


def _peer(plugin_id: str) -> Any:
    plugin = current_app.config["PLUGIN_REGISTRY"].get(plugin_id)
    return plugin.server_module if plugin is not None else None


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_entity_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if not raw:
        return []
    return [tok.strip() for tok in str(raw).replace("\n", ",").split(",") if tok.strip()]


def _round_to_64(n: int, *, minimum: int = 512, maximum: int = 2048) -> int:
    """Round ``n`` up to the next multiple of 64, clamped to a Fal-
    friendly range. Most Fal image models (Flux variants, SDXL,
    Recraft) require dimensions to be multiples of 64 in [512, 2048].
    Smaller crashes some Flux variants with a model-input error;
    larger spikes cost + latency without quality gain at most cell
    sizes. Rounding UP rather than nearest so the image never has
    less resolution than the cell needs."""
    if n <= 0:
        return minimum
    rounded = ((int(n) + 63) // 64) * 64
    return max(minimum, min(maximum, rounded))


def _request_dims(
    options: dict[str, Any], ctx: dict[str, Any]
) -> tuple[int, int]:
    """Resolve the (width, height) Fal should generate at.

    Priority:
      1. Explicit ``image_width`` / ``image_height`` on the cell, if set.
      2. The cell's actual pixel size from ``ctx`` (``cell_w`` /
         ``cell_h``), rounded UP to the next multiple of 64.
      3. Square 1024x1024 fallback if neither is available (preview
         outside a composition).

    Without this, every generation defaulted to 1024x1024 and either
    got stretched or aggressively cropped to fit the cell, which is
    the whole point of telling Fal what aspect ratio to render at.
    """
    explicit_w = int(options.get("image_width") or 0)
    explicit_h = int(options.get("image_height") or 0)
    cell_w = int(ctx.get("cell_w") or 0)
    cell_h = int(ctx.get("cell_h") or 0)
    width = explicit_w if explicit_w > 0 else _round_to_64(cell_w or 1024)
    height = explicit_h if explicit_h > 0 else _round_to_64(cell_h or 1024)
    return width, height


def _http_json(url: str) -> dict[str, Any] | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------- weather


_WEATHER_CODES = {
    0: "clear sky",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "rime fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    80: "rain showers",
    81: "rain showers",
    82: "violent rain showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}


def _fetch_weather(
    lat: float, lon: float, units: str, data_dir: Path
) -> dict[str, Any] | None:
    if lat == 0.0 and lon == 0.0:
        return None
    cache_path = data_dir / f"wx_{lat:.3f}_{lon:.3f}_{units}.json"
    if cache_path.exists() and time.time() - cache_path.stat().st_mtime < WEATHER_CACHE_TTL_S:
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    temp_unit = "fahrenheit" if units == "imperial" else "celsius"
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,weather_code,apparent_temperature,relative_humidity_2m"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        f"&temperature_unit={temp_unit}"
        "&forecast_days=1&timezone=auto"
    )
    payload = _http_json(url)
    if not isinstance(payload, dict):
        return None

    current = payload.get("current") or {}
    daily = payload.get("daily") or {}

    def _first(arr: object) -> Any:
        if isinstance(arr, list) and arr:
            return arr[0]
        return None

    code = current.get("weather_code")
    snapshot = {
        "now": {
            "condition": _WEATHER_CODES.get(
                int(code) if isinstance(code, int | float) else -1, "unknown"
            ),
            "temp_c": _round(current.get("temperature_2m")),
            "feels_like_c": _round(current.get("apparent_temperature")),
            "humidity_pct": _round(current.get("relative_humidity_2m")),
        },
        "today": {
            "high_c": _round(_first(daily.get("temperature_2m_max"))),
            "low_c": _round(_first(daily.get("temperature_2m_min"))),
            "precip_pct": _round(_first(daily.get("precipitation_probability_max"))),
        },
    }
    with contextlib.suppress(OSError):
        cache_path.write_text(json.dumps(snapshot), encoding="utf-8")
    return snapshot


def _round(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return f"{round(float(value))}"
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- calendar


def _fetch_calendar() -> dict[str, Any] | None:
    """calendar_core.load_events signature is
    ``(feed_ids: list[str] | None, start: datetime, end: datetime)``.
    Passing ``None`` for feed_ids includes every enabled feed."""
    core = _peer("calendar_core")
    if core is None or not hasattr(core, "load_events"):
        return None
    now = datetime.now(UTC)
    window_end = now + timedelta(hours=24)
    try:
        events = core.load_events(None, now, window_end)
    except Exception:
        return None
    if not isinstance(events, list):
        return None

    today_count = 0
    next_event: dict[str, Any] | None = None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        start_raw = ev.get("start") or ev.get("start_iso")
        if not start_raw:
            continue
        try:
            start_dt = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=UTC)
        if start_dt.date() == now.date():
            today_count += 1
        if start_dt > now and next_event is None:
            mins = max(0, int((start_dt - now).total_seconds() // 60))
            next_event = {"title": str(ev.get("title") or "untitled"), "in_minutes": str(mins)}
    return {
        "events_today": str(today_count),
        "next": next_event or {"title": "nothing scheduled", "in_minutes": "0"},
    }


# ---------------------------------------------------------------- fetch


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings
    template = str(options.get("prompt_template") or "").strip()
    if not template:
        return {"error": "Prompt template is empty."}

    ai_core = _peer("ai_core")
    if ai_core is None:
        return {"error": "AI Core plugin not installed."}

    lat = float(options.get("latitude") or ctx.get("home_lat") or 0.0)
    lon = float(options.get("longitude") or ctx.get("home_lon") or 0.0)
    units = str(options.get("units") or "metric")
    include_calendar = options.get("include_calendar", True) is not False
    ha_allow = _parse_entity_list(options.get("ha_entities"))
    refresh_minutes = max(5, int(options.get("refresh_minutes") or 60))
    model = (str(options.get("model") or "").strip()) or "fal-ai/flux/schnell"
    width, height = _request_dims(options, ctx)
    negative_prompt = str(options.get("negative_prompt") or "").strip()
    scale = (str(options.get("scale") or "").strip()) or "fill"

    data_dir = Path(ctx["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)

    weather_snapshot = _fetch_weather(lat, lon, units, data_dir)
    calendar_snapshot = _fetch_calendar() if include_calendar else None

    resolved_prompt, debug_values = ai_core.resolve_placeholders(
        template,
        weather=weather_snapshot,
        calendar=calendar_snapshot,
        ha_allow=ha_allow,
    )

    cache_key = hashlib.sha256(
        f"{resolved_prompt}|{model}|{width}x{height}|{negative_prompt}".encode()
    ).hexdigest()[:16]
    cache_path = data_dir / f"scene_{cache_key}.json"
    refresh_seconds = refresh_minutes * 60
    if cache_path.exists() and time.time() - cache_path.stat().st_mtime < refresh_seconds:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            cached["from_cache"] = True
            return cached
        except (json.JSONDecodeError, OSError):
            pass

    result = ai_core.fal_call(
        resolved_prompt,
        model=model,
        width=width,
        height=height,
        negative_prompt=negative_prompt,
    )
    if result.get("error"):
        return {
            "error": result["error"],
            "resolved_prompt": resolved_prompt,
            "debug_values": debug_values,
        }

    response = {
        "image_url": result.get("image_url"),
        "model": result.get("model", model),
        "generated_at": _utcnow_iso(),
        "resolved_prompt": resolved_prompt,
        "debug_values": debug_values,
        "scale": scale,
        "from_cache": False,
    }
    with contextlib.suppress(OSError):
        cache_path.write_text(json.dumps(response), encoding="utf-8")
    return response
