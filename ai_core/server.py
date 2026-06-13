"""ai_core, shared Anthropic Claude connection + context resolver.

No widget cell of its own; sibling ai_* widgets reach in via the plugin
registry and call ``call_llm`` and ``resolve_placeholders`` so prompts
across the ai family stay consistent and the API key only lives in one
place.

Config lives in Settings -> Plugins -> AI Core: an Anthropic API key
(get one at https://console.anthropic.com/) and a default model. Each
widget can override the model in its cell options.

Placeholder DSL: prompt templates reference live state like
``{weather.now.temp_c}``, ``{todo.count}``, ``{calendar.next.title}``,
``{ha.entity.<entity_id>.state}``, and ``{time.day_of_week}``. The
resolver reads from peer cores (weather_core, calendar_core, ha_core)
+ the running clock and substitutes values. Unknown placeholders fall
back to a literal ``unknown`` so a malformed template never crashes
the widget.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, send_from_directory

USER_AGENT = "tesserae/0.1 (+ai_core)"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
ENDPOINT = ANTHROPIC_ENDPOINT  # backwards-compat alias for v0.1.x callers
FAL_API_BASE = "https://fal.run"
HTTP_TIMEOUT_S = 30
# Image generation can take 10-60s for slower models (Flux Dev, Recraft).
FAL_TIMEOUT_S = 90

# Static cost table — USD per million tokens for Anthropic, USD per
# image for Fal. Estimates only; actual billing is whatever the
# provider invoices you. Numbers from each provider's published
# pricing page; bump when those move.
ANTHROPIC_PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    # model_id -> (input_per_million, output_per_million)
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-8": (15.00, 75.00),
}
FAL_PRICING_PER_IMAGE: dict[str, float] = {
    "fal-ai/flux/schnell": 0.003,
    "fal-ai/fast-sdxl": 0.01,
    "fal-ai/flux/dev": 0.025,
    "fal-ai/flux-pro/v1.1": 0.04,
    "fal-ai/recraft-v3": 0.04,
    "fal-ai/nano-banana": 0.039,
    "fal-ai/nano-banana-2": 0.08,
}

# Cap on the usage log size — rotate to ``usage.jsonl.1`` once we
# cross this. 90 days at hourly Claude + hourly Fal sits around 1 MiB,
# so 4 MiB is plenty for a normal home install.
_USAGE_LOG_MAX_BYTES = 4 * 1024 * 1024

# Whitelist of placeholder roots the resolver recognises. Anything
# outside this set returns "unknown" rather than reaching into the
# Flask app config. Keeps the template DSL inert: no env vars, no
# arbitrary attribute access on Python objects.
_KNOWN_ROOTS = frozenset({"weather", "todo", "calendar", "ha", "time"})

PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z][\w.\-]*)\}")


# ---------------------------------------------------------------- settings


def _settings() -> dict[str, Any]:
    store = current_app.config["SETTINGS_STORE"]
    section = store.get_section("plugins") or {}
    return section.get("ai_core") or {}


def api_key() -> str:
    """The Anthropic API key. Stored as ``api_key_secret`` on disk per
    the settings_store secret convention."""
    s = _settings()
    return (s.get("api_key_secret") or s.get("api_key") or "").strip()


def default_model() -> str:
    return (_settings().get("model") or "claude-haiku-4-5").strip()


def fal_api_key() -> str:
    """The Fal.ai API key. Stored as ``fal_api_key_secret`` on disk per
    the settings_store secret convention."""
    s = _settings()
    return (s.get("fal_api_key_secret") or s.get("fal_api_key") or "").strip()


# ---------------------------------------------------------------- LLM call


def call_llm(
    prompt: str,
    *,
    model: str | None = None,
    max_tokens: int = 300,
    system: str | None = None,
) -> dict[str, Any]:
    """Send a single-turn prompt to Anthropic Messages API.

    Returns ``{"text": "..."}`` on success, ``{"error": "..."}`` on
    any failure (missing key, network blip, non-200, malformed
    response). Never raises so widget ``fetch()`` callers can render
    a clean error state without try/except gymnastics.
    """
    key = api_key()
    if not key:
        return {
            "error": (
                "Anthropic API key not set. Add one at Settings -> Plugins "
                "-> AI Core."
            )
        }

    body: dict[str, Any] = {
        "model": (model or default_model()),
        "max_tokens": int(max_tokens),
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system

    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        try:
            detail = json.loads(err.read().decode("utf-8")).get("error", {}).get("message", "")
        except (json.JSONDecodeError, OSError):
            detail = ""
        return {"error": f"HTTP {err.code}: {detail or err.reason}"}
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        return {"error": f"{type(err).__name__}: {err}"}
    except json.JSONDecodeError as err:
        return {"error": f"Malformed response: {err}"}

    content = payload.get("content") or []
    text_parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    text = "".join(text_parts).strip()
    if not text:
        return {"error": "Empty response from model"}
    model_id = payload.get("model") or body["model"]
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    _log_usage(
        {
            "provider": "anthropic",
            "model": str(model_id),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": _anthropic_cost(model_id, input_tokens, output_tokens),
        }
    )
    return {"text": text, "model": model_id}


# ---------------------------------------------------------------- Fal image call


def fal_call(
    prompt: str,
    *,
    model: str = "fal-ai/flux/schnell",
    width: int = 1024,
    height: int = 1024,
    negative_prompt: str = "",
    seed: int | None = None,
) -> dict[str, Any]:
    """Synchronous Fal.ai image-generation call. Returns
    ``{"image_url": "...", "model": "..."}`` on success or
    ``{"error": "..."}`` on any failure.

    Body shape covers the most common Fal models: Flux Schnell / Dev /
    Pro, SDXL variants, Recraft V3, Nano Banana. Each accepts
    ``prompt`` + ``image_size`` (object with width/height) + optional
    ``negative_prompt`` + ``seed`` + ``num_images`` and returns
    ``{"images": [{"url": ...}], ...}``. Never raises.
    """
    key = fal_api_key()
    if not key:
        return {
            "error": (
                "Fal.ai API key not set. Add one at Settings -> Plugins -> AI Core."
            )
        }
    body: dict[str, Any] = {
        "prompt": prompt,
        "image_size": {"width": int(width), "height": int(height)},
        "num_images": 1,
        "enable_safety_checker": False,
    }
    if negative_prompt:
        body["negative_prompt"] = negative_prompt
    if seed is not None:
        body["seed"] = int(seed)

    url = f"{FAL_API_BASE}/{model.lstrip('/')}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Key {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=FAL_TIMEOUT_S) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        try:
            body_text = err.read().decode("utf-8", errors="replace")
            detail = json.loads(body_text).get("detail") or json.loads(body_text).get(
                "error", {}
            ).get("message", "")
            if not detail:
                detail = body_text[:200]
        except (json.JSONDecodeError, OSError):
            detail = ""
        return {"error": f"HTTP {err.code}: {detail or err.reason}"}
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        return {"error": f"{type(err).__name__}: {err}"}
    except json.JSONDecodeError as err:
        return {"error": f"Malformed response: {err}"}

    image_url = _pick_fal_image_url(payload)
    if not image_url:
        return {"error": "Fal response did not contain an image URL"}
    _log_usage(
        {
            "provider": "fal",
            "model": str(model),
            "image_count": 1,
            "width": int(width),
            "height": int(height),
            "cost_usd": _fal_cost(model),
        }
    )
    # Re-host the image locally. Two reasons:
    #   1. Fal's CDN serves images with a sandboxed CSP that breaks
    #      embedding in some browser / iframe contexts (Recraft v3 was
    #      the visible regression: webp images returned alt text in
    #      the cell editor preview rather than the actual image).
    #   2. Fal storage URLs are not promised to be permanent. A cached
    #      local copy means the widget keeps painting after Fal rotates
    #      its CDN paths.
    cached_url = _cache_remote_image(image_url) or image_url
    return {"image_url": cached_url, "model": model, "fal_url": image_url}


# ---------------------------------------------------------------- image cache


_CACHE_TIMEOUT_S = 30
_CACHE_MAX_BYTES = 16 * 1024 * 1024  # 16 MiB; the largest Fal model output


def _data_dir() -> Path | None:
    """Resolve ai_core's data_dir from the plugin registry. Returns
    None when running outside a request context (e.g. unit tests),
    so callers can fall back to passing through the upstream URL."""
    try:
        registry = current_app.config["PLUGIN_REGISTRY"]
    except (RuntimeError, KeyError):
        return None
    plugin = registry.get("ai_core")
    if plugin is None:
        return None
    return plugin.data_dir  # type: ignore[no-any-return]


def _cache_remote_image(url: str) -> str | None:
    """Download ``url`` into ai_core's data_dir/cache/ and return a
    local URL pointing at the cached copy. Returns ``None`` on any
    failure so the caller can fall back to the original URL.

    The cache filename is the sha256 of the URL itself + the original
    extension, so the same Fal URL hits the same file (and a single
    Recraft / Nano Banana render is downloaded only once across
    multiple cells using the same prompt).
    """
    data_dir = _data_dir()
    if data_dir is None:
        return None
    cache_dir = data_dir / "cache"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    # Pull the file extension off the original URL. Strip query
    # strings since Fal sometimes signs URLs.
    bare = url.split("?", 1)[0]
    ext = bare.rsplit(".", 1)[-1].lower() if "." in bare.rsplit("/", 1)[-1] else "bin"
    if ext not in {"png", "jpg", "jpeg", "webp", "gif", "bin"}:
        ext = "bin"
    filename = f"{digest}.{ext}"
    target = cache_dir / filename
    if not target.exists():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=_CACHE_TIMEOUT_S) as resp:  # noqa: S310
                data = resp.read(_CACHE_MAX_BYTES + 1)
        except (urllib.error.URLError, TimeoutError, OSError):
            return None
        if not data or len(data) > _CACHE_MAX_BYTES:
            return None
        with contextlib.suppress(OSError):
            target.write_bytes(data)
        if not target.exists():
            return None
    return f"/plugins/ai_core/cache/{filename}"


# ---------------------------------------------------------------- usage log + cost helpers


def _anthropic_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimated USD cost for an Anthropic call. Falls back to the
    Haiku rate when an unknown model id sneaks through (so the totals
    don't undercount); the per-model breakdown will still flag the
    unknown id with its actual name."""
    pricing = ANTHROPIC_PRICING_PER_MTOK.get(
        model, ANTHROPIC_PRICING_PER_MTOK["claude-haiku-4-5"]
    )
    in_rate, out_rate = pricing
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate


