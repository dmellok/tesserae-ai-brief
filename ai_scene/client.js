// ai_scene, full-bleed AI-generated image. The image URL is a local
// /plugins/ai_core/cache/<sha>.<ext> from ai_core's image cache, so
// the Fal-CDN sandboxed-CSP gotcha doesn't apply. Uses Tesserae's
// ``.w.is-bleed`` shell so the cell host strips its default padding
// + border and the image really does fill edge-to-edge.

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// Tesserae's spectra-widgets.css already handles .w.is-bleed > img.
// We add a tiny overlay rule for the fit-mode letterbox, plus the
// error / empty fallbacks.
const LAYOUT = `
.w.is-bleed > img.is-fit {
  object-fit: contain;
  background: var(--bg);
}
.ai-scene-state {
  position: absolute; inset: 0;
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  gap: 0.5em; padding: 1.5em;
  color: var(--text-muted);
  font-family: var(--font-family, inherit);
  text-align: center;
}
.ai-scene-state i {
  font-size: 2.6em;
  color: var(--accent-4);
}
.ai-scene-state p {
  margin: 0; max-width: 30ch;
  font-size: 0.95em; line-height: 1.4;
}
`;

function stateCard(icon, message) {
  return `
    <div class="w is-bleed" data-widget="ai_scene">
      <div class="ai-scene-state">
        <i class="ph-bold ph-${icon}"></i>
        <p>${escapeHtml(message)}</p>
      </div>
    </div>`;
}

export default function render(shadow, ctx) {
  const data = ctx?.data ?? {};
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;
  const style = `<style>${LAYOUT}</style>`;
  if (data.error) {
    shadow.innerHTML = `${css}${style}${stateCard("warning-circle", data.error)}`;
    return;
  }
  const imageUrl = data.image_url;
  if (!imageUrl) {
    shadow.innerHTML = `${css}${style}${stateCard("sparkle", "Waiting for the first generation...")}`;
    return;
  }
  const fitClass = (data.scale === "fit") ? "is-fit" : "";
  shadow.innerHTML = `
    ${css}${style}
    <div class="w is-bleed" data-widget="ai_scene">
      <img class="${fitClass}" src="${escapeHtml(imageUrl)}" alt="AI Scene" loading="eager" decoding="sync">
    </div>`;
}
