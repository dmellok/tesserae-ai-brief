// ai_image, full-bleed AI-generated art. The image URL is a local
// /plugins/ai_core/cache/<sha>.<ext> from ai_core's image cache, so
// the Fal-CDN sandboxed-CSP gotcha doesn't apply.
//
// Heir to fal_image. Same shape (full_bleed cell, single <img> with
// object-fit: cover), neutral alt text so a failed embed never
// renders the prompt template.

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

const LAYOUT = `
.w[data-widget="ai_image"] {
  height: 100%;
  width: 100%;
  background: var(--bg);
  display: flex;
  align-items: stretch;
  justify-content: stretch;
  overflow: hidden;
}
.w[data-widget="ai_image"] img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.ai-image-error, .ai-image-empty {
  width: 100%;
  height: 100%;
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  gap: 0.6em;
  padding: 1.5em;
  color: var(--text-muted);
  font-family: var(--font-family, inherit);
  text-align: center;
}
.ai-image-error i, .ai-image-empty i {
  font-size: 3em;
  color: var(--accent-4);
}
.ai-image-error p, .ai-image-empty p {
  margin: 0;
  max-width: 30ch;
  font-size: 0.95em;
  line-height: 1.4;
}
`;

function errorCard(message) {
  return `
    <div class="w" data-widget="ai_image">
      <div class="ai-image-error">
        <i class="ph-bold ph-warning-circle"></i>
        <p>${escapeHtml(message)}</p>
      </div>
    </div>`;
}

function emptyCard() {
  return `
    <div class="w" data-widget="ai_image">
      <div class="ai-image-empty">
        <i class="ph-bold ph-image-square"></i>
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
  shadow.innerHTML = `
    ${css}${style}
    <div class="w" data-widget="ai_image">
      <img src="${escapeHtml(imageUrl)}" alt="AI Image" loading="eager" decoding="sync">
    </div>`;
}
