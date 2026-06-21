const state = {
  status: "",
  warning: "",
  search: "",
  offset: 0,
  limit: 300,
  view: "photos",
  photos: [],
  photoCount: 0,
  orphanRaws: [],
  selected: null,
  selectedIndex: -1,
  library: "",
  librarySelected: false,
  browsePath: "",
  browseParent: null,
};

const grid = document.querySelector("#grid");
const summary = document.querySelector("#summary");
const loadMore = document.querySelector("#loadMore");
const viewer = document.querySelector("#viewer");
const imageStage = document.querySelector("#imageStage");
const viewerImg = document.querySelector("#viewerImg");
const viewerName = document.querySelector("#viewerName");
const viewerPosition = document.querySelector("#viewerPosition");
const viewerMeta = document.querySelector("#viewerMeta");
const viewerExposure = document.querySelector("#viewerExposure");
const viewerBrandBadges = document.querySelector("#viewerBrandBadges");
const viewerSource = document.querySelector("#viewerSource");
const scanBtn = document.querySelector("#scanBtn");
const scanControlBtn = document.querySelector("#scanControlBtn");
const chooseFolderBtn = document.querySelector("#chooseFolderBtn");
const folderDialog = document.querySelector("#folderDialog");
const folderPath = document.querySelector("#folderPath");
const folderList = document.querySelector("#folderList");
const folderShortcuts = document.querySelector("#folderShortcuts");
const folderUpBtn = document.querySelector("#folderUpBtn");
const useFolderBtn = document.querySelector("#useFolderBtn");
const zoomState = {
  scale: 1,
  x: 0,
  y: 0,
  dragging: false,
  gesturing: false,
  startX: 0,
  startY: 0,
  baseX: 0,
  baseY: 0,
  gestureStartScale: 1,
  gestureLastScale: 1,
  wheelZoomEventCount: 0,
  wheelZoomResetTimer: 0,
  fitScale: 1,
  frame: 0,
  prewarmFrame: 0,
  loadToken: 0,
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok || payload.error) {
    throw new Error(payload.error || "Request failed");
  }
  return payload;
}

function formatScore(score) {
  return score == null ? "n/a" : Math.round(score);
}

function focusScore(photo) {
  return photo.focusScore ?? photo.blurScore;
}

const WARNING_LABELS = {
  focus_risk: "focus risk",
  soft: "focus risk",
  no_raw_pair: "no RAW pair",
};

function formatWarning(warning) {
  return WARNING_LABELS[warning] || warning.replaceAll("_", " ");
}

function setLibrary(path) {
  state.library = path;
  state.librarySelected = true;
  document.querySelector("#libraryPath").textContent = path;
}

function showChooseFolderPrompt() {
  grid.innerHTML = "";
  loadMore.hidden = true;
  summary.textContent = "Choose a folder to start scanning photos.";
}

function syncLoadMoreVisibility(count, loadedAny = true) {
  const loaded = state.view === "orphan_raws" ? state.orphanRaws.length : state.photos.length;
  loadMore.hidden = !loadedAny || loaded >= count;
}

function renderFolderButton(item) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = item.name;
  button.title = item.path;
  button.addEventListener("click", () => loadFolder(item.path));
  return button;
}

function renderFolderBrowser(payload) {
  state.browsePath = payload.path;
  state.browseParent = payload.parent;
  folderPath.textContent = payload.path;
  folderUpBtn.disabled = !payload.parent;
  folderShortcuts.replaceChildren(...payload.shortcuts.map(renderFolderButton));
  const fragment = document.createDocumentFragment();
  payload.directories.forEach((directory) => {
    const button = renderFolderButton(directory);
    button.classList.add("folderItem");
    fragment.appendChild(button);
  });
  folderList.replaceChildren(fragment);
  if (!payload.directories.length) {
    const empty = document.createElement("div");
    empty.className = "emptyFolder";
    empty.textContent = "No subfolders";
    folderList.appendChild(empty);
  }
}

async function loadFolder(path = state.library) {
  const params = new URLSearchParams();
  if (path) params.set("path", path);
  const payload = await api(`/api/browse?${params}`);
  renderFolderBrowser(payload);
}

