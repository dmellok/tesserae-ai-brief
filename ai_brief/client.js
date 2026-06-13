// ai_brief, large readable text card. Header carries the configured
// label + a small model badge; body is the LLM-written brief; footer
// shows freshness ("2min ago"). Error states swap the body for a
// muted "configure me" message so a brand-new install still renders
// something coherent before the API key is set.

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function relativeAge(iso) {
  if (!iso) return "";
  const ts = Date.parse(iso);
  if (Number.isNaN(ts)) return "";
  const diff = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (diff < 60) return "just now";
  const mins = Math.round(diff / 60);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

function modelBadge(model) {
  if (!model) return "";
  // claude-haiku-4-5-20251001 → "HAIKU 4.5"
  const m = model.match(/claude-(opus|sonnet|haiku|fable)-(\d+(?:-\d+)?)/i);
  if (m) return `${m[1].toUpperCase()} ${m[2].replace("-", ".")}`;
  return model.toUpperCase();
}

const LAYOUT = `
.w[data-widget="ai_brief"] {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 1em 1.1em;
  gap: 0.8em;
  background: var(--bg);
  color: var(--text);
}
.ai-brief-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.6em;
  font-family: var(--font-mono, monospace);
  font-size: 0.65em;
  letter-spacing: 0.18em;
  text-transform: uppercase;
}
.ai-brief-head .lead {
  display: inline-flex;
  align-items: center;
  gap: 0.45em;
  color: var(--accent-3);
  font-weight: 700;
}
.ai-brief-head .lead i {
  font-size: 1.4em;
}
.ai-brief-head .meta {
  color: var(--text-muted);
  font-weight: 600;
  letter-spacing: 0.12em;
}
.ai-brief-head .meta .badge {
  background: var(--surface);
  padding: 0.18em 0.55em;
  border-radius: 0.4em;
  margin-right: 0.5em;
  color: var(--text);
  letter-spacing: 0.16em;
}
.ai-brief-body {
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  font-size: 1em;
  line-height: 1.45;
  font-weight: 500;
  color: var(--text);
  hyphens: auto;
}
.ai-brief-body p {
  margin: 0;
}
.ai-brief-body.is-error,
.ai-brief-body.is-empty {
  color: var(--text-muted);
  font-style: italic;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  text-align: center;
  gap: 0.6em;
}
.ai-brief-body.is-error i,
.ai-brief-body.is-empty i {
  font-size: 2.4em;
  color: var(--accent-4);
}
.ai-brief-body.is-error i {
  color: var(--accent-5, var(--accent-4));
}
`;

export default function render(shadow, ctx) {
  const data = ctx?.data ?? {};
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;
  const style = `<style>${LAYOUT}</style>`;

  const label = data.header_label || "BRIEF";

  // Error state: configure-me prompt or upstream LLM error.
  if (data.error) {
    shadow.innerHTML = `
      ${css}${style}
      <div class="w" data-widget="ai_brief">
        <div class="ai-brief-head">
          <span class="lead"><i class="ph-bold ph-sparkle"></i> ${escapeHtml(label)}</span>
        </div>
        <div class="ai-brief-body is-error">
          <i class="ph-bold ph-warning-circle"></i>
          <p>${escapeHtml(data.error)}</p>
        </div>
      </div>`;
    return;
  }

  const brief = (data.brief || "").trim();
  if (!brief) {
    shadow.innerHTML = `
      ${css}${style}
      <div class="w" data-widget="ai_brief">
        <div class="ai-brief-head">
          <span class="lead"><i class="ph-bold ph-sparkle"></i> ${escapeHtml(label)}</span>
        </div>
        <div class="ai-brief-body is-empty">
          <i class="ph-bold ph-sparkle"></i>
          <p>Waiting for the first generation...</p>
        </div>
      </div>`;
    return;
  }

  const badge = modelBadge(data.model);
  const age = relativeAge(data.generated_at);
  const meta = [badge && `<span class="badge">${escapeHtml(badge)}</span>`, age]
    .filter(Boolean)
    .join("");

  shadow.innerHTML = `
    ${css}${style}
    <div class="w" data-widget="ai_brief">
      <div class="ai-brief-head">
        <span class="lead"><i class="ph-bold ph-sparkle"></i> ${escapeHtml(label)}</span>
        <span class="meta">${meta}</span>
      </div>
      <div class="ai-brief-body">
        <p>${escapeHtml(brief)}</p>
      </div>
    </div>`;
}
