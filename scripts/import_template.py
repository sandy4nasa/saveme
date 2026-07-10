#!/usr/bin/env python3
"""
import_template.py

HTML for the /import page -- lets a user upload their Instagram "Download
Your Information" export (.zip) to bulk-import their existing saved posts,
instead of sharing each one individually. Processing runs in the background
(see import_instagram.py), so this page just shows the status of the most
recent import job on load -- no live polling.
"""

IMPORT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Import from Instagram - SaveMe</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f1115; color: #e8e8ea; margin: 0; padding: 24px; }
  .wrap { max-width: 640px; margin: 0 auto; }
  h1 { font-size: 1.4rem; margin-bottom: 4px; }
  p.sub { color: #9a9aa2; margin-top: 0; }
  a.back { color: #7aa2f7; text-decoration: none; font-size: 0.9rem; }
  .card { background: #191b21; border: 1px solid #2a2d36; border-radius: 12px; padding: 20px; margin-top: 16px; }
  .card h2 { font-size: 1rem; margin-top: 0; }
  input[type=file] { color: #e8e8ea; margin: 12px 0; }
  button { background: #7aa2f7; color: #0f1115; border: none; border-radius: 8px; padding: 10px 18px; font-weight: 600; cursor: pointer; font-size: 0.95rem; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .status-row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #2a2d36; font-size: 0.9rem; }
  .status-row:last-child { border-bottom: none; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 0.8rem; font-weight: 600; }
  .badge.running { background: #3b3410; color: #f7d774; }
  .badge.done { background: #103b1e; color: #74f7a0; }
  .badge.error { background: #3b1010; color: #f77474; }
  .msg { margin-top: 10px; font-size: 0.9rem; color: #9a9aa2; }
  .help { font-size: 0.85rem; color: #9a9aa2; margin-top: 16px; line-height: 1.5; }
  .help a { color: #7aa2f7; }
</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="/">&larr; Back to dashboard</a>
  <h1>Import from Instagram</h1>
  <p class="sub">Bulk-import all your existing saved posts from an Instagram data export.</p>

  <div class="card">
    <h2>Upload your export</h2>
    <form id="importForm">
      <input type="file" id="fileInput" name="file" accept=".zip" required>
      <br>
      <button type="submit" id="submitBtn">Upload &amp; start import</button>
    </form>
    <div class="msg" id="uploadMsg"></div>
  </div>

  <div class="card" id="statusCard" style="display:none;">
    <h2>Latest import status</h2>
    <div id="statusBody"></div>
  </div>

  <div class="help">
    Don't have an export yet? In the Instagram app go to
    <b>Settings &rarr; Accounts Center &rarr; Your information and permissions &rarr; Download your information</b>,
    request a download for just your account, choose <b>JSON</b> format, and select
    at least the <b>"Saved"</b> category. Instagram will email/notify you when it's ready
    (can take a few minutes to a day) &mdash; download the .zip and upload it here.
    <br><br>
    This may take several minutes to process depending on how many posts you saved.
    You can close this page and come back later &mdash; your progress is saved automatically.
  </div>
</div>

<script>
async function loadStatus() {
  const res = await fetch('/api/import/status');
  if (res.status === 401) { window.location.href = '/login'; return; }
  const job = await res.json();
  const card = document.getElementById('statusCard');
  const body = document.getElementById('statusBody');
  if (!job) { card.style.display = 'none'; return; }
  card.style.display = 'block';

  const badgeClass = job.status === 'running' ? 'running' : (job.status === 'error' ? 'error' : 'done');
  const badgeText = job.status === 'running' ? 'Processing...' : (job.status === 'error' ? 'Error' : 'Done');

  let html = `<div class="status-row"><span>File</span><span>${job.filename || '(unknown)'}</span></div>`;
  html += `<div class="status-row"><span>Status</span><span class="badge ${badgeClass}">${badgeText}</span></div>`;
  html += `<div class="status-row"><span>Progress</span><span>${job.processed} / ${job.total}</span></div>`;
  html += `<div class="status-row"><span>Added &amp; ready</span><span>${job.ready_count}</span></div>`;
  html += `<div class="status-row"><span>Needs review</span><span>${job.needs_review_count}</span></div>`;
  html += `<div class="status-row"><span>Duplicates skipped</span><span>${job.skipped_duplicate}</span></div>`;
  if (job.status === 'error') {
    html += `<div class="status-row"><span>Error</span><span>${job.error_message || 'unknown error'}</span></div>`;
  }
  if (job.status === 'running') {
    html += `<div class="msg">Still processing &mdash; refresh this page later to check progress.</div>`;
  } else if (job.status === 'done' && job.needs_review_count > 0) {
    html += `<div class="msg">${job.needs_review_count} posts need a more specific note &mdash; visit <a href="/review" style="color:#7aa2f7;">Needs review</a> to fix them up.</div>`;
  }
  body.innerHTML = html;
}

document.getElementById('importForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fileInput = document.getElementById('fileInput');
  const submitBtn = document.getElementById('submitBtn');
  const msg = document.getElementById('uploadMsg');
  if (!fileInput.files.length) return;

  submitBtn.disabled = true;
  msg.textContent = 'Uploading...';

  try {
    const formData = new FormData();
    formData.append('file', fileInput.files[0]);
    const res = await fetch('/api/import', { method: 'POST', body: formData });
    const data = await res.json();
    if (res.status === 401) { window.location.href = '/login'; return; }
    if (!res.ok) {
      msg.textContent = 'Error: ' + (data.error || 'upload failed');
      submitBtn.disabled = false;
      return;
    }
    msg.textContent = `Import started (${data.total} posts found). Processing in the background -- check back here for progress.`;
    fileInput.value = '';
    setTimeout(loadStatus, 1500);
  } catch (err) {
    msg.textContent = 'Error: ' + err.message;
  }
  submitBtn.disabled = false;
});

loadStatus();
</script>
</body>
</html>
"""
