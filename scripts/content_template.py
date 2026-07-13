"""
content_template.py

The "Saved Content" page: lists saved_places rows with status='saved_no_place'
-- posts that were correctly identified as having no real-world venue at all
(recipes, DIY/craft tutorials, product posts, etc.), so they can never appear
on the map. These are still fully tagged and embedded (chat-searchable), just
browsable here as their own list instead of being stuck in "Needs review"
forever waiting for a location that doesn't exist. Fetches its own data
client-side via GET /api/content -- same self-contained pattern as
review_template.py / index.html.
"""

CONTENT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Saved Content — SaveMe</title>
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon-192.png">
<meta name="theme-color" content="#4f8cff">
<style>
  :root { --bg: #0f1115; --panel: #171a21; --border: #2a2e38; --text: #e8eaed; --muted: #9aa0ac; --accent: #4f8cff; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); padding: 16px; max-width: 640px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 4px 0; }
  p.hint { color: var(--muted); font-size: 13px; margin: 0 0 16px 0; }
  a.back { color: var(--accent); text-decoration: none; font-size: 13px; }
  .filters { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }
  .filters button { padding: 6px 12px; border-radius: 999px; border: 1px solid var(--border); background: var(--panel); color: var(--text); font-size: 12px; cursor: pointer; }
  .filters button.active { background: var(--accent); border-color: var(--accent); }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 14px; margin-bottom: 12px; }
  .card .title { font-size: 15px; font-weight: 600; margin-bottom: 4px; }
  .card .caption { font-size: 13px; color: var(--muted); max-height: 4.5em; overflow: hidden; margin-bottom: 8px; white-space: pre-line; }
  .card .meta { display: flex; justify-content: space-between; align-items: center; font-size: 11px; color: var(--muted); }
  .card .url { color: var(--accent); text-decoration: none; }
  .tag { display: inline-block; font-size: 11px; background: #10131a; border-radius: 6px; padding: 2px 8px; margin: 2px 4px 2px 0; color: var(--muted); }
  #empty { color: var(--muted); font-size: 14px; text-align: center; padding: 40px 0; }
</style>
</head>
<body>
  <a class="back" href="/">&larr; Back to map</a>
  <h1>📝 Saved Content</h1>
  <p class="hint">Posts saved with no map location -- recipes, DIY/craft, and other content. Still searchable in chat ("what recipes did I save?").</p>
  <div class="filters" id="filters"></div>
  <div id="list"></div>
  <div id="empty" style="display:none;">Nothing here yet.</div>

<script>
let allItems = [];
let activeCategory = null;

function renderFilters() {
  const cats = [...new Set(allItems.map(i => i.category))].sort();
  const filtersEl = document.getElementById("filters");
  filtersEl.innerHTML = "";
  const allBtn = document.createElement("button");
  allBtn.textContent = "All";
  allBtn.className = activeCategory === null ? "active" : "";
  allBtn.onclick = () => { activeCategory = null; render(); };
  filtersEl.appendChild(allBtn);
  for (const cat of cats) {
    const btn = document.createElement("button");
    btn.textContent = cat.replace(/_/g, " ");
    btn.className = activeCategory === cat ? "active" : "";
    btn.onclick = () => { activeCategory = cat; render(); };
    filtersEl.appendChild(btn);
  }
}

function render() {
  renderFilters();
  const list = document.getElementById("list");
  const empty = document.getElementById("empty");
  const items = activeCategory ? allItems.filter(i => i.category === activeCategory) : allItems;
  list.innerHTML = "";
  if (!items.length) { empty.style.display = "block"; return; }
  empty.style.display = "none";
  const PLATFORM_ICONS = { instagram: "📷", youtube: "▶️" };
  for (const item of items) {
    const card = document.createElement("div");
    card.className = "card";
    const tags = (item.tags || []).map(t => `<span class="tag">${t}</span>`).join(" ");
    const badge = PLATFORM_ICONS[item.platform] || "🔗";
    card.innerHTML = `
      <div class="title">${badge} ${item.name || "Saved post"}</div>
      <div class="caption">${(item.raw_caption || "").replace(/</g, "&lt;")}</div>
      <div>${tags}</div>
      <div class="meta">
        <a class="url" href="${item.source_url}" target="_blank" rel="noopener">View original</a>
        <span>${(item.saved_at || "").slice(0, 10)}</span>
      </div>
    `;
    list.appendChild(card);
  }
}

async function loadItems() {
  const res = await fetch("/api/content");
  if (res.status === 401) { window.location.href = "/login"; return; }
  allItems = await res.json();
  render();
}

loadItems();
</script>
</body>
</html>
"""