def _fal_cost(model: str) -> float:
    """Estimated USD cost for a single Fal image. Falls back to the
    cheapest known rate when an unknown model is used; the dashboard
    still surfaces the unknown id."""
    return FAL_PRICING_PER_IMAGE.get(model, 0.003)


def _usage_log_path() -> Path | None:
    data_dir = _data_dir()
    if data_dir is None:
        return None
    return data_dir / "usage.jsonl"


def _log_usage(record: dict[str, Any]) -> None:
    """Append a single call record to ``usage.jsonl``. Never raises —
    a missing data_dir, a disk-full error, or a corrupt timestamp all
    just drop the record silently rather than break the live render
    path. Rotates to ``usage.jsonl.1`` once the live log exceeds
    ``_USAGE_LOG_MAX_BYTES``."""
    path = _usage_log_path()
    if path is None:
        return
    record_with_ts = {"ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), **record}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _USAGE_LOG_MAX_BYTES:
            with contextlib.suppress(OSError):
                rotated = path.with_suffix(".jsonl.1")
                if rotated.exists():
                    rotated.unlink()
                path.rename(rotated)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record_with_ts, separators=(",", ":")) + "\n")
    except OSError:
        return


def _iter_usage_records(*, days: int = 30) -> list[dict[str, Any]]:
    """Read both ``usage.jsonl`` and its rotated sibling, filtering to
    the last ``days`` days. Records that fail to parse are dropped
    silently — a single corrupt line doesn't break aggregation."""
    path = _usage_log_path()
    if path is None:
        return []
    cutoff = datetime.now(UTC) - timedelta(days=days)
    out: list[dict[str, Any]] = []
    for candidate in (path.with_suffix(".jsonl.1"), path):
        if not candidate.exists():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_raw = record.get("ts")
            if not isinstance(ts_raw, str):
                continue
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts < cutoff:
                continue
            record["_ts"] = ts
            out.append(record)
    out.sort(key=lambda r: r["_ts"])
    return out


