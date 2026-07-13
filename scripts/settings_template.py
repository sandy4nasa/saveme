"""
settings_template.py

The "Settings" page: shows the user's personal, non-expiring API token and
step-by-step instructions for wiring it into an iOS Shortcut, so iPhone
users get a "Share to SaveMe" experience despite iOS Safari not supporting
the Web Share Target API that the Android PWA relies on (see
IMPLEMENTATION_PLAN.md's iOS support section for the full design).

The token itself is server-rendered directly into the page (not fetched via
a separate JS call) since the page already requires a valid session cookie
to view -- no extra API surface needed just to display it.
"""

SETTINGS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Settings — SaveMe</title>
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon-192.png">
<meta name="theme-color" content="#4f8cff">
<style>
  :root {{ --bg: #0f1115; --panel: #171a21; --border: #2a2e38; --text: #e8eaed; --muted: #9aa0ac; --accent: #4f8cff; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); padding: 16px; max-width: 640px; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin: 4px 0; }}
  h2 {{ font-size: 15px; margin: 24px 0 8px 0; }}
  p {{ font-size: 13px; line-height: 1.5; color: var(--muted); }}
  a.back {{ color: var(--accent); text-decoration: none; font-size: 13px; }}
  .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 14px; margin-bottom: 14px; }}
  .token-box {{ display: flex; gap: 8px; align-items: center; background: #10131a; border: 1px solid var(--border); border-radius: 8px; padding: 10px; font-size: 12px; font-family: monospace; word-break: break-all; }}
  .token-box span {{ flex: 1; }}
  button {{ padding: 8px 14px; border-radius: 8px; border: none; background: var(--accent); color: #fff; font-size: 13px; font-weight: 600; cursor: pointer; white-space: nowrap; }}
  button:active {{ opacity: 0.8; }}
  button.secondary {{ background: #232a3a; border: 1px solid var(--border); }}
  #copy-status {{ font-size: 12px; color: var(--accent); margin-top: 6px; min-height: 16px; }}
  ol {{ font-size: 13px; color: var(--muted); line-height: 1.7; padding-left: 20px; }}
  code {{ background: #10131a; padding: 1px 5px; border-radius: 4px; font-size: 12px; }}
  .warn {{ color: #f5c518; font-size: 12px; margin-top: 8px; }}
</style>
</head>
<body>
  <a class="back" href="/">&larr; Back to map</a>
  <h1>⚙️ Settings</h1>

  <div class="card">
    <h2 style="margin-top:0;">📱 Set up "Share to SaveMe" on iOS</h2>
    <p>iOS doesn't support the same "Share to app" feature Android uses, but a free
    Apple Shortcut gets you the same result from the native Share Sheet. This needs
    to be set up once per device.</p>

    <p style="color: var(--text); font-weight: 600; margin-bottom: 4px;">Step 1 — Copy your personal token</p>
    <div class="token-box">
      <span id="token-text">{api_token}</span>
    </div>
    <button onclick="copyToken()">Copy Token</button>
    <div id="copy-status"></div>

    <p style="color: var(--text); font-weight: 600; margin: 16px 0 4px 0;">Step 2 — Build the Shortcut (one time)</p>
    <p>Open the <b>Shortcuts</b> app on your iPhone → <b>+</b> new shortcut → add these
    actions in order:</p>
    <ol>
      <li>Tap the shortcut's settings (ⓘ) → enable <b>"Show in Share Sheet"</b> →
      set accepted types to <b>URLs</b> and <b>Safari web pages</b>.</li>
      <li><code>Get Dictionary from Input</code> — leave as default (captures the shared link).</li>
      <li><code>Get Dictionary Value</code> → key <code>link</code> (falls back to
      <code>Shortcut Input</code> as plain text if the dictionary step returns nothing —
      Instagram/YouTube share sheets usually pass a plain URL).</li>
      <li><code>Get Contents of URL</code>:
        <ul>
          <li>URL: <code>https://saveme.blog/api/ingest</code></li>
          <li>Method: <code>POST</code></li>
          <li>Headers: <code>Authorization</code> = <code>Bearer YOUR_TOKEN</code>
          (paste the token from Step 1 here), <code>Content-Type</code> = <code>application/json</code></li>
          <li>Request Body → JSON: <code>{{"source_url": [the URL/text from step above]}}</code></li>
        </ul>
      </li>
      <li><code>Show Notification</code> (or <code>Show Result</code>) with the response
      text, so you get quick confirmation it saved.</li>
    </ol>
    <p>Name it <b>"SaveMe"</b> and save. Your token is now baked into this Shortcut and
    doesn't need to be re-entered again on this device.</p>

    <p style="color: var(--text); font-weight: 600; margin: 16px 0 4px 0;">Step 3 — Use it</p>
    <p>From Instagram, YouTube, or Safari: tap <b>Share</b> on a post → tap
    <b>SaveMe</b> in the list (drag it up if it's under "More"). You'll get a
    confirmation showing what was saved.</p>

    <p style="color: var(--text); font-weight: 600; margin: 16px 0 4px 0;">Keep your token private</p>
    <p>Don't share an already-configured copy of the Shortcut with anyone -- your token
    travels with it. If you ever think it's leaked, regenerate it below (this immediately
    invalidates the old one, so you'd need to update the Shortcut's header with the new one).</p>
    <button class="secondary" onclick="regenerateToken()">Regenerate Token</button>
  </div>

<script>
function copyToken() {{
  const text = document.getElementById("token-text").textContent;
  navigator.clipboard.writeText(text).then(() => {{
    document.getElementById("copy-status").textContent = "Copied! Paste it into the Shortcut's Authorization header.";
  }}).catch(() => {{
    document.getElementById("copy-status").textContent = "Couldn't copy automatically -- long-press the token above to copy it manually.";
  }});
}}

async function regenerateToken() {{
  if (!confirm("This invalidates your current token. Any Shortcut using the old one will stop working until you paste in the new one. Continue?")) return;
  const res = await fetch("/api/regenerate-token", {{ method: "POST" }});
  if (res.status === 401) {{ window.location.href = "/login"; return; }}
  const data = await res.json();
  if (data.token) {{
    document.getElementById("token-text").textContent = data.token;
    document.getElementById("copy-status").textContent = "New token generated. Update it in your Shortcut's Authorization header.";
  }}
}}
</script>
</body>
</html>
"""
