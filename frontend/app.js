// Photo System — Frontend JavaScript
// Uses native Fetch API — no build step, no framework needed.
const API = "http://localhost:8000";
const DEMO_USER = "00000000-0000-0000-0000-000000000001";

// ─── Tab Navigation ───────────────────────────────────────────────
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach(t => { t.classList.add("hidden"); t.classList.remove("active"); });
    btn.classList.add("active");
    const tab = document.getElementById(`tab-${btn.dataset.tab}`);
    tab.classList.remove("hidden");
    tab.classList.add("active");
  });
});

// ─── Image Preview on File Select ────────────────────────────────
document.getElementById("file-input").addEventListener("change", e => {
  const file = e.target.files[0];
  if (!file) return;
  const container = document.getElementById("preview-container");
  const img = document.getElementById("preview-img");
  img.src = URL.createObjectURL(file);
  container.style.display = "block";
});

// ─── UPLOAD ───────────────────────────────────────────────────────
document.getElementById("upload-form").addEventListener("submit", async e => {
  e.preventDefault();
  const btn = document.getElementById("upload-btn");
  const resultBox = document.getElementById("upload-result");
  const errorBox  = document.getElementById("upload-error");
  resultBox.classList.add("hidden");
  errorBox.classList.add("hidden");

  const file = document.getElementById("file-input").files[0];
  if (!file) return;

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Uploading…';

  const form = new FormData();
  form.append("file",       file);
  form.append("title",      document.getElementById("title-input").value);
  form.append("user_id",    document.getElementById("user-id-input").value);
  const productId = document.getElementById("product-id-input").value.trim();
  if (productId) form.append("product_id", productId);

  try {
    const res = await fetch(`${API}/api/photos/upload`, { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || JSON.stringify(data));

    // Render result
    const details = document.getElementById("upload-details");
    details.innerHTML = `
      <span class="key">Photo ID</span>   <span class="val">${data.id}</span>
      <span class="key">Title</span>      <span class="val">${data.title}</span>
      <span class="key">Product ID</span> <span class="val">${data.product_id || "—"}</span>
      <span class="key">Size</span>       <span class="val">${(data.size_bytes/1024).toFixed(1)} KB</span>
      <span class="key">Tier</span>       <span class="val"><span class="tier-badge tier-${data.storage_tier}">🔥 ${data.storage_tier}</span></span>
      <span class="key">View URL</span>   <span class="val"><a href="${data.url}" target="_blank">Open presigned URL ↗</a></span>
    `;
    resultBox.classList.remove("hidden");

    // Pre-fill view tab with this ID
    document.getElementById("view-id-input").value = data.id;

  } catch (err) {
    errorBox.textContent = `❌ Upload failed: ${err.message}`;
    errorBox.classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.innerHTML = "⬆ Upload Photo";
  }
});

// ─── VIEW BY ID ────────────────────────────────────────────────────
document.getElementById("view-btn").addEventListener("click", async () => {
  const photoId   = document.getElementById("view-id-input").value.trim();
  const resultBox = document.getElementById("view-result");
  const errorBox  = document.getElementById("view-error");
  resultBox.classList.add("hidden");
  errorBox.classList.add("hidden");

  if (!photoId) { showError(errorBox, "Please enter a Photo ID"); return; }

  try {
    const res  = await fetch(`${API}/api/photos/${photoId}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || JSON.stringify(data));

    document.getElementById("view-img").src = data.url;
    document.getElementById("view-details").innerHTML = `
      <div class="result-grid">
        <span class="key">ID</span>              <span class="val">${data.id}</span>
        <span class="key">Title</span>           <span class="val">${data.title}</span>
        <span class="key">Product ID</span>      <span class="val">${data.product_id || "—"}</span>
        <span class="key">Size</span>            <span class="val">${(data.size_bytes/1024).toFixed(1)} KB</span>
        <span class="key">Content Type</span>   <span class="val">${data.content_type}</span>
        <span class="key">Storage Tier</span>   <span class="val"><span class="tier-badge tier-${data.storage_tier}">${tierIcon(data.storage_tier)} ${data.storage_tier}</span></span>
        <span class="key">Uploaded</span>        <span class="val">${formatDate(data.created_at)}</span>
        <span class="key">Last Accessed</span>  <span class="val">${formatDate(data.last_accessed_at)}</span>
        <span class="key">Presigned URL</span>  <span class="val"><a href="${data.url}" target="_blank">Download ↗</a></span>
      </div>
    `;
    resultBox.classList.remove("hidden");
  } catch (err) {
    showError(errorBox, `❌ Fetch failed: ${err.message}`);
  }
});

// ─── SEARCH ────────────────────────────────────────────────────────
document.getElementById("search-btn").addEventListener("click", async () => {
  const q         = document.getElementById("search-q").value.trim();
  const productId = document.getElementById("search-product-id").value.trim();
  const container = document.getElementById("search-results");
  const errorBox  = document.getElementById("search-error");
  container.classList.add("hidden");
  errorBox.classList.add("hidden");

  if (!q && !productId) { showError(errorBox, "Enter a search query or product ID"); return; }

  const params = new URLSearchParams();
  if (q)         params.set("q",          q);
  if (productId) params.set("product_id", productId);
  params.set("page", "1");
  params.set("size", "20");

  try {
    const res  = await fetch(`${API}/api/photos/search?${params}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || JSON.stringify(data));

    if (data.total === 0) {
      container.innerHTML = `<p style="color:var(--text-muted)">No results found. Try uploading some photos first!</p>`;
      container.classList.remove("hidden");
      return;
    }

    container.innerHTML = data.results.map(photo => `
      <div class="search-card">
        <img src="${photo.url}" alt="${escHtml(photo.title)}"
             onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 width=%22220%22 height=%22140%22><rect fill=%22%23252836%22 width=%22220%22 height=%22140%22/><text fill=%22%238892a4%22 x=%2250%%22 y=%2250%%22 text-anchor=%22middle%22 dy=%22.3em%22>📷</text></svg>'" />
        <div class="card-body">
          <div class="card-title">${escHtml(photo.title)}</div>
          <div class="card-meta">
            ${photo.product_id ? `<span>🏷 ${escHtml(photo.product_id)}</span><br>` : ""}
            <span>${(photo.size_bytes/1024).toFixed(1)} KB · <span class="tier-badge tier-${photo.storage_tier}" style="font-size:0.68rem;padding:0.15rem 0.5rem">${tierIcon(photo.storage_tier)} ${photo.storage_tier}</span></span>
          </div>
          <div class="card-score">ES score: ${photo.score.toFixed(3)}</div>
        </div>
      </div>
    `).join("");
    container.classList.remove("hidden");

  } catch (err) {
    showError(errorBox, `❌ Search failed: ${err.message}`);
  }
});

// Allow Enter key on search inputs
["search-q", "search-product-id"].forEach(id => {
  document.getElementById(id).addEventListener("keydown", e => {
    if (e.key === "Enter") document.getElementById("search-btn").click();
  });
});

// ─── Utilities ────────────────────────────────────────────────────
function showError(el, msg) {
  el.textContent = msg;
  el.classList.remove("hidden");
}

function formatDate(iso) {
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

function escHtml(str) {
  return String(str).replace(/[&<>"']/g, c =>
    ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c])
  );
}

function tierIcon(tier) {
  return { HOT: "🔥", WARM: "🌡", COLD: "🧊" }[tier] || "";
}