def _aggregate_usage(
    records: list[dict[str, Any]], *, now: datetime | None = None
) -> dict[str, Any]:
    """Build the snapshot the admin page renders. Pure function, easy
    to unit-test."""
    now = now or datetime.now(UTC)
    today = now.strftime("%Y-%m-%d")
    this_month = now.strftime("%Y-%m")
    cutoff_7d = now - timedelta(days=7)

    by_day: dict[str, dict[str, float]] = {}
    by_model: dict[str, dict[str, float]] = {}
    totals = {
        "spend_today": 0.0,
        "spend_month": 0.0,
        "calls_today": 0,
        "calls_month": 0,
        "spend_7d": 0.0,
        "calls_7d": 0,
        "anthropic_spend": 0.0,
        "fal_spend": 0.0,
        "anthropic_calls": 0,
        "fal_calls": 0,
    }
    for record in records:
        ts = record["_ts"]
        day_key = ts.strftime("%Y-%m-%d")
        cost = float(record.get("cost_usd") or 0.0)
        provider = str(record.get("provider") or "unknown")
        model = str(record.get("model") or "unknown")
        day_bucket = by_day.setdefault(
            day_key, {"day": day_key, "anthropic": 0.0, "fal": 0.0, "calls": 0}
        )
        day_bucket[provider] = day_bucket.get(provider, 0.0) + cost
        day_bucket["calls"] = day_bucket.get("calls", 0) + 1
        model_bucket = by_model.setdefault(
            model,
            {
                "model": model,
                "provider": provider,
                "calls": 0,
                "spend": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "images": 0,
            },
        )
        model_bucket["calls"] += 1
        model_bucket["spend"] += cost
        model_bucket["input_tokens"] += int(record.get("input_tokens") or 0)
        model_bucket["output_tokens"] += int(record.get("output_tokens") or 0)
        model_bucket["images"] += int(record.get("image_count") or 0)
        if provider == "anthropic":
            totals["anthropic_spend"] += cost
            totals["anthropic_calls"] += 1
        elif provider == "fal":
            totals["fal_spend"] += cost
            totals["fal_calls"] += 1
        if day_key == today:
            totals["spend_today"] += cost
            totals["calls_today"] += 1
        if day_key.startswith(this_month):
            totals["spend_month"] += cost
            totals["calls_month"] += 1
        if ts >= cutoff_7d:
            totals["spend_7d"] += cost
            totals["calls_7d"] += 1

    # Walk the last 30 days so the chart x-axis has a continuous zero
    # baseline (Chart.js looks weird if you skip days where there
    # were no calls).
    daily_series: list[dict[str, Any]] = []
    for offset in range(29, -1, -1):
        day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
        bucket = by_day.get(day) or {"day": day, "anthropic": 0.0, "fal": 0.0, "calls": 0}
        daily_series.append(
            {
                "day": day,
                "anthropic": round(bucket.get("anthropic", 0.0), 4),
                "fal": round(bucket.get("fal", 0.0), 4),
                "calls": int(bucket.get("calls", 0)),
            }
        )

    # Projection: at the recent 7-day rate, how much would 30 days
    # cost? Defends against a zero-week (empty log → 0 projection).
    projection_monthly = (totals["spend_7d"] / 7.0) * 30.0 if totals["spend_7d"] else 0.0

    model_rows = sorted(by_model.values(), key=lambda m: m["spend"], reverse=True)
    return {
        "totals": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in totals.items()},
        "projection_monthly": round(projection_monthly, 2),
        "daily_series": daily_series,
        "model_breakdown": [
            {
                **row,
                "spend": round(row["spend"], 4),
                "avg_cost": round(row["spend"] / row["calls"], 5) if row["calls"] else 0.0,
            }
            for row in model_rows
        ],
        "record_count": len(records),
        "anthropic_pricing": ANTHROPIC_PRICING_PER_MTOK,
        "fal_pricing": FAL_PRICING_PER_IMAGE,
    }


