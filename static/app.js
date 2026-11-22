/* ------------------------------------------------------------------ *
 * TubeGrab — frontend logic
 * ------------------------------------------------------------------ */

const $ = (sel, root = document) => root.querySelector(sel);

const els = {
  form: $("#fetchForm"),
  url: $("#url"),
  fetchBtn: $("#fetchBtn"),
  pasteBtn: $("#pasteBtn"),
  result: $("#result"),
  jobs: $("#jobs"),
  emptyJobs: $("#emptyJobs"),
  clearBtn: $("#clearBtn"),
  toasts: $("#toasts"),
};

let info = null;
let currentUrl = "";
let selection = { height: null, audio: false };
let fetching = false;

/** jobId -> { el, timer, job } */
const cards = new Map();

/* ------------------------------- helpers ------------------------------- */

async function api(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 90000);
  let res;
  try {
    res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      ...options,
    });
  } catch (err) {
    if (err.name === "AbortError") {
      throw new Error("The request timed out. Please try again.");
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* non-JSON response */
  }
  if (!res.ok) {
    const message =
      (data && (data.detail || data.message)) || `Request failed (${res.status})`;
    throw new Error(message);
  }
  return data;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}

function fmtBytes(n) {
  if (!n) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
}

function fmtSpeed(bytesPerSec) {
  if (!bytesPerSec) return "";
  const mb = bytesPerSec / (1024 * 1024);
  return mb >= 1 ? `${mb.toFixed(1)} MB/s` : `${Math.round(bytesPerSec / 1024)} KB/s`;
}

