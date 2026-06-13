# ai-brief widget bundle for Tesserae

An LLM-written 1–3 sentence dashboard summary that reads **live weather, calendar events, allow-listed Home Assistant entities, and the wall clock**, then renders as a typeset card. Bring your own Anthropic API key.

Default model is **Claude Haiku 4.5** — roughly **$0.18/month** at hourly refresh (≈$0.00025 per generation). Switchable to Sonnet or Opus per cell.

Install via Settings → Widgets → Browse community widgets on [Tesserae](https://github.com/dmellok/tesserae).

## Folders shipped

- `ai_core` — shared Anthropic Claude connection + the placeholder resolver. No cell of its own; configured once at Settings → Plugins → AI Core. Used by every `ai_*` widget; you only need to set the API key in one place.
- `ai_brief` — the actual widget. Writes the brief.

## What you get on the dashboard

```
┌──────────────────────────────────────────────────┐
│ ✨ MORNING BRIEF                  HAIKU 4.5 · 5min│
├──────────────────────────────────────────────────┤
│ Cloudy and 14°C this morning, climbing to 19 by  │
│ midday with showers possible after lunch. Three  │
│ tasks for today including the standup at 9; the  │
│ living room's already warming up at 21°.         │
└──────────────────────────────────────────────────┘
```

## Placeholders you can use in the prompt template

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

**Missing data → `unknown`**. Templates never crash; whatever the LLM gets is what was actually available at fetch time. The widget's debug panel shows you exactly which placeholders resolved to which values, so you can iterate the template without a dry-run.

## Privacy

Each generation sends **only the resolved prompt** to `api.anthropic.com`. No cell config, no API key for other services, no other widget's data. The API key lives in your local settings store; nothing routes through any third-party intermediary.

Home Assistant entities are gated by an **explicit allow list** in the cell config: `{ha.entity.<entity_id>.state}` placeholders only resolve when `<entity_id>` is in the cell's `ha_entities` list. A rogue template can't read arbitrary HA state.

## Cost

Per generation (Claude Haiku 4.5, ~300 input tokens + ~150 output tokens):

- Haiku 4.5: **~$0.00025** → $0.18/month at hourly
- Sonnet 4.6: **~$0.00125** → $0.90/month at hourly
- Opus 4.8: **~$0.005** → $3.60/month at hourly

For freer prose or denser inputs, bump `max_tokens` (default 200). The cache key is `(resolved_prompt, model_override, max_tokens)`, so changes to any of those invalidate the cache on next render.

## License

MIT. See [LICENSE](./LICENSE).
