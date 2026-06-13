// ai_scene, full-bleed AI-generated image. The image URL comes from
// Fal.ai with a prompt rewritten every refresh from live data. The
// renderer treats this widget as full_bleed, so the image fills the
// cell edge-to-edge. Error states swap the image for a muted
// "configure me" placeholder so a fresh install still renders.

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

const LAYOUT = `
.w[data-widget="ai_scene"] {
  height: 100%;
  width: 100%;
  background: var(--bg);
  display: flex;
  align-items: stretch;
  justify-content: stretch;
  overflow: hidden;
}
.w[data-widget="ai_scene"] img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.ai-scene-error,
.ai-scene-empty {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.6em;
  color: var(--text-muted);
  text-align: center;
  padding: 1.5em;
  font-family: var(--font-family, inherit);
}
.ai-scene-error i,
.ai-scene-empty i {
  font-size: 3em;
  color: var(--accent-4);
}
.ai-scene-error p,
.ai-scene-empty p {
  margin: 0;
  max-width: 30ch;
  font-size: 0.95em;
  line-height: 1.4;
}
`;

function errorCard(message) {
  return `
    <div class="w" data-widget="ai_scene">
      <div class="ai-scene-error">
        <i class="ph-bold ph-warning-circle"></i>
        <p>${escapeHtml(message)}</p>
      </div>
    </div>`;
}

function emptyCard() {
  return `
    <div class="w" data-widget="ai_scene">
      <div class="ai-scene-empty">
        <i class="ph-bold ph-sparkle"></i>
        <p>Waiting for the first generation...</p>
      </div>
    </div>`;
}

export default function render(shadow, ctx) {
  const data = ctx?.data ?? {};
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;
  const style = `<style>${LAYOUT}</style>`;

  if (data.error) {
    shadow.innerHTML = `${css}${style}${errorCard(data.error)}`;
    return;
  }
  const imageUrl = data.image_url;
  if (!imageUrl) {
    shadow.innerHTML = `${css}${style}${emptyCard()}`;
    return;
  }
  // Deliberately generic alt. The resolved prompt could be quite
  // long, and some browser / iframe contexts (Recraft v3 webp was
  // the visible regression) fall back to rendering alt text when the
  // img source fails to embed — better that the user sees a neutral
  // label than the whole prompt template materialised on the panel.
  shadow.innerHTML = `
    ${css}${style}
    <div class="w" data-widget="ai_scene">
      <img src="${escapeHtml(imageUrl)}" alt="AI Scene" loading="eager" decoding="sync">
    </div>`;
}
