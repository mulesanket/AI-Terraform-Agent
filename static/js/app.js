/* ============================================================
   Self-Healing Terraform Agent — Frontend JavaScript
   ============================================================ */

// ---- API helpers (session-based auth via cookies) ----

async function apiGet(endpoint) {
  const resp = await fetch(endpoint, {
    credentials: 'same-origin',
  });
  if (resp.status === 401 || resp.status === 302) {
    window.location.href = '/auth/login';
    return { ok: false, error: 'Session expired' };
  }
  return resp.json();
}

async function apiPost(endpoint, body) {
  const resp = await fetch(endpoint, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'same-origin',
    body: JSON.stringify(body || {}),
  });
  if (resp.status === 401 || resp.status === 302) {
    window.location.href = '/auth/login';
    return { ok: false, error: 'Session expired' };
  }
  return resp.json();
}

// ---- UI helpers ----

function showAlert(id, message, type) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = 'alert alert-' + (type === 'error' ? 'error' : type === 'success' ? 'success' : 'info');
  el.textContent = message;
  el.style.display = 'block';
  if (type === 'success') {
    setTimeout(() => { el.style.display = 'none'; }, 5000);
  }
}

function logAppend(msg, cls) {
  const log = document.getElementById('activity-log');
  if (!log) return;
  if (log.textContent === 'Waiting for activity...') log.textContent = '';
  const span = document.createElement('span');
  if (cls) span.className = 'log-' + cls;
  const ts = new Date().toLocaleTimeString();
  span.textContent = '[' + ts + '] ' + msg + '\n';
  log.appendChild(span);
  log.scrollTop = log.scrollHeight;
}

// ---- Health check ----

async function checkHealth() {
  const el = document.getElementById('health-status');
  if (!el) return;
  try {
    const resp = await fetch('/health');
    const data = await resp.json();
    if (data.ok) {
      el.innerHTML = '<span style="color:var(--green);">&#9679;</span> Online';
    } else {
      el.innerHTML = '<span style="color:var(--red);">&#9679;</span> Error';
    }
  } catch {
    el.innerHTML = '<span style="color:var(--red);">&#9679;</span> Offline';
  }
}