def blueprint() -> Blueprint:
    """Plugin admin surface for ai_core. Mounted by Tesserae's
    plugin_loader at ``/plugins/ai_core/``.

    Routes:
      * ``GET /``            — usage dashboard (token/cost charts).
      * ``GET /cache/<f>``   — serves a locally cached Fal image.
    """
    bp = Blueprint("ai_core", __name__, template_folder="templates")

    @bp.get("/")
    def index() -> str:
        from flask import render_template  # local import keeps top of file light

        records = _iter_usage_records(days=30)
        aggregate = _aggregate_usage(records)
        return render_template(
            "ai_core/index.html",
            aggregate=aggregate,
            log_path=str(_usage_log_path()) if _usage_log_path() else "(unset)",
            has_anthropic_key=bool(api_key()),
            has_fal_key=bool(fal_api_key()),
        )

    @bp.get("/cache/<path:filename>")
    def cached(filename: str) -> Any:
        # Block path traversal — only flat files inside cache/.
        if "/" in filename or filename.startswith("."):
            abort(404)
        data_dir = _data_dir()
        if data_dir is None:
            abort(404)
        cache_dir = data_dir / "cache"
        if not cache_dir.exists():
            abort(404)
        return send_from_directory(cache_dir, filename)

    return bp


def _pick_fal_image_url(payload: Any) -> str | None:
    """Extract the first image URL from a Fal response.

    Most Fal models return ``{"images": [{"url": ...}], ...}``. A few
    (older single-image endpoints) return ``{"image": {"url": ...}}``.
    Try both shapes.
    """
    if not isinstance(payload, dict):
        return None
    images = payload.get("images")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            url = first.get("url")
            if isinstance(url, str) and url:
                return url
    image = payload.get("image")
    if isinstance(image, dict):
        url = image.get("url")
        if isinstance(url, str) and url:
            return url
    return None


