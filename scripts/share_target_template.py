SHARE_TARGET_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Save to SaveMe</title>
<style>
  :root {{ --bg: #0f1115; --panel: #171a21; --border: #2a2e38; --text: #e8eaed; --muted: #9aa0ac; --accent: #4f8cff; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); padding: 20px; }}
  h1 {{ font-size: 18px; margin: 0 0 4px 0; }}
  p.hint {{ color: var(--muted); font-size: 12px; margin: 0 0 16px 0; }}
  .url-box {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 10px; font-size: 12px; word-break: break-all; color: var(--muted); margin-bottom: 14px; }}
  textarea {{
    width: 100%; min-height: 90px; padding: 10px; border-radius: 8px; border: 1px solid var(--border);
    background: #10131a; color: var(--text); font-size: 14px; resize: vertical;
  }}
  button {{
    margin-top: 12px; width: 100%; padding: 12px; border-radius: 8px; border: none;
    background: var(--accent); color: #fff; font-size: 15px; font-weight: 600; cursor: pointer;
  }}
  button:disabled {{ opacity: 0.5; }}
  #status {{ margin-top: 16px; font-size: 14px; line-height: 1.5; color: var(--muted); }}
  #result {{ margin-top: 16px; font-size: 14px; line-height: 1.5; }}
  #result .tag {{ display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 6px; background: #2a2e38; color: var(--muted); margin: 2px; }}
  #fallback {{ display: none; margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--border); }}
  a.back {{ display: inline-block; margin-top: 16px; color: var(--accent); font-size: 13px; text-decoration: none; }}
  .spinner {{ display: inline-block; width: 14px; height: 14px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite; margin-right: 8px; vertical-align: middle; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
</style>
</head>
<body>
  <h1>📍 Save to SaveMe</h1>
  <p class="hint">Saving automatically -- we fetch the real Instagram caption ourselves, no typing needed.</p>
  <div class="url-box">{shared_url}</div>
  <div id="status"><span class="spinner"></span>Fetching caption &amp; saving...</div>
  <div id="result"></div>

  <div id="fallback">
    <p class="hint">Couldn't confidently match a place from the caption. Add/edit a note with the place name or area and try again:</p>
    <textarea id="note" placeholder="e.g. Third Wave Coffee, Jayanagar"></textarea>
    <button type="button" id="retry-btn">Retry</button>
  </div>

  <a class="back" href="/">&larr; Back to map</a>

<script>
const sharedUrl = {shared_url_json};
let placeId = null;

function renderResult(data) {{
  const statusEl = document.getElementById("status");
  const resultEl = document.getElementById("result");
  const fallbackEl = document.getElementById("fallback");
  statusEl.style.display = "none";
  placeId = data.place_id;

  if (data.status === "ready") {{
    const tags = (data.tags || []).map(t => `<span class="tag">${{t}}</span>`).join(" ");
    resultEl.innerHTML = `<strong>Saved: ${{data.name}}</strong><br>${{data.address || ""}}<br>${{tags}}`;
    fallbackEl.style.display = "none";
  }} else {{
    resultEl.innerHTML = `Saved the link, but couldn't confidently match a place (status: ${{data.status}}).`;
    fallbackEl.style.display = "block";
  }}
}}

async function saveIt(note) {{
  const res = await fetch("/api/ingest", {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ source_url: sharedUrl, note: note || "" }}),
  }});
  if (res.status === 401) {{
    window.location.href = "/login";
    return null;
  }}
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Save failed");
  return data;
}}

async function retryIt(note) {{
  const res = await fetch("/api/retry", {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ place_id: placeId, note: note || "" }}),
  }});
  if (res.status === 401) {{
    window.location.href = "/login";
    return null;
  }}
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Retry failed");
  return data;
}}

(async () => {{
  try {{
    const data = await saveIt("");
    if (data) renderResult(data);
  }} catch (err) {{
    document.getElementById("status").style.display = "none";
    document.getElementById("result").innerHTML = `<span style="color:#ff6b6b">Error: ${{err.message}}</span>`;
    document.getElementById("fallback").style.display = "block";
  }}
}})();

document.getElementById("retry-btn").addEventListener("click", async () => {{
  const btn = document.getElementById("retry-btn");
  btn.disabled = true;
  btn.textContent = "Retrying...";
  try {{
    const data = await retryIt(document.getElementById("note").value);
    if (data) renderResult(data);
  }} catch (err) {{
    document.getElementById("result").innerHTML = `<span style="color:#ff6b6b">Error: ${{err.message}}</span>`;
  }} finally {{
    btn.disabled = false;
    btn.textContent = "Retry";
  }}
}});
</script>
</body>
</html>"""
