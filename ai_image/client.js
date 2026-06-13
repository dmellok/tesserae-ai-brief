// ai_image, full-bleed AI-generated art. Uses Tesserae's
// ``.w.is-bleed`` shell so the host strips its default padding and
// border, then the single <img> covers (or contains, per the scale
// option) the cell edge-to-edge. Image URL is the local
// /plugins/ai_core/cache/<sha>.<ext> so Fal-CDN sandboxed-CSP isn't
// in the embedding path.

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// Tesserae's spectra-widgets.css already handles .w.is-bleed > img.
// We add a tiny overlay rule for the fit-mode letterbox (centred,
// dark backdrop, image contained), plus the error / empty fallbacks.
const LAYOUT = `
.w.is-bleed > img.is-fit {
  object-fit: contain;
  background: var(--bg);
}
.ai-image-state {
  position: absolute; inset: 0;
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  gap: 0.6em; padding: 1.5em;
  color: var(--text-muted);
  font-family: var(--font-family, inherit);
  text-align: center;
}
.ai-image-state i {
  font-size: 3em;
  color: var(--accent-4);
}
.ai-image-state p {
  margin: 0; max-width: 30ch;
  font-size: 0.95em; line-height: 1.4;
}
`;

function stateCard(icon, message) {
  return `
    <div class="w is-bleed" data-widget="ai_image">
      <div class="ai-image-state">
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
    shadow.innerHTML = `${css}${style}${stateCard("image-square", "Waiting for the first generation...")}`;
    return;
  }
  const fitClass = (data.scale === "fit") ? "is-fit" : "";
  shadow.innerHTML = `
    ${css}${style}
    <div class="w is-bleed" data-widget="ai_image">
      <img class="${fitClass}" src="${escapeHtml(imageUrl)}" alt="AI Image" loading="eager" decoding="sync">
    </div>`;
}