# ---------------------------------------------------------------- placeholders


def _peer(plugin_id: str) -> Any:
    """Look up another plugin's loaded server module via the registry.

    Returns ``None`` if the peer isn't installed; the caller falls back
    to ``unknown`` rather than crashing a brief because the user hasn't
    set up weather_core yet.
    """
    plugin = current_app.config["PLUGIN_REGISTRY"].get(plugin_id)
    return plugin.server_module if plugin is not None else None


def _resolve_one(
    path: str,
    *,
    time_now: datetime,
    weather_snapshot: dict[str, Any] | None = None,
    todo_snapshot: dict[str, Any] | None = None,
    calendar_snapshot: dict[str, Any] | None = None,
    ha_allow: set[str] | None = None,
) -> str:
    """Resolve a single dotted placeholder path to a string value.

    Unknown roots and unreachable subpaths return the literal string
    ``unknown`` so the rendered template still reads as a sentence.
    """
    parts = path.split(".")
    if not parts or parts[0] not in _KNOWN_ROOTS:
        return "unknown"

    root = parts[0]
    if root == "time":
        return _resolve_time(parts[1:], time_now)
    if root == "weather" and weather_snapshot is not None:
        return _walk(weather_snapshot, parts[1:])
    if root == "todo" and todo_snapshot is not None:
        return _walk(todo_snapshot, parts[1:])
    if root == "calendar" and calendar_snapshot is not None:
        return _walk(calendar_snapshot, parts[1:])
    if root == "ha":
        return _resolve_ha(parts[1:], ha_allow or set())
    return "unknown"


def _walk(snapshot: dict[str, Any], rest: list[str]) -> str:
    cur: Any = snapshot
    for key in rest:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list):
            try:
                cur = cur[int(key)]
            except (ValueError, IndexError):
                return "unknown"
        else:
            return "unknown"
        if cur is None:
            return "unknown"
    if isinstance(cur, dict | list):
        return "unknown"
    return str(cur)


def _resolve_time(rest: list[str], now: datetime) -> str:
    if not rest:
        return now.isoformat()
    key = rest[0]
    if key == "hour":
        return f"{now.hour:02d}"
    if key == "minute":
        return f"{now.minute:02d}"
    if key == "day_of_week":
        return now.strftime("%A")
    if key == "day":
        return now.strftime("%d")
    if key == "month":
        return now.strftime("%B")
    if key == "year":
        return str(now.year)
    if key == "am_pm":
        return "AM" if now.hour < 12 else "PM"
    if key == "is_morning":
        return "true" if 5 <= now.hour < 12 else "false"
    if key == "is_afternoon":
        return "true" if 12 <= now.hour < 17 else "false"
    if key == "is_evening":
        return "true" if 17 <= now.hour < 22 else "false"
    if key == "is_night":
        return "true" if (now.hour >= 22 or now.hour < 5) else "false"
    if key == "iso":
        return now.isoformat()
    return "unknown"


