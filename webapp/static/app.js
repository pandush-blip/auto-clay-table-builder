window.CSRF = window.CSRF || "";

async function apiGet(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(url + " -> " + response.status);
  return response.json();
}

async function apiPost(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": window.CSRF },
    body: JSON.stringify(body || {}),
  });
  let data;
  try { data = await response.json(); } catch (e) { data = {}; }
  if (!response.ok && !(data.errors && data.errors.length) && !data.message) {
    data.ok = false;
    data.errors = [url + " -> " + response.status];
  }
  return data;
}

function escapeHtml(value) {
  return String(value === undefined || value === null ? "" : value)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function postForm(url) {
  const form = document.createElement("form");
  form.method = "POST";
  form.action = url;
  const token = document.createElement("input");
  token.type = "hidden";
  token.name = "_csrf";
  token.value = window.CSRF;
  form.appendChild(token);
  document.body.appendChild(form);
  form.submit();
}
