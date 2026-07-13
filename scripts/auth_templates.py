"""
auth_templates.py

HTML templates for the login and signup pages, styled to match
share_target_template.py and web/index.html's dark theme. Plain
.format()-style strings (stdlib only, no templating engine) -- note the
doubled {{ }} for literal JS/CSS braces.
"""

_BASE_STYLE = """
  :root { --bg: #0f1115; --panel: #171a21; --border: #2a2e38; --text: #e8eaed; --muted: #9aa0ac; --accent: #4f8cff; --error: #ff6b6b; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--text); padding: 20px;
  }
  .card { width: 100%; max-width: 340px; background: var(--panel); border: 1px solid var(--border); border-radius: 14px; padding: 28px; }
  h1 { font-size: 20px; margin: 0 0 4px 0; text-align: center; }
  p.hint { color: var(--muted); font-size: 13px; margin: 0 0 20px 0; text-align: center; }
  label { display: block; font-size: 12px; color: var(--muted); margin: 12px 0 4px 0; }
  input {
    width: 100%; padding: 10px; border-radius: 8px; border: 1px solid var(--border);
    background: #10131a; color: var(--text); font-size: 14px;
  }
  button {
    margin-top: 20px; width: 100%; padding: 12px; border-radius: 8px; border: none;
    background: var(--accent); color: #fff; font-size: 15px; font-weight: 600; cursor: pointer;
  }
  button:disabled { opacity: 0.5; }
  #error { margin-top: 12px; font-size: 13px; color: var(--error); text-align: center; display: none; }
  .switch-link { margin-top: 18px; font-size: 13px; text-align: center; color: var(--muted); }
  .switch-link a { color: var(--accent); text-decoration: none; }
"""

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Log in — SaveMe</title>
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon-192.png">
<meta name="theme-color" content="#4f8cff">
<link rel="apple-touch-icon" href="/icon-192.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="SaveMe">
<style>{style}</style>
</head>
<body>
  <div class="card">
    <h1>📍 SaveMe</h1>
    <p class="hint">Log in to see your saved places</p>
    <form id="login-form">
      <label for="username">Username</label>
      <input id="username" name="username" autocomplete="username" required>
      <label for="password">Password</label>
      <input id="password" name="password" type="password" autocomplete="current-password" required>
      <button type="submit" id="submit-btn">Log in</button>
    </form>
    <div id="error"></div>
    <div class="switch-link">New here? <a href="/signup">Create an account</a></div>
  </div>
<script>
document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = document.getElementById("submit-btn");
  const errorEl = document.getElementById("error");
  errorEl.style.display = "none";
  btn.disabled = true;
  btn.textContent = "Logging in...";
  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.getElementById("username").value.trim(),
        password: document.getElementById("password").value,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Login failed");
    window.location.href = "/";
  } catch (err) {
    errorEl.textContent = err.message;
    errorEl.style.display = "block";
    btn.disabled = false;
    btn.textContent = "Log in";
  }
});
</script>
</body>
</html>
""".replace("{style}", _BASE_STYLE)

SIGNUP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sign up — SaveMe</title>
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon-192.png">
<meta name="theme-color" content="#4f8cff">
<link rel="apple-touch-icon" href="/icon-192.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="SaveMe">
<style>{style}</style>
</head>
<body>
  <div class="card">
    <h1>📍 SaveMe</h1>
    <p class="hint">Create an account — you'll need an invite code</p>
    <form id="signup-form">
      <label for="username">Username</label>
      <input id="username" name="username" autocomplete="username" required minlength="3">
      <label for="password">Password</label>
      <input id="password" name="password" type="password" autocomplete="new-password" required minlength="8">
      <label for="invite-code">Invite code</label>
      <input id="invite-code" name="invite_code" autocomplete="off" required>
      <button type="submit" id="submit-btn">Create account</button>
    </form>
    <div id="error"></div>
    <div class="switch-link">Already have an account? <a href="/login">Log in</a></div>
  </div>
<script>
document.getElementById("signup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = document.getElementById("submit-btn");
  const errorEl = document.getElementById("error");
  errorEl.style.display = "none";
  btn.disabled = true;
  btn.textContent = "Creating account...";
  try {
    const res = await fetch("/api/signup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.getElementById("username").value.trim(),
        password: document.getElementById("password").value,
        invite_code: document.getElementById("invite-code").value,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Signup failed");
    window.location.href = "/";
  } catch (err) {
    errorEl.textContent = err.message;
    errorEl.style.display = "block";
    btn.disabled = false;
    btn.textContent = "Create account";
  }
});
</script>
</body>
</html>
""".replace("{style}", _BASE_STYLE)
