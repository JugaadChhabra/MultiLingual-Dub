/* Video pane — the monitor: the avatar going in, the render coming out, and
   the picker for reusing an avatar already on HeyGen. */
(() => {
  "use strict";
  const U = window.UI, $ = U.$, V = window.Video;
  const { els, state } = V;

  // ── monitor ──────────────────────────────────────────────────────────────
  // mtag names what the monitor is SHOWING; the run message names what the job
  // is doing. Never tinted or animated — a render has to be judged honestly.
  function showPlaceholder() {
    els.monitor.dataset.empty = "1";
    els.body.innerHTML = `<div class="ph"><div class="t">No avatar yet</div>
      <div class="s">Drag an image here, or pick one</div>
      <div class="row"><button type="button" class="btn" id="vBrowse">Upload image</button>
      <button type="button" class="btn" id="vReuse">Reuse an avatar</button></div></div>`;
    $("#vBrowse").addEventListener("click", (e) => { e.stopPropagation(); if (!state.running) els.img.click(); });
    $("#vReuse").addEventListener("click", (e) => { e.stopPropagation(); if (!state.running) openAvatars(); });
    els.tag.textContent = "No input"; els.file.hidden = true;
    delete els.body.dataset.src;
  }
  function showStill(tag) {
    els.monitor.dataset.empty = state.stillURL ? "0" : "1";
    els.body.innerHTML = state.stillURL ? `<img src="${state.stillURL}" alt="Selected reference avatar" />` : "";
    els.tag.textContent = tag || "Reference";
    els.file.hidden = !(state.image || state.photoId);
    delete els.body.dataset.src;
  }
  // Clips this machine has already failed to read. The dataset guard below only
  // holds while the clip is mounted, and giving up unmounts it — so without
  // this, a genuinely broken file is retried on every poll tick forever: mount,
  // fail, fall back to the still, remount four seconds later.
  const unplayable = new Set();
  function showVideo(url) {
    // The batch poller reports every completed row every four seconds, so this
    // is called repeatedly with a clip that is already on screen. Rebuilding
    // the element each time restarted playback under the operator and armed a
    // fresh unplayable-clip timer, which is what was stacking notices.
    if (unplayable.has(url) || els.body.dataset.src === url) return;
    els.body.dataset.src = url;
    els.monitor.dataset.empty = "0";
    els.body.innerHTML = `<video src="${url}" controls autoplay playsinline></video>`;
    els.tag.textContent = "Playback"; els.file.hidden = true;
    const v = els.body.querySelector("video");
    if (!v) return;
    // A truncated download does not reliably fire `error` — Chrome sits at
    // networkState=LOADING forever — so give up on a metadata timeout too.
    let settled = false;
    const giveUp = (why) => { if (settled) return; settled = true; clearTimeout(t);
      unplayable.add(url);
      showStill("Clip unavailable");
      U.toast("The clip could not be played", { kind: "warn", id: "clip:" + url,
        detail: `It rendered, but this machine could not read the file (${U.esc(why)}).` }); };
    const t = setTimeout(() => giveUp("no metadata after 12s"), 12000);
    v.addEventListener("loadedmetadata", () => { settled = true; clearTimeout(t); }, { once: true });
    v.addEventListener("error", () => giveUp("media error"), { once: true });
  }

  els.monitor.addEventListener("click", (e) => {
    if (state.running || e.target.closest(".mfile") || e.target.closest("video")) return;
    els.img.click();
  });
  ["dragover", "dragenter"].forEach((e) => els.monitor.addEventListener(e, (ev) => {
    ev.preventDefault(); if (!state.running) els.monitor.classList.add("over"); }));
  ["dragleave", "drop"].forEach((e) => els.monitor.addEventListener(e, (ev) => {
    ev.preventDefault(); els.monitor.classList.remove("over"); }));
  els.monitor.addEventListener("drop", (ev) => {
    if (state.running) return; const f = ev.dataTransfer.files[0]; if (f) onImage(f); });
  els.img.addEventListener("change", () => { if (els.img.files[0]) onImage(els.img.files[0]); });
  $("#vReplace").addEventListener("click", (e) => { e.stopPropagation(); if (!state.running) els.img.click(); });
  $("#vRemove").addEventListener("click", (e) => { e.stopPropagation(); if (!state.running) clearImage(); });

  function onImage(file) {
    if (!/^image\//.test(file.type)) return;
    state.image = file; state.photoId = null;
    if (state.stillURL) URL.revokeObjectURL(state.stillURL);
    state.stillURL = URL.createObjectURL(file);
    els.fileName.textContent = file.name; els.fileSize.textContent = U.kb(file.size);
    if (!state.running) showStill("Reference");
    V.refreshRun();
  }
  function clearImage() {
    state.image = null; state.photoId = null;
    if (state.stillURL) { URL.revokeObjectURL(state.stillURL); state.stillURL = null; }
    els.img.value = "";
    if (!state.running) showPlaceholder();
    V.refreshRun();
  }

  // ── avatar picker ────────────────────────────────────────────────────────
  const avOv = U.overlay($("#avDrawer"), $("#avScrim"), { fallbackFocus: () => $("#vBrowse") });
  $("#avClose").addEventListener("click", avOv.close);
  async function openAvatars() {
    avOv.open();
    const body = $("#avBody");
    body.innerHTML = `<div class="empty"><div class="s">Loading avatars…</div></div>`;
    try {
      const r = await U.fetchT("/video/heygen/talking-photos", {}, 25000);
      if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`);
      const items = (await r.json()).items || [];
      if (!items.length) {
        body.innerHTML = `<div class="empty"><div class="t">No avatars yet</div>
          <div class="s">Upload an image once and it appears here.</div></div>`; return;
      }
      const grid = document.createElement("div"); grid.className = "avgrid";
      items.forEach((it) => {
        // HeyGen returns talking_photo_id / name / preview_url
        const id = it.talking_photo_id || it.id || "";
        const name = it.name || id.slice(0, 8);
        const url = it.preview_url || it.preview_image_url || "";
        const card = document.createElement("button");
        card.type = "button"; card.className = "avcard";
        card.setAttribute("aria-label", `Use avatar ${name}`);
        card.innerHTML = (url ? `<img src="${U.esc(url)}" alt="" />` : `<span class="noimg">No preview</span>`)
          + `<span class="nm">${U.esc(name)}</span>`;
        card.addEventListener("click", () => {
          state.photoId = id; state.image = null; els.img.value = "";
          if (state.stillURL) URL.revokeObjectURL(state.stillURL);
          state.stillURL = url || null;
          els.fileName.textContent = name; els.fileSize.textContent = "Reused";
          if (!state.running) showStill("Reference");
          avOv.close(); V.refreshRun();
        });
        grid.appendChild(card);
      });
      body.innerHTML = ""; body.appendChild(grid);
    } catch (e) {
      body.innerHTML = `<div class="empty"><div class="t">Could not load avatars</div>
        <div class="s">${U.esc(e.message)}</div></div>`;
    }
  }

  Object.assign(V, { showPlaceholder, showStill, showVideo });
})();
