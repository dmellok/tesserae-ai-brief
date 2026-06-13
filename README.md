# ai-brief widget bundle for Tesserae

Two data-aware AI widgets that share an Anthropic + Fal.ai connection:

- **`ai_brief`** — LLM-written 1–3 sentence summary card. Defaults to **Claude Haiku 4.5** (~$0.18/month at hourly refresh).
- **`ai_scene`** — full-bleed image whose prompt is rewritten every refresh from live data. Defaults to **Fal.ai Flux Schnell** (~$2.20/month at hourly refresh).

Both widgets read **live weather, calendar events, allow-listed Home Assistant entities, and the wall clock**, then resolve `{placeholders}` in your prompt template against that snapshot.

Install via Settings → Widgets → Browse community widgets on [Tesserae](https://github.com/dmellok/tesserae).

## Folders shipped

- `ai_core` — shared connection layer. Holds the Anthropic + Fal.ai API keys; no cell of its own. Both widgets depend on this folder being installed.
- `ai_brief` — the text widget. Writes the brief.
- `ai_scene` — the image widget. Generates the scene.

## What you get on the dashboard

```
┌── ai_brief ────────────────────────────────────┐
│ ✨ MORNING BRIEF                  HAIKU 4.5    │
│ │ Cloudy and 14°C this morning, climbing to 19 │
│ │ by midday with showers possible after lunch. │
│ │ Three calendar events today; the living room │
│ │ is already warming up at 21°.                │
│ ── ✨ JUST NOW · HAIKU 4.5 ✨ ──                │
└────────────────────────────────────────────────┘

┌── ai_scene ────────────────────────────────────┐
│                                                │
│        ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░         │
│      ▒▓ AI-generated watercolor of cloudy ▓▒   │
│      ▒▓ morning, dithered to your e-ink   ▓▒   │
│      ▒▓ panel. Prompt rewritten hourly.   ▓▒   │
│        ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░         │
│                                                │
└────────────────────────────────────────────────┘
```

## Placeholders you can use in either prompt template

Resolve against live data at render time:

| Placeholder                                         | Resolved from        |
|----------------------------------------------------|----------------------|
| `{weather.now.condition}` / `{weather.now.temp_c}` | Open-Meteo (no key)  |
| `{weather.now.feels_like_c}` / `{weather.now.humidity_pct}` | Open-Meteo |
| `{weather.today.high_c}` / `{weather.today.low_c}` / `{weather.today.precip_pct}` | Open-Meteo |
| `{calendar.events_today}` / `{calendar.next.title}` / `{calendar.next.in_minutes}` | Tesserae's `calendar_core` |
| `{ha.entity.<entity_id>.state}`                     | Tesserae's `ha_core` (allow-listed) |
| `{ha.entity.<entity_id>.attr_<attr>}`              | HA entity attribute |
| `{time.day_of_week}` / `{time.hour}` / `{time.am_pm}` | Server clock |
| `{time.is_morning}` / `{time.is_afternoon}` / `{time.is_evening}` / `{time.is_night}` | Server clock |

**Missing data → `unknown`**. Templates never crash; whatever the AI gets is what was actually available at fetch time. The cell editor shows a click-to-insert chip rack for every placeholder so you don't have to memorise the names.

Cell latitude / longitude default to the server-level home location (Settings → Server → Location) when left empty, so you don't re-type Melbourne coords on every cell.

## Privacy

Each generation sends **only the resolved prompt** to its respective provider:

- `ai_brief` → `api.anthropic.com` (text-only)
- `ai_scene` → `fal.run` (text-only prompt → image URL)

No cell config, no API keys for other services, no other widget's data. Both keys live in your local settings store.

Home Assistant entities are gated by an **explicit allow list** in the cell config: `{ha.entity.<entity_id>.state}` placeholders only resolve when `<entity_id>` is in the cell's `ha_entities` list. A rogue template can't read arbitrary HA state.

## Cost

Per generation (~300 input tokens for the brief, ~300 input tokens for the image prompt):

| Widget | Model | Per call | Hourly |
|---|---|---|---|
| `ai_brief` | Claude Haiku 4.5 | ~$0.00025 | $0.18/month |
| `ai_brief` | Claude Sonnet 4.6 | ~$0.00125 | $0.90/month |
| `ai_brief` | Claude Opus 4.8 | ~$0.005 | $3.60/month |
| `ai_scene` | Fal Flux Schnell | ~$0.003 | $2.20/month |
| `ai_scene` | Fal Hyper-SDXL | ~$0.003 | $2.20/month |
| `ai_scene` | Fal Fast-SDXL | ~$0.01 | $7.30/month |
| `ai_scene` | Fal Flux Dev | ~$0.025 | $18/month |
| `ai_scene` | Fal Flux Pro 1.1 | ~$0.04 | $29/month |

The cache key is `(resolved_prompt, model, …)`, so changes to either invalidate the cache on next render. Same prompt + same data + same model = no API call.

## Requirements

- Tesserae **>= 0.46.8** (for the `variables_textarea` field type and the `home_lat` ctx fallback).
- An Anthropic API key (for `ai_brief`) and/or a Fal.ai API key (for `ai_scene`). Either or both.

## License

MIT. See [LICENSE](./LICENSE).
