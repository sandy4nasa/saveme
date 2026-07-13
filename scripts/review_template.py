"""
review_template.py

The "Needs review" page: lists saved_places rows that never reached
status='ready' (so they're invisible from the map/chat, which only show
'ready' places) and lets the user add/edit a specific place-name note and
retry enrichment in place. Fetches its own data client-side via
GET /api/needs-review, POST /api/retry -- keeps serve_app.py's routing
simple (one static HTML shell + fetch calls, same pattern as index.html).
"""

REVIEW_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Needs review — SaveMe</title>
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon-192.png">
<meta name="theme-color" content="#4f8cff">
<link rel="apple-touch-icon" href="/icon-192.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="SaveMe">
<style>
  :root { --bg: #0f1115; --panel: #171a21; --border: #2a2e38; --text: #e8eaed; --muted: #9aa0ac; --accent: #4f8cff; --error: #ff6b6b; --ok: #4caf7d; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); padding: 16px; max-width: 640px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 4px 0; }
  p.hint { color: var(--muted); font-size: 13px; margin: 0 0 20px 0; }
  a.back { color: var(--accent); text-decoration: none; font-size: 13px; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 14px; margin-bottom: 12px; }
  .card .url { font-size: 13px; word-break: break-all; color: var(--accent); text-decoration: none; }
  .card .status { display: inline-block; font-size: 11px; color: var(--muted); background: #10131a; border-radius: 6px; padding: 2px 8px; margin: 6px 0; }
  .card input { width: 100%; padding: 9px; border-radius: 8px; border: 1px solid var(--border); background: #10131a; color: var(--text); font-size: 14px; margin-top: 6px; }
  .card button { margin-top: 8px; padding: 9px 14px; border-radius: 8px; border: none; background: var(--accent); color: #fff; font-size: 13px; font-weight: 600; cursor: pointer; }
  .card button:disabled { opacity: 0.5; }
  .card .result { font-size: 12px; margin-top: 6px; }
  .result.ok { color: var(--ok); }
  .result.err { color: var(--error); }
  #empty { color: var(--muted); font-size: 14px; text-align: center; padding: 40px 0; }
</style>
</head>
<body>
  <a class="back" href="/">&larr; Back to map</a>
  <h1>🔍 Needs review</h1>
  <p class="hint">These were saved but couldn't be matched to a specific place. Add the exact place/project name below and retry.</p>
  <div id="list"></div>
  <div id="empty" style="display:none;">Nothing to review — everything's matched! 🎉</div>

<script>
async function loadItems() {
  const res = await fetch("/api/needs-review");
  if (res.status === 401) { window.location.href = "/login"; return; }
  const items = await res.json();
  const list = document.getElementById("list");
  const empty = document.getElementById("empty");
  list.innerHTML = "";
  if (!items.length) { empty.style.display = "block"; return; }
  empty.style.display = "none";
  for (const item of items) {
    const card = document.createElement("div");
    card.className = "card";
    card.dataset.id = item.id;
    card.innerHTML = `
      <a class="url" href="${item.source_url}" target="_blank" rel="noopener">${item.source_url}</a><br>
      <span class="status">${item.status}</span>
      <input type="text" placeholder="e.g. Third Wave Coffee, Indiranagar" value="${(item.raw_caption || "").replace(/"/g, '&quot;')}">
      <div>
        <button onclick="retryItem(${item.id}, this)">Retry</button>
      </div>
      <div class="result"></div>
    `;
    list.appendChild(card);
  }
}

async function retryItem(id, btn) {
  const card = btn.closest(".card");
  const note = card.querySelector("input").value.trim();
  const resultEl = card.querySelector(".result");
  btn.disabled = true;
  btn.textContent = "Retrying...";
  resultEl.className = "result";
  resultEl.textContent = "";
  try {
    const res = await fetch("/api/retry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ place_id: id, note }),
    });
    if (res.status === 401) { window.location.href = "/login"; return; }
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Retry failed");

    if (data.status === "ready") {
      resultEl.className = "result ok";
      resultEl.textContent = `Matched: ${data.name} \u2713 -- now visible on the map`;
      setTimeout(() => { card.remove(); }, 1200);
    } else {
      resultEl.className = "result err";
      resultEl.textContent = `Still couldn't match (status: ${data.status}). Try a more specific name.`;
      card.querySelector(".status").textContent = data.status;
      btn.disabled = false;
      btn.textContent = "Retry";
    }
  } catch (err) {
    resultEl.className = "result err";
    resultEl.textContent = `Error: ${err.message}`;
    btn.disabled = false;
    btn.textContent = "Retry";
  }
}

loadItems();
</script>
</body>
</html>
"""