def _resolve_ha(rest: list[str], allow: set[str]) -> str:
    """``ha.entity.<entity_id>.state`` against the live HA core.

    ``entity_id`` must appear in the cell's explicit allow list, so a
    rogue template can't read arbitrary HA state. Empty allow list ->
    every ha placeholder resolves to ``unknown``.
    """
    if len(rest) < 3 or rest[0] != "entity":
        return "unknown"
    # Recombine entity_id (may contain dots up to one: domain.object_id)
    # The wire form is ``ha.entity.sensor.living_room_temp.state`` so
    # entity_id = "sensor.living_room_temp" and tail = "state".
    if len(rest) < 4:
        return "unknown"
    entity_id = f"{rest[1]}.{rest[2]}"
    tail = rest[3]
    if entity_id not in allow:
        return "unknown"
    core = _peer("ha_core")
    if core is None or not hasattr(core, "get_state"):
        return "unknown"
    try:
        state = core.get_state(entity_id)
    except Exception:
        return "unknown"
    if not isinstance(state, dict):
        return "unknown"
    if tail == "state":
        return str(state.get("state") or "unknown")
    if tail.startswith("attr_"):
        attrs = state.get("attributes") or {}
        return str(attrs.get(tail[len("attr_") :]) or "unknown")
    return "unknown"


def _tz_now() -> datetime:
    """Render time in the host's effective wall clock.

    Matches Tesserae's canonical timezone resolution (see
    ``app.app_factory._resolve_timezone``):

      * Reads ``settings.json -> app.timezone`` (NOT ``server``; the
        section name was wrong in v0.1.x and v0.2.0 so every render
        landed in UTC, off by the user's offset).
      * ``"system"`` / empty / unknown IANA name falls back to the
        host's local timezone via ``datetime.now().astimezone()`` so
        ``{time.hour}`` matches what the user sees on their wall
        clock, not what UTC says.
    """
    store = current_app.config["SETTINGS_STORE"]
    raw = str((store.get_section("app") or {}).get("timezone") or "system").strip()
    if not raw or raw.lower() == "system":
        return datetime.now().astimezone()
    try:
        return datetime.now(ZoneInfo(raw))
    except Exception:
        return datetime.now().astimezone()


def resolve_placeholders(
    template: str,
    *,
    weather: dict[str, Any] | None = None,
    todo: dict[str, Any] | None = None,
    calendar: dict[str, Any] | None = None,
    ha_allow: list[str] | None = None,
) -> tuple[str, dict[str, str]]:
    """Substitute every ``{path}`` placeholder in ``template``.

    Returns ``(resolved_string, debug_values)`` where ``debug_values``
    maps the original placeholder paths to their resolved values; the
    widget's debug panel renders this so users can see what got sent
    to the model.

    Snapshots (``weather`` / ``todo`` / ``calendar``) are passed in by
    the caller so the widget controls the freshness and shape of the
    data; ai_core stays a pure resolver and doesn't make decisions
    about cache or fetching.
    """
    now = _tz_now()
    allow_set = set(ha_allow or [])
    debug: dict[str, str] = {}

    def _sub(match: re.Match[str]) -> str:
        path = match.group(1)
        value = _resolve_one(
            path,
            time_now=now,
            weather_snapshot=weather,
            todo_snapshot=todo,
            calendar_snapshot=calendar,
            ha_allow=allow_set,
        )
        debug[path] = value
        return value

    return PLACEHOLDER_RE.sub(_sub, template), debug


def available_placeholders() -> list[str]:
    """Enumerated examples the editor surfaces in widget help text."""
    return [
        "weather.now.condition",
        "weather.now.temp_c",
        "weather.now.feels_like_c",
        "weather.today.high_c",
        "weather.today.low_c",
        "weather.today.precip_mm",
        "todo.count",
        "todo.urgent_count",
        "todo.top",
        "calendar.next.title",
        "calendar.next.in_minutes",
        "calendar.events_today",
        "ha.entity.<entity_id>.state",
        "time.day_of_week",
        "time.hour",
        "time.am_pm",
        "time.is_morning",
        "time.is_afternoon",
        "time.is_evening",
    ]
