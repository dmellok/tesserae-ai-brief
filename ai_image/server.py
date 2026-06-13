"""ai_image, ambient AI-generated image widget.

Heir to the legacy ``fal_image`` plugin. Differences from the original:

  * Reads the Fal.ai API key from AI Core (Settings -> Plugins -> AI
    Core), so a single key works across ai_brief / ai_scene / ai_image.
  * Routes every call through ``ai_core.fal_call``, which logs to
    ``usage.jsonl`` and surfaces in the AI Core admin dashboard.
  * Returns the locally-cached image URL (no Fal-CDN sandboxed-CSP
    surprises).
  * Same prompt + same time-bucket = no API call (the bucket-derived
    cache key keeps refresh costs predictable).

What it does NOT do that ``ai_scene`` does: read live weather, calendar,
or HA state. ai_image is the ambient-art widget; ai_scene is the data-
reflective one. They share the bundle so you can install either.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from flask import current_app

USER_AGENT = "tesserae/0.1 (+ai_image)"

# Prepended style descriptors. Mirror fal_image's preset names so a
# user migrating from the legacy widget can copy their config 1:1.
_STYLE_PREFIXES: dict[str, str] = {
    "none": "",
    "oil_painting": "an oil painting of",
    "watercolor": "a soft watercolor illustration of",
    "pencil_sketch": "a pencil sketch of",
    "pixel_art": "16-bit pixel art of",
    "cyberpunk": "a cyberpunk illustration of",
    "botanical": "a botanical illustration of",
    "bauhaus": "a Bauhaus geometric poster of",
    "risograph": "a risograph print of",
    "line_art": "a minimal line drawing of",
    "ukiyo_e": "a ukiyo-e woodblock print of",
    "art_deco": "an art deco poster of",
}

# Trailing hint that nudges the model toward dithering-friendly output.
_EINK_SUFFIX = (
    " high contrast, limited palette, simple composition, painterly, "
    "designed for printing"
)


_MOON_PHASES = (
    "new moon",
    "waxing crescent",
    "first quarter",
    "waxing gibbous",
    "full moon",
    "waning gibbous",
    "last quarter",
    "waning crescent",
)


def _time_of_day(hour: int) -> str:
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 14:
        return "midday"
    if 14 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 20:
        return "evening"
    if 20 <= hour < 23:
        return "dusk"
    return "night"


def _season(month: int) -> str:
    # Northern-hemisphere mapping; the user can override by writing
    # the season they want in the prompt directly.
    if month in (12, 1, 2):
        return "winter"
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    return "autumn"


def _moon_phase(now: datetime) -> str:
    """Approximate lunar phase from a reference new moon at
    2000-01-06. Good enough for prompt flavour; not an astronomy
    library."""
    reference = datetime(2000, 1, 6, 18, 14, tzinfo=UTC)
    days_since = (now - reference).total_seconds() / 86400.0
    cycle = days_since % 29.53058867
    index = int(cycle / 29.53058867 * 8) % 8
    return _MOON_PHASES[index]


def _tz_now() -> datetime:
    """Local wall-clock, mirroring ai_core's resolver. Defensive
    fallback to UTC so an unset / unknown timezone never crashes the
    placeholder rendering."""
    try:
        store = current_app.config["SETTINGS_STORE"]
        raw = str((store.get_section("app") or {}).get("timezone") or "system").strip()
    except (RuntimeError, KeyError):
        raw = "system"
    if not raw or raw.lower() == "system":
        return datetime.now().astimezone()
    try:
        return datetime.now(ZoneInfo(raw))
    except Exception:
        return datetime.now().astimezone()


def _resolve_placeholders(prompt: str) -> str:
    """Substitute ``{time_of_day}`` / ``{season}`` / etc. against the
    server clock. Unknown placeholders are left untouched."""
    now = _tz_now()
    table = {
        "time_of_day": _time_of_day(now.hour),
        "day_of_week": now.strftime("%A"),
        "date": now.strftime("%Y-%m-%d"),
        "month": now.strftime("%B"),
        "season": _season(now.month),
        "hour": f"{now.hour:02d}",
        "year": str(now.year),
        "moon_phase": _moon_phase(now),
    }
    out = prompt
    for key, value in table.items():
        out = out.replace("{" + key + "}", value)
    return out


def _peer(plugin_id: str) -> Any:
    plugin = current_app.config["PLUGIN_REGISTRY"].get(plugin_id)
    return plugin.server_module if plugin is not None else None


def _bucket_from_refresh_hours(refresh_hours: int, now: datetime) -> int:
    """Discrete time bucket for the prompt-rotation + cache key.
    A 6-hour cadence advances the bucket every six wall-clock hours,
    so the prompt line rotates predictably and we never re-call Fal
    within a bucket."""
    refresh_hours = max(1, int(refresh_hours))
    epoch_hours = int(now.timestamp() // 3600)
    return epoch_hours // refresh_hours


def _round_to_64(n: int, *, minimum: int = 512, maximum: int = 2048) -> int:
    if n <= 0:
        return minimum
    rounded = ((int(n) + 63) // 64) * 64
    return max(minimum, min(maximum, rounded))


def _request_dims(options: dict[str, Any], ctx: dict[str, Any]) -> tuple[int, int]:
    explicit_w = int(options.get("image_width") or 0)
    explicit_h = int(options.get("image_height") or 0)
    cell_w = int(ctx.get("cell_w") or 0)
    cell_h = int(ctx.get("cell_h") or 0)
    width = explicit_w if explicit_w > 0 else _round_to_64(cell_w or 1024)
    height = explicit_h if explicit_h > 0 else _round_to_64(cell_h or 1024)
    return width, height


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings
    prompt_raw = str(options.get("prompt") or "").strip()
    if not prompt_raw:
        return {"error": "Prompt is empty."}

    ai_core = _peer("ai_core")
    if ai_core is None:
        return {"error": "AI Core plugin not installed."}

    model = (str(options.get("model") or "").strip()) or "fal-ai/flux/schnell"
    style = (str(options.get("style") or "").strip()) or "none"
    eink_friendly = options.get("eink_friendly", True) is not False
    negative_prompt = str(options.get("negative_prompt") or "").strip()
    refresh_hours = int(options.get("refresh_hours") or 6)
    scale = (str(options.get("scale") or "").strip()) or "fill"

    now = datetime.now(UTC)
    bucket = _bucket_from_refresh_hours(refresh_hours, now)

    # Multi-line prompts rotate by bucket so a "Mon/Tue/Wed" weekly
    # cycle on a 24h cadence steps through the lines in order.
    lines = [ln.strip() for ln in prompt_raw.splitlines() if ln.strip()]
    if not lines:
        return {"error": "Prompt is empty."}
    chosen = lines[bucket % len(lines)]
    resolved_user = _resolve_placeholders(chosen)

    prefix = _STYLE_PREFIXES.get(style, "")
    suffix = _EINK_SUFFIX if eink_friendly else ""
    full_prompt = " ".join(part for part in (prefix, resolved_user, suffix) if part).strip()

    width, height = _request_dims(options, ctx)

    data_dir = Path(ctx["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(
        f"{full_prompt}|{model}|{width}x{height}|{negative_prompt}|{bucket}".encode()
    ).hexdigest()[:16]
    cache_path = data_dir / f"image_{cache_key}.json"

    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            cached["from_cache"] = True
            return cached
        except (json.JSONDecodeError, OSError):
            pass

    result = ai_core.fal_call(
        full_prompt,
        model=model,
        width=width,
        height=height,
        negative_prompt=negative_prompt,
    )
    if result.get("error"):
        return {"error": result["error"], "resolved_prompt": full_prompt}

    response = {
        "image_url": result.get("image_url"),
        "model": result.get("model", model),
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "resolved_prompt": full_prompt,
        "bucket": bucket,
        "scale": scale,
        "from_cache": False,
    }
    with contextlib.suppress(OSError):
        cache_path.write_text(json.dumps(response), encoding="utf-8")
    return response


_ = time  # silence unused-import warnings if time helpers grow later
