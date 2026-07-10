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
  #result {{ margin-top: 16px; font-size: 14px; line-height: 1.5; }}
  #result .tag {{ display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 6px; background: #2a2e38; color: var(--muted); margin: 2px; }}
  a.back {{ display: inline-block; margin-top: 16px; color: var(--accent); font-size: 13px; text-decoration: none; }}
</style>
</head>
<body>
  <h1>📍 Save to SaveMe</h1>
  <p class="hint">Instagram doesn't share the caption, just the link -- add a quick note (place name, or paste part of the caption) so we can find and tag it.</p>
  <div class="url-box">{shared_url}</div>
  <form id="save-form">
    <textarea id="note" placeholder="e.g. Third Wave Coffee, Jayanagar - great filter coffee and cozy seating #coffee"></textarea>
    <button type="submit" id="submit-btn">Save it</button>
  </form>
  <div id="result"></div>
  <a class="back" href="/">&larr; Back to map</a>

<script>
document.getElementById("save-form").addEventListener("submit", async (e) => {{
  e.preventDefault();
  const btn = document.getElementById("submit-btn");
  const resultEl = document.getElementById("result");
  btn.disabled = true;
  btn.textContent = "Saving...";
  resultEl.textContent = "";

  try {{
    const res = await fetch("/api/ingest", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ source_url: {shared_url_json}, note: document.getElementById("note").value }}),
    }});
    if (res.status === 401) {{
      window.location.href = "/login";
      return;
    }}
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Save failed");

    if (data.status === "ready") {{
      const tags = (data.tags || []).map(t => `<span class="tag">${{t}}</span>`).join(" ");
      resultEl.innerHTML = `<strong>Saved: ${{data.name}}</strong><br>${{data.address || ""}}<br>${{tags}}`;
    }} else if (data.status === "needs_manual_caption") {{
      resultEl.innerHTML = `Saved the link. Add a note later from the dashboard to enrich it with a real place match.`;
    }} else {{
      resultEl.innerHTML = `Saved, but couldn't confidently match a place (status: ${{data.status}}). You can edit it later from the dashboard.`;
    }}
    btn.textContent = "Saved \u2713";
  }} catch (err) {{
    resultEl.innerHTML = `<span style="color:#ff6b6b">Error: ${{err.message}}</span>`;
    btn.disabled = false;
    btn.textContent = "Save it";
  }}
}});
</script>
</body>
</html>"""