function applyZoom() {
  if (zoomState.frame) return;
  zoomState.frame = requestAnimationFrame(() => {
    zoomState.frame = 0;
    clampPan();
    viewerImg.style.transform = `translate3d(calc(-50% + ${zoomState.x}px), calc(-50% + ${zoomState.y}px), 0) scale(${zoomState.scale})`;
  });
}

function applyZoomNow() {
  if (zoomState.frame) {
    cancelAnimationFrame(zoomState.frame);
    zoomState.frame = 0;
  }
  clampPan();
  viewerImg.style.transform = `translate3d(calc(-50% + ${zoomState.x}px), calc(-50% + ${zoomState.y}px), 0) scale(${zoomState.scale})`;
}

function clampPan() {
  const rect = imageStage.getBoundingClientRect();
  const width = viewerImg.naturalWidth * zoomState.scale;
  const height = viewerImg.naturalHeight * zoomState.scale;
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return;

  const maxX = Math.max(0, (width - rect.width) / 2);
  const maxY = Math.max(0, (height - rect.height) / 2);
  zoomState.x = Math.min(Math.max(zoomState.x, -maxX), maxX);
  zoomState.y = Math.min(Math.max(zoomState.y, -maxY), maxY);
}

function prewarmZoomLayer() {
  if (zoomState.prewarmFrame) {
    cancelAnimationFrame(zoomState.prewarmFrame);
    zoomState.prewarmFrame = 0;
  }
  const originalScale = zoomState.scale;
  const originalX = zoomState.x;
  const originalY = zoomState.y;
  zoomState.prewarmFrame = requestAnimationFrame(() => {
    zoomState.scale = originalScale * 1.015;
    applyZoomNow();
    viewerImg.getBoundingClientRect();
    zoomState.prewarmFrame = requestAnimationFrame(() => {
      zoomState.scale = originalScale;
      zoomState.x = originalX;
      zoomState.y = originalY;
      applyZoomNow();
      viewerImg.getBoundingClientRect();
      zoomState.prewarmFrame = 0;
    });
  });
}

function panBy(deltaX, deltaY) {
  zoomState.x += deltaX;
  zoomState.y += deltaY;
  applyZoom();
}

function setZoom(scale, originX = 0, originY = 0) {
  const previous = zoomState.scale;
  const minScale = zoomState.fitScale > 0 ? zoomState.fitScale : 0.05;
  const next = Math.min(Math.max(scale, minScale), 8);
  if (next === previous) return;
  if (originX || originY) {
    const ratio = next / previous;
    zoomState.x = originX - (originX - zoomState.x) * ratio;
    zoomState.y = originY - (originY - zoomState.y) * ratio;
  }
  zoomState.scale = next;
  applyZoom();
}

function resetWheelZoomGesture() {
  zoomState.wheelZoomEventCount = 0;
  if (zoomState.wheelZoomResetTimer) {
    clearTimeout(zoomState.wheelZoomResetTimer);
  }
  zoomState.wheelZoomResetTimer = setTimeout(() => {
    zoomState.wheelZoomEventCount = 0;
    zoomState.wheelZoomResetTimer = 0;
  }, 180);
}

function fitZoom() {
  const rect = imageStage.getBoundingClientRect();
  const fit = Math.min(rect.width / viewerImg.naturalWidth, rect.height / viewerImg.naturalHeight, 1);
  zoomState.fitScale = Number.isFinite(fit) && fit > 0 ? fit : 1;
  zoomState.scale = zoomState.fitScale;
  zoomState.x = 0;
  zoomState.y = 0;
  applyZoomNow();
}

function actualSizeZoom() {
  zoomState.scale = 1;
  zoomState.x = 0;
  zoomState.y = 0;
  applyZoomNow();
}

function setViewerSource(label, mode) {
  viewerSource.textContent = label;
  viewerSource.className = `sourceBadge ${mode}`;
}

function isPointOutsideDialog(dialog, event) {
  const rect = dialog.getBoundingClientRect();
  return event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom;
}