function fmtEta(seconds) {
  if (!seconds || seconds < 0) return "";
  if (seconds < 60) return `${Math.round(seconds)}s left`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s}s left`;
}

function qualityTag(h) {
  if (h >= 4320) return "8K";
  if (h >= 2160) return "4K";
  if (h >= 1440) return "2K";
  if (h >= 1080) return "FULL HD";
  if (h >= 720) return "HD";
  return "";
}

function toast(message, type = "error") {
  const el = document.createElement("div");
  el.className = `toast${type === "ok" ? " ok" : ""}`;
  el.textContent = message;
  els.toasts.appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .25s, transform .25s";
    el.style.opacity = "0";
    el.style.transform = "translateY(6px)";
    setTimeout(() => el.remove(), 260);
  }, 5200);
}

/* -------------------------------- fetch -------------------------------- */

function setBusy(busy) {
  els.fetchBtn.disabled = busy;
  els.fetchBtn.querySelector(".btn-label").textContent = busy ? "Fetching" : "Fetch";
  els.fetchBtn.querySelector(".spinner").hidden = !busy;
}

els.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (fetching) return;

  const url = els.url.value.trim();
  if (!url) {
    els.url.focus();
    return;
  }

  fetching = true;
  setBusy(true);

  try {
    info = await api("/api/info", {
      method: "POST",
      body: JSON.stringify({ url }),
    });
    currentUrl = url;

    const preferred =
      info.qualities.find((q) => q.height <= 1080) || info.qualities[0];
    selection = {
      height: preferred ? preferred.height : null,
      audio: false,
      bitrate: null,
    };

    renderResult(info);
    els.result.classList.remove("hidden");
    els.result.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (err) {
    toast(err.message);
  } finally {
    fetching = false;
    setBusy(false);
  }
});

els.pasteBtn.addEventListener("click", async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      els.url.value = text.trim();
      els.url.focus();
    }
  } catch {
    els.url.focus();
    toast("Clipboard access was blocked — paste with Ctrl+V instead.");
  }
});

/* ------------------------------ result card ---------------------------- */

function renderResult(data) {
  const qualities = data.qualities
    .map((q) => {
      const tag = qualityTag(q.height);
      const size = q.filesize_text
        ? `${q.approximate ? "~" : ""}${q.filesize_text}`
        : "size varies";
      return `
        <button type="button" class="q" data-height="${q.height}" title="${esc(q.note || q.label)}">
          ${tag ? `<span class="q-tag">${tag}</span>` : ""}
          <span class="q-label">${esc(q.label)}</span>
          <span class="q-size">${esc(size)}</span>
        </button>`;
    })
    .join("");

  const audioChips = (data.audio || [])
    .map((a) => {
      const size = a.filesize_text ? `~${a.filesize_text}` : "size varies";
      return `
        <button type="button" class="q audio" data-bitrate="${a.bitrate}" title="MP3 ${a.bitrate} kbps">
          <span class="q-tag">MP3</span>
          <span class="q-label">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18V6l10-2v12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><circle cx="6.5" cy="18" r="2.5" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="16.5" cy="16" r="2.5" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>
            ${a.bitrate} kbps
          </span>
          <span class="q-size">${esc(size)}</span>
        </button>`;
    })
    .join("");

  const byline = [
    esc(data.uploader),
    data.view_count_text ? `${data.view_count_text} views` : null,
    data.upload_date ? formatUploadDate(data.upload_date) : null,
  ]
    .filter(Boolean)
    .join('<span class="sep">·</span>');

  els.result.innerHTML = `
    <div class="video-head">
      <div class="thumb">
        <img src="${esc(data.thumbnail || "")}" alt="" referrerpolicy="no-referrer" />
        <span class="time">${esc(data.duration_text)}</span>
      </div>
      <div class="video-meta">
        <h3>${esc(data.title)}</h3>
        <div class="byline">${byline}</div>
      </div>
    </div>

    <div class="picker">
      <div class="picker-head">
        <h4>Choose quality</h4>
        <span class="note">MP4 · H.264 preferred</span>
      </div>
      <div class="quality-grid">
        ${qualities}
      </div>

      <div class="audio-head">
        <h4>Audio only</h4>
        <span class="note">MP3 · extracted with ffmpeg</span>
      </div>
      <div class="audio-grid">
        ${audioChips}
      </div>

      <div class="download-row">
        <button type="button" id="dlBtn" class="btn primary">Download</button>
        <span class="selection-hint" id="selHint"></span>
      </div>
    </div>
  `;

  els.result.querySelector("#dlBtn").addEventListener("click", startDownload);
  syncSelection();
}

function formatUploadDate(yyyymmdd) {
  const s = String(yyyymmdd);
  if (s.length !== 8) return "";
  const d = new Date(`${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}T00:00:00`);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function syncSelection() {
  if (!info) return;

  els.result.querySelectorAll(".q").forEach((btn) => {
    const bitrate = btn.dataset.bitrate;
    const selected =
      bitrate !== undefined
        ? selection.audio && Number(bitrate) === selection.bitrate
        : !selection.audio && Number(btn.dataset.height) === selection.height;
    btn.classList.toggle("selected", selected);
  });

  const dlBtn = els.result.querySelector("#dlBtn");
  const hint = els.result.querySelector("#selHint");
  if (!dlBtn) return;

  if (selection.audio) {
    dlBtn.textContent = `Download MP3 · ${selection.bitrate} kbps`;
    hint.textContent = "Audio extracted with ffmpeg";
  } else {
    const q = info.qualities.find((x) => x.height === selection.height);
    dlBtn.textContent = q ? `Download MP4 · ${q.label}` : "Download MP4";
    hint.textContent = "Video and audio merged into one MP4";
  }
}

els.result.addEventListener("click", (event) => {
  const chip = event.target.closest(".q");
  if (!chip) return;
  if (chip.dataset.bitrate !== undefined) {
    selection = { height: null, audio: true, bitrate: Number(chip.dataset.bitrate) };
  } else {
    selection = { height: Number(chip.dataset.height), audio: false, bitrate: null };
  }
  syncSelection();
});

/* ------------------------------ downloads ------------------------------ */

async function startDownload() {
  if (!info) return;
  const btn = els.result.querySelector("#dlBtn");
  if (!btn || btn.disabled) return;

  btn.disabled = true;
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        url: currentUrl,
        height: selection.audio ? null : selection.height,
        audio_only: selection.audio,
        audio_bitrate: selection.audio ? selection.bitrate : null,
      }),
    });
    addJobCard(job);
    pollJob(job.id);
    toast("Download started", "ok");
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
  }
}

async function saveFile(jobId, filename, size, btn) {
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = "Saving...";
  // For large files, avoid loading everything into memory.
  if (size > 50 * 1024 * 1024) {
    window.open(`/api/jobs/${jobId}/file`, "_blank");
    btn.disabled = false;
    btn.innerHTML = original;
    return;
  }
  try {
    const res = await fetch(`/api/jobs/${jobId}/file`);
    if (!res.ok) throw new Error("Save failed");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    toast("Saved", "ok");
  } catch (err) {
    toast(err.message || "Save failed");
    // Last-resort fallback.
    window.open(`/api/jobs/${jobId}/file`, "_blank");
  } finally {
    btn.disabled = false;
    btn.innerHTML = original;
  }
}

function addJobCard(job) {
  if (cards.has(job.id)) return;

  const el = document.createElement("article");
  el.className = "job";
  el.dataset.id = job.id;
  el.innerHTML = `
    <div class="job-thumb">
      ${job.thumbnail ? `<img src="${esc(job.thumbnail)}" alt="" referrerpolicy="no-referrer" />` : ""}
    </div>
    <div class="job-main">
      <div class="job-title">${esc(job.title)}</div>
      <div class="job-sub"></div>
      <div class="bar"><span></span></div>
      <div class="job-status">
        <span class="left">Starting…</span>
        <span class="right"></span>
      </div>
    </div>
    <div class="job-actions"></div>
  `;

  els.jobs.prepend(el);
  els.emptyJobs.classList.add("hidden");
  cards.set(job.id, { el, timer: null, job });
  updateJobCard(job);
  updateEmptyState();
}

function updateJobCard(job) {
  const card = cards.get(job.id);
  if (!card) return;
  card.job = job;

  const { el } = card;
  const bar = el.querySelector(".bar > span");
  const sub = el.querySelector(".job-sub");
  const left = el.querySelector(".job-status .left");
  const right = el.querySelector(".job-status .right");
  const actions = el.querySelector(".job-actions");

  const quality = job.audio_only
    ? `MP3 ${job.audio_bitrate || 192} kbps`
    : `${job.height ? `${job.height}p` : "Best"} · MP4`;

  el.classList.toggle("done", job.status === "finished");
  el.classList.toggle("error", job.status === "error");

  sub.textContent = [quality, job.size ? fmtBytes(job.size) : ""]
    .filter(Boolean)
    .join(" · ");

  if (job.status === "finished") {
    bar.style.width = "100%";
    left.textContent = "Ready";
    right.textContent = job.duration_text || "";
    actions.innerHTML = `
      <button type="button" class="btn-save" data-save="${job.id}" data-name="${esc(job.filename)}" data-size="${job.size || 0}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0l-4.5-4.5M12 15l4.5-4.5M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>
        Save
      </button>
      <button class="icon-btn" data-del title="Remove" aria-label="Remove">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>`;
    return;
  }

  if (job.status === "error") {
    bar.style.width = "0%";
    left.textContent = job.error || "Download failed";
    right.textContent = "";
    actions.innerHTML = `
      <button class="icon-btn" data-del title="Remove" aria-label="Remove">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>`;
    return;
  }

  const pct = Math.max(0, Math.min(job.progress || 0, 100));
  bar.style.width = `${job.status === "queued" ? 2 : Math.max(pct, 2)}%`;
  if (job.status === "processing") {
    left.textContent = job.message || "Processing…";
  } else if (job.status === "queued") {
    left.textContent = "Queued…";
  } else {
    left.textContent = `${pct.toFixed(0)}% · ${job.message || "Downloading…"}`;
  }
  right.textContent =
    job.status === "downloading"
      ? [fmtSpeed(job.speed), fmtEta(job.eta)].filter(Boolean).join(" · ")
      : "";
  actions.innerHTML = `
    <button class="icon-btn" data-del title="Cancel" aria-label="Cancel">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
    </button>`;
}

function pollJob(id) {
  const card = cards.get(id);
  if (!card) return;

  const tick = async () => {
    if (!cards.has(id)) return;
    try {
      const job = await api(`/api/jobs/${id}`);
      updateJobCard(job);
      updateEmptyState();
      if (job.status === "finished") {
        toast(`Ready to save: ${job.filename}`, "ok");
        return;
      }
      if (job.status === "error") return;
    } catch (err) {
      if (/not found|expired/i.test(err.message)) return;
    }
    const current = cards.get(id);
    if (current) current.timer = setTimeout(tick, 900);
  };

  card.timer = setTimeout(tick, 600);
}

els.jobs.addEventListener("click", async (event) => {
  const save = event.target.closest("[data-save]");
  if (save) {
    event.preventDefault();
    const jobId = save.dataset.save;
    const filename = save.dataset.name || "download";
    const size = Number(save.dataset.size) || 0;
    await saveFile(jobId, filename, size, save);
    return;
  }

  const del = event.target.closest("[data-del]");
  if (!del) return;
  const el = del.closest(".job");
  if (!el) return;
  const id = el.dataset.id;
  const card = cards.get(id);
  if (card?.timer) clearTimeout(card.timer);
  cards.delete(id);
  el.remove();
  updateEmptyState();
  try {
    await fetch(`/api/jobs/${id}`, { method: "DELETE" });
  } catch {
    /* ignore */
  }
});

els.clearBtn.addEventListener("click", async () => {
  for (const [id, card] of [...cards.entries()]) {
    if (card.job?.status !== "finished" && card.job?.status !== "error") continue;
    if (card.timer) clearTimeout(card.timer);
    cards.delete(id);
    card.el.remove();
    try {
      await fetch(`/api/jobs/${id}`, { method: "DELETE" });
    } catch {
      /* ignore */
    }
  }
  updateEmptyState();
});

function updateEmptyState() {
  els.emptyJobs.classList.toggle("hidden", cards.size > 0);
  const hasFinished = [...cards.values()].some(
    (c) => c.job?.status === "finished" || c.job?.status === "error"
  );
  els.clearBtn.classList.toggle("hidden", !hasFinished);
}

/* --------------------------- service worker (PWA) --------------------------- */

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      /* ignore registration failures */
    });
  });
}

async function loadExistingJobs() {
  try {
    const data = await api("/api/jobs");
    (data.jobs || []).forEach((job) => {
      addJobCard(job);
      if (job.status !== "finished" && job.status !== "error") pollJob(job.id);
    });
    updateEmptyState();
  } catch {
    /* server not reachable — ignore */
  }
}

/* --------------------------------- init -------------------------------- */

els.url.focus();
loadExistingJobs();