function formatExposureInfo(metadata = {}) {
  const parts = [
    metadata.iso,
    metadata.aperture,
    metadata.shutter,
    metadata.focalLength,
    metadata.exposureCompensation,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : "EXIF n/a";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function renderBrandBadges(metadata = {}) {
  return (metadata.brandBadges || []).map((badge) => `<span class="badge brandBadge">${escapeHtml(badge)}</span>`).join("");
}

function syncViewerBrandBadges(metadata = {}) {
  viewerBrandBadges.innerHTML = renderBrandBadges(metadata);
}

async function loadExposureInfo(photo) {
  viewerExposure.textContent = "Loading EXIF...";
  try {
    const metadata = await api(`/api/photos/${photo.id}/metadata`);
    if (!state.selected || state.selected.id !== photo.id) return;
    photo.metadata = metadata;
    syncViewerBrandBadges(metadata);
    replacePhotoCard(photo);
    viewerExposure.textContent = formatExposureInfo(metadata);
  } catch {
    if (!state.selected || state.selected.id !== photo.id) return;
    viewerExposure.textContent = "EXIF n/a";
  }
}

function loadViewerImage(photo) {
  zoomState.loadToken += 1;
  viewerImg.dataset.loadToken = String(zoomState.loadToken);
  actualSizeZoom();
  viewerImg.style.opacity = "0.001";
  viewerImg.decoding = "async";
  viewerImg.removeAttribute("src");
  viewerImg.dataset.fallbackUrl = photo.fullUrl;
  viewerImg.dataset.usingFallback = "false";
  setViewerSource("Loading original", "original");
  requestAnimationFrame(() => {
    viewerImg.src = photo.originalUrl;
  });
}

function renderCard(photo, index) {
  const card = document.createElement("article");
  card.className = `card ${photo.status}`;
  card.dataset.id = photo.id;
  card.dataset.index = String(index);
  card.innerHTML = `
    <img class="thumb" src="${photo.thumbUrl}" alt="${photo.filename}" loading="lazy">
    <div class="info">
      <div class="filename" title="${photo.path}">${photo.filename}</div>
      <div class="meta">
        <span class="status">${photo.status}</span>
        <span class="badge">Focus ${formatScore(focusScore(photo))}</span>
        ${photo.rawPath ? `<span class="badge">RAW</span>` : ""}
      </div>
      <div class="badges">
        ${renderBrandBadges(photo.metadata)}
        ${photo.warnings.map((warning) => `<span class="badge">${formatWarning(warning)}</span>`).join("")}
      </div>
      <div class="mark">
        <button data-status="keep">Keep</button>
        <button data-status="review">Review</button>
        <button data-status="reject">Reject</button>
      </div>
    </div>
  `;
  card.querySelector(".thumb").addEventListener("click", () => openViewer(photo, index));
  return card;
}

function formatBytes(bytes) {
  if (!bytes && bytes !== 0) return "n/a";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function renderOrphanRaw(raw) {
  const card = document.createElement("article");
  card.className = "card raw-card";
  card.dataset.id = raw.id;
  card.innerHTML = `
    <div class="rawIcon">RAW</div>
    <div class="info">
      <div class="filename" title="${raw.path}">${raw.filename}</div>
      <div class="meta">
        <span class="status">orphan raw</span>
        <span class="badge">${formatBytes(raw.sizeBytes)}</span>
      </div>
      <div class="badges">
        <span class="badge">${raw.createdAt || "date n/a"}</span>
      </div>
    </div>
  `;
  return card;
}

function render(append = false) {
  if (!append) grid.innerHTML = "";
  const fragment = document.createDocumentFragment();
  const items = state.view === "orphan_raws" ? state.orphanRaws : state.photos;
  items.forEach((item, index) => fragment.appendChild(state.view === "orphan_raws" ? renderOrphanRaw(item) : renderCard(item, index)));
  if (append) {
    grid.appendChild(fragment);
  } else {
    grid.replaceChildren(fragment);
  }
}

async function loadPhotos({ reset = false } = {}) {
  if (reset) {
    state.offset = 0;
    state.photos = [];
    state.photoCount = 0;
    state.orphanRaws = [];
    state.selectedIndex = -1;
  }
  if (state.view === "orphan_raws") {
    await loadOrphanRaws();
    return;
  }
  const params = new URLSearchParams({
    limit: state.limit,
    offset: state.offset,
  });
  if (state.status) params.set("status", state.status);
  if (state.warning) params.set("warning", state.warning);
  if (state.search) params.set("search", state.search);
  const payload = await api(`/api/photos?${params}`);
  state.photos = reset ? payload.photos : payload.photos;
  state.photoCount = payload.count;
  summary.textContent = `${payload.count} photos · keep ${payload.stats.keep || 0} · review ${payload.stats.review || 0} · reject ${payload.stats.reject || 0}`;
  syncLoadMoreVisibility(payload.count, payload.photos.length > 0);
  render(false);
}

async function loadOrphanRaws({ append = false } = {}) {
  const params = new URLSearchParams({
    limit: state.limit,
    offset: state.offset,
  });
  if (state.search) params.set("search", state.search);
  const payload = await api(`/api/orphan-raws?${params}`);
  state.orphanRaws = append ? state.orphanRaws.concat(payload.orphanRaws) : payload.orphanRaws;
  summary.textContent = `${payload.count} orphan RAW files`;
  syncLoadMoreVisibility(payload.count, payload.orphanRaws.length > 0);
  render(false);
}

async function mark(id, status) {
  return api(`/api/photos/${id}/mark`, {
    method: "POST",
    body: JSON.stringify({ status }),
  });
}

function syncViewerStatus(photo) {
  viewerMeta.textContent = `Focus ${formatScore(focusScore(photo))} · ${photo.rawPath ? "RAW paired" : "no RAW pair"}`;
  document.querySelectorAll("[data-mark]").forEach((button) => {
    const isSelected = button.dataset.mark === photo.status;
    button.classList.toggle("selected", isSelected);
    button.setAttribute("aria-pressed", String(isSelected));
  });
}

function replacePhotoCard(photo) {
  const card = grid.querySelector(`.card[data-id="${photo.id}"]`);
  if (!card) return;
  const index = state.photos.findIndex((item) => item.id === photo.id);
  if (index < 0) return;
  card.replaceWith(renderCard(photo, index));
}

function updatePhotoInState(photo) {
  const index = state.photos.findIndex((item) => item.id === photo.id);
  if (index >= 0) {
    Object.assign(state.photos[index], photo);
    state.selected = state.photos[index];
    replacePhotoCard(state.photos[index]);
    return state.photos[index];
  }
  state.selected = photo;
  return photo;
}

async function markSelectedPhoto(status) {
  if (!state.selected) return;
  const updated = await mark(state.selected.id, status);
  const photo = updatePhotoInState(updated);
  syncViewerStatus(photo);
}

function updateViewerNavButtons() {
  // Navigation is keyboard-only for now; keep this hook for future UI state.
}

function showViewerPhoto(photo, index) {
  state.selected = photo;
  state.selectedIndex = index;
  viewerName.textContent = photo.filename;
  viewerPosition.textContent = `${index + 1}/${state.photoCount || state.photos.length}`;
  syncViewerStatus(photo);
  syncViewerBrandBadges(photo.metadata);
  viewerExposure.textContent = formatExposureInfo(photo.metadata);
  updateViewerNavButtons();
  loadViewerImage(photo);
  loadExposureInfo(photo);
}

function openViewer(photo, index) {
  const resolvedIndex = Number.isInteger(index) ? index : state.photos.findIndex((item) => item.id === photo.id);
  viewer.showModal();
  showViewerPhoto(photo, resolvedIndex);
}

async function fetchNextPhotoPage() {
  if (state.view !== "photos" || state.photos.length >= state.photoCount) return false;
  const params = new URLSearchParams({ limit: state.limit, offset: state.photos.length });
  if (state.status) params.set("status", state.status);
  if (state.warning) params.set("warning", state.warning);
  if (state.search) params.set("search", state.search);
  const payload = await api(`/api/photos?${params}`);
  state.photos = state.photos.concat(payload.photos);
  state.photoCount = payload.count;
  syncLoadMoreVisibility(payload.count, payload.photos.length > 0);
  render(false);
  return payload.photos.length > 0;
}

async function navigateViewer(delta) {
  if (!viewer.open || state.view !== "photos" || !state.selected) return;
  const currentIndex = state.photos.findIndex((item) => item.id === state.selected.id);
  if (currentIndex >= 0) state.selectedIndex = currentIndex;
  if (state.selectedIndex < 0) return;
  let nextIndex = state.selectedIndex + delta;
  if (nextIndex < 0) return;
  if (nextIndex >= state.photos.length) {
    const loaded = await fetchNextPhotoPage();
    if (!loaded) return;
  }
  if (nextIndex >= state.photos.length || nextIndex >= state.photoCount) return;
  showViewerPhoto(state.photos[nextIndex], nextIndex);
  updateViewerNavButtons();
}

async function pollScan() {
  scanBtn.disabled = true;
  scanControlBtn.hidden = false;
  while (true) {
    const job = await api("/api/scan-status");
    const total = job.total || 0;
    const done = job.done || 0;
    const percent = total ? Math.round((done / total) * 100) : 0;
    scanControlBtn.textContent = job.paused ? "Continue" : "Stop";
    summary.textContent = job.running
      ? `${job.paused ? "Paused" : job.message} · ${done} / ${total} · ${percent}%`
      : job.error
        ? `Scan failed: ${job.error}`
        : job.result
          ? `Scanned ${job.result.scanned} photos in ${job.result.seconds}s · paired ${job.result.paired} · focus risk ${job.result.focusRisk || 0} · orphan RAW ${job.result.orphanRaws || 0}`
          : job.message;
    if (!job.running) break;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  scanBtn.disabled = false;
  scanControlBtn.hidden = true;
  await loadPhotos({ reset: true });
}

scanBtn.addEventListener("click", async () => {
  summary.textContent = "Starting scan...";
  await api("/api/scan", { method: "POST", body: JSON.stringify({ library: state.library }) });
  await pollScan();
});

scanControlBtn.addEventListener("click", async () => {
  const paused = scanControlBtn.textContent !== "Continue";
  await api("/api/scan-control", { method: "POST", body: JSON.stringify({ paused }) });
  scanControlBtn.textContent = paused ? "Continue" : "Stop";
});

chooseFolderBtn.addEventListener("click", async () => {
  await loadFolder(state.library);
  folderDialog.showModal();
});

folderUpBtn.addEventListener("click", async () => {
  if (!state.browseParent) return;
  await loadFolder(state.browseParent);
});

useFolderBtn.addEventListener("click", async () => {
  if (!state.browsePath) return;
  setLibrary(state.browsePath);
  folderDialog.close();
  summary.textContent = "Starting scan...";
  await api("/api/scan", { method: "POST", body: JSON.stringify({ library: state.library }) });
  await pollScan();
});

document.querySelector("#moveBtn").addEventListener("click", async () => {
  const payload = await api("/api/photos?status=reject&limit=1");
  const count = payload.count || 0;
  const ok = confirm(`Move ${count} rejected photo${count === 1 ? "" : "s"} and paired RAW files into _PHOTO_CULLER_REJECTED?`);
  if (!ok) return;
  const result = await api("/api/move-rejected", { method: "POST", body: "{}" });
  alert(`Moved ${result.count} rejected photos.`);
  await loadPhotos({ reset: true });
});

document.querySelector("#moveOrphanBtn").addEventListener("click", async () => {
  const payload = await api("/api/orphan-raws?limit=1");
  const count = payload.count || 0;
  const ok = confirm(`Move ${count} orphan RAW file${count === 1 ? "" : "s"} into _PHOTO_CULLER_ORPHAN_RAW?`);
  if (!ok) return;
  const result = await api("/api/move-orphan-raws", { method: "POST", body: "{}" });
  alert(`Moved ${result.count} orphan RAW files.`);
  await loadPhotos({ reset: true });
});

document.querySelectorAll(".filters button").forEach((button) => {
  button.addEventListener("click", async () => {
    document.querySelectorAll(".filters button").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    state.view = button.dataset.view || "photos";
    state.status = button.dataset.status || "";
    state.warning = button.dataset.warning || "";
    await loadPhotos({ reset: true });
  });
});

document.querySelector("#search").addEventListener("input", async (event) => {
  state.search = event.target.value;
  await loadPhotos({ reset: true });
});

loadMore.addEventListener("click", async () => {
  if (state.view === "orphan_raws") {
    state.offset = state.orphanRaws.length;
    await loadOrphanRaws({ append: true });
    return;
  }
  state.offset = state.photos.length;
  const params = new URLSearchParams({ limit: state.limit, offset: state.offset });
  if (state.status) params.set("status", state.status);
  if (state.warning) params.set("warning", state.warning);
  if (state.search) params.set("search", state.search);
  const payload = await api(`/api/photos?${params}`);
  state.photos = state.photos.concat(payload.photos);
  state.photoCount = payload.count;
  render(false);
  syncLoadMoreVisibility(payload.count, payload.photos.length > 0);
});

grid.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-status]");
  if (!button) return;
  const card = button.closest(".card");
  if (!card) return;
  const photo = state.photos.find((item) => item.id === card.dataset.id);
  if (!photo) return;
  const updated = await mark(photo.id, button.dataset.status);
  Object.assign(photo, updated);
  replacePhotoCard(photo);
  if (state.selected?.id === photo.id) {
    state.selected = photo;
    syncViewerStatus(photo);
  }
});

document.querySelectorAll("[data-mark]").forEach((button) => {
  button.addEventListener("click", async () => {
    await markSelectedPhoto(button.dataset.mark);
  });
});

document.querySelector("#zoomOut").addEventListener("click", () => setZoom(zoomState.scale / 1.25));
document.querySelector("#zoomFit").addEventListener("click", fitZoom);
document.querySelector("#zoomIn").addEventListener("click", () => setZoom(zoomState.scale * 1.25));

const viewerDismissPointer = {
  started: false,
  target: null,
  x: 0,
  y: 0,
};

viewer.addEventListener("pointerdown", (event) => {
  const target = event.target === viewer && isPointOutsideDialog(viewer, event) ? "backdrop" : null;
  viewerDismissPointer.started = Boolean(target);
  viewerDismissPointer.target = target;
  viewerDismissPointer.x = event.clientX;
  viewerDismissPointer.y = event.clientY;
});

viewer.addEventListener("pointerup", (event) => {
  if (!viewerDismissPointer.started) return;
  const target = viewerDismissPointer.target;
  viewerDismissPointer.started = false;
  viewerDismissPointer.target = null;
  if (target === "backdrop" && event.target === viewer && isPointOutsideDialog(viewer, event)) {
    viewer.close();
  }
});

viewer.addEventListener("pointercancel", () => {
  viewerDismissPointer.started = false;
  viewerDismissPointer.target = null;
});

viewerImg.addEventListener("load", async () => {
  const token = viewerImg.dataset.loadToken;
  try {
    if (viewerImg.decode) {
      await viewerImg.decode();
    }
  } catch {
    // Some Safari HEIF paths reject decode() even when the image is displayable.
  }
  if (token !== viewerImg.dataset.loadToken) return;
  requestAnimationFrame(() => {
    fitZoom();
    requestAnimationFrame(() => {
      viewerImg.style.opacity = "1";
    });
  });
  if (viewerImg.dataset.usingFallback === "true") {
    setViewerSource("JPEG Preview", "preview");
    return;
  }
  const filename = state.selected?.filename || "";
  const ext = filename.split(".").pop()?.toUpperCase() || "Original";
  setViewerSource(`Original ${ext}`, "original");
});

viewerImg.addEventListener("error", () => {
  if (viewerImg.dataset.usingFallback === "true") {
    setViewerSource("Preview failed", "preview");
    return;
  }
  viewerImg.dataset.usingFallback = "true";
  viewerImg.dataset.loadToken = String(++zoomState.loadToken);
  setViewerSource("JPEG Preview", "preview");
  viewerImg.src = viewerImg.dataset.fallbackUrl;
});

imageStage.addEventListener(
  "wheel",
  (event) => {
    event.preventDefault();
    const rect = imageStage.getBoundingClientRect();
    const shouldZoom = event.ctrlKey || event.metaKey;
    if (shouldZoom) {
      resetWheelZoomGesture();
      zoomState.wheelZoomEventCount += 1;
      if (zoomState.wheelZoomEventCount === 1) return;
      const originX = event.clientX - rect.left - rect.width / 2;
      const originY = event.clientY - rect.top - rect.height / 2;
      const clampedDelta = Math.max(Math.min(event.deltaY, 60), -60);
      const zoomFactor = Math.exp(-clampedDelta * 0.002);
      setZoom(zoomState.scale * zoomFactor, originX, originY);
      return;
    }
    panBy(-event.deltaX, -event.deltaY);
  },
  { passive: false },
);

imageStage.addEventListener(
  "gesturestart",
  (event) => {
    event.preventDefault();
    zoomState.gesturing = true;
    zoomState.gestureStartScale = zoomState.scale;
    zoomState.gestureLastScale = 0;
  },
  { passive: false },
);

imageStage.addEventListener(
  "gesturechange",
  (event) => {
    event.preventDefault();
    const currentGestureScale = event.scale || 1;
    if (!zoomState.gestureLastScale) {
      zoomState.gestureLastScale = currentGestureScale;
      return;
    }
    const rect = imageStage.getBoundingClientRect();
    const originX = event.clientX - rect.left - rect.width / 2;
    const originY = event.clientY - rect.top - rect.height / 2;
    const incrementalScale = currentGestureScale / zoomState.gestureLastScale;
    zoomState.gestureLastScale = currentGestureScale;
    const clampedIncrement = Math.min(Math.max(incrementalScale, 0.92), 1.087);
    setZoom(zoomState.scale * clampedIncrement, originX, originY);
  },
  { passive: false },
);

imageStage.addEventListener(
  "gestureend",
  (event) => {
    event.preventDefault();
    zoomState.gesturing = false;
    zoomState.gestureLastScale = 0;
  },
  { passive: false },
);

imageStage.addEventListener("pointerdown", (event) => {
  if (zoomState.gesturing) return;
  zoomState.dragging = true;
  zoomState.startX = event.clientX;
  zoomState.startY = event.clientY;
  zoomState.baseX = zoomState.x;
  zoomState.baseY = zoomState.y;
  imageStage.classList.add("dragging");
  imageStage.setPointerCapture(event.pointerId);
});

imageStage.addEventListener("pointermove", (event) => {
  if (!zoomState.dragging) return;
  zoomState.x = zoomState.baseX + event.clientX - zoomState.startX;
  zoomState.y = zoomState.baseY + event.clientY - zoomState.startY;
  applyZoom();
});

imageStage.addEventListener("pointerup", (event) => {
  zoomState.dragging = false;
  imageStage.classList.remove("dragging");
  imageStage.releasePointerCapture(event.pointerId);
});

document.addEventListener("keydown", async (event) => {
  if (!state.selected || !viewer.open) return;
  if (event.key === "ArrowLeft") {
    event.preventDefault();
    await navigateViewer(-1);
    return;
  }
  if (event.key === "ArrowRight") {
    event.preventDefault();
    await navigateViewer(1);
    return;
  }
  const keyMap = { "1": "keep", "2": "review", "3": "reject" };
  if (!keyMap[event.key]) return;
  event.preventDefault();
  await markSelectedPhoto(keyMap[event.key]);
});

async function init() {
  const config = await api("/api/config");
  state.library = config.library;
  state.librarySelected = Boolean(config.librarySelected);
  document.querySelector("#libraryPath").textContent = config.library;
  if (!state.librarySelected) {
    showChooseFolderPrompt();
    return;
  }
  const job = await api("/api/scan-status");
  if (job.running) {
    await pollScan();
    return;
  }
  await loadPhotos({ reset: true });
}

init().catch(() => {
  summary.textContent = "Click Scan to build the first catalog.";
});
