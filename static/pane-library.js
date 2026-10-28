/* Library pane — every render on the HeyGen account.
   Each one says where it stands (on the NAS, not on it, can't be recovered and
   why) and offers what can be done: download it, or send it to the NAS. The
   server decides all of that (services/video_pipeline/library.py); this only
   presents it, and watches what it sent until each one lands or fails.

   Cheap by construction: thumbnails load lazily (only rows on screen ask) and
   the server marks them immutable, so each is fetched from HeyGen once ever and
   by a browser once; a poll patches only the rows whose content changed, so it
   never rebuilds the list or reloads its images. */
(() => {
  "use strict";
  const U = window.UI, $ = U.$;

  const PAGE = 50, POLL_MS = 4000, PENDING_KEY = "autodub_library_pending";
  const BADGE = {
    on_nas: ["g", "On NAS"],
    in_progress: ["a", "Working"],
    sendable: ["o", "Not on NAS"],
    needs_details: ["o", "Not on NAS"],
    rendering: ["a", "Rendering"],
    not_recoverable: ["r", "Can't recover"],
  };
  const CAN_DOWNLOAD = new Set(["on_nas", "sendable", "needs_details", "in_progress"]);
  // States whose render finished on HeyGen, so it has a thumbnail to show.
  const HAS_THUMB = new Set(["on_nas", "sendable", "needs_details"]);

  const els = {
    list: $("#libList"), filter: $("#libFilter"), search: $("#libSearch"),
    refresh: $("#libRefresh"), sendAll: $("#libSendAll"), more: $("#libMore"),
    msg: $("#libMsg"), banners: $("#libraryBanners"), live: $("#libraryLive"),
  };
  let filter = "", query = "", items = [], total = 0, counts = {}, loaded = false, loadSeq = 0;

  // ── what was sent, watched until it settles ─────────────────────────────
  // Kept in localStorage so a reload mid-send still reports how it ended.
  const pending = new Map();
  try { for (const [k, v] of JSON.parse(localStorage.getItem(PENDING_KEY)) || []) pending.set(k, v); } catch (_) {}
  const savePending = () => { try { localStorage.setItem(PENDING_KEY, JSON.stringify([...pending])); } catch (_) {} };
  let pollTimer = null;

  function watch(videoId, title) {
    if (!videoId) return;
    pending.set(videoId, title || videoId);
    savePending();
    schedulePoll();
  }

  function schedulePoll() {
    const on = pending.size > 0;
    els.live.hidden = !on;
    window.Shell && window.Shell.setRunning("library", on, on ? `Sending ${pending.size} to NAS` : undefined);
    if (on && !pollTimer) pollTimer = setTimeout(poll, POLL_MS);
  }

  async function poll() {
    pollTimer = null;
    const ids = [...pending.keys()];
    try {
      const r = await U.fetchT(`/video/heygen/library?limit=200&ids=${encodeURIComponent(ids.join(","))}`, {}, 20000);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const byId = new Map((await r.json()).items.map((e) => [e.video_id, e]));
      let settled = 0;
      for (const id of ids) {
        const e = byId.get(id);
        if (e && e.state === "in_progress") continue;
        const name = pending.get(id);
        pending.delete(id); settled++;
        if (e && e.state === "on_nas") {
          U.toast(`Saved to NAS: ${name}`, { kind: "ok", detail: e.nas_path || "" });
        } else {
          U.toast(`Could not send ${name} to the NAS`, { kind: "error",
            detail: (e && e.reason) || "It is no longer in the library." });
        }
      }
      savePending();
      if (settled) { load(false); window.Shell && window.Shell.refreshRecoverable(); }
      else if (loaded && !$("#pane-library").hidden) load(false, { quiet: true });
    } catch (_) { /* try again next tick */ }
    schedulePoll();
  }

  // ── loading ──────────────────────────────────────────────────────────────
  async function load(append, { refresh = false, quiet = false } = {}) {
    const seq = ++loadSeq;
    const offset = append ? items.length : 0;
    const shown = append ? items.length + PAGE : Math.max(PAGE, items.length);
    const params = new URLSearchParams({ offset, limit: append ? PAGE : shown });
    if (filter) params.set("state", filter);
    if (query) params.set("q", query);
    if (refresh) params.set("refresh", "true");
    if (!quiet) els.msg.textContent = loaded ? "Updating…" : "Listing renders on HeyGen — the first load takes a few seconds…";
    els.refresh.disabled = true;
    try {
      const r = await U.fetchT(`/video/heygen/library?${params}`, {}, 90000);
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
      const d = await r.json();
      if (seq !== loadSeq) return;  // a newer filter or search won
      items = append ? items.concat(d.items) : d.items;
      total = d.total; counts = d.counts || {};
      loaded = true;
      U.clearBanners(els.banners);
      if (d.listing_error) U.banner(els.banners, { kind: "warn", id: "lib-listing",
        title: "HeyGen's list is unavailable",
        detail: "Showing only the videos this app made. Renders made elsewhere, and whether HeyGen still has them, can't be checked until it is back.",
        raw: d.listing_error });
      render();
      els.msg.textContent = total ? `Showing ${items.length} of ${total}` : "";
    } catch (e) {
      if (seq !== loadSeq) return;
      els.msg.textContent = "";
      U.banner(els.banners, { kind: "error", id: "lib-load", title: "Could not load the library",
        raw: String(e && e.message || e) });
    } finally {
      if (seq === loadSeq) els.refresh.disabled = false;
    }
  }

  // ── rendering ────────────────────────────────────────────────────────────
  const when = (iso) => {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleString("en-GB", { day: "numeric", month: "short", year: "numeric",
        hour: "2-digit", minute: "2-digit" });
    } catch (_) { return ""; }
  };

  function render() {
    els.filter.querySelectorAll(".n").forEach((n) => {
      const keys = n.dataset.n ? n.dataset.n.split(",") : Object.keys(counts);
      const c = keys.reduce((a, k) => a + (counts[k] || 0), 0);
      n.textContent = c ? String(c) : "";
    });
    const sendable = counts.sendable || 0;
    els.sendAll.hidden = sendable === 0;
    els.sendAll.textContent = `Send ${sendable} to NAS`;
    els.more.hidden = items.length >= total;

    if (!items.length) {
      els.list.innerHTML = `<div class="empty"><div class="g" aria-hidden="true">▦</div>
        <div class="t">${query || filter ? "Nothing matches" : "No renders yet"}</div>
        <div class="s">${query || filter ? "Try another filter or search." : "Videos rendered on HeyGen appear here."}</div></div>`;
      return;
    }
    patch(items);
  }

  // Keyed by video id: a row whose markup is unchanged keeps its node (and its
  // loaded thumbnail, and an open details form); only changed rows are redrawn.
  function patch(list) {
    const old = new Map();
    els.list.querySelectorAll(":scope > .row.lib").forEach((n) => old.set(n.dataset.key, n));
    els.list.querySelectorAll(":scope > .empty").forEach((n) => n.remove());
    const formOf = (n) => {
      const f = n.nextElementSibling;
      return f && f.classList.contains("libform") ? f : null;
    };
    let at = els.list.firstElementChild;
    for (const e of list) {
      const key = e.video_id || e.job_id;
      const html = row(e);
      let node = old.get(key), form = null;
      if (node) { old.delete(key); form = formOf(node); }
      if (!node || node.dataset.html !== html) {
        const t = document.createElement("template");
        t.innerHTML = html.trim();
        const fresh = t.content.firstElementChild;
        fresh.dataset.html = html;
        if (node) node.replaceWith(fresh);
        node = fresh;
      }
      if (node !== at) els.list.insertBefore(node, at);
      if (form) node.after(form);  // a details form travels with its row
      at = (form || node).nextElementSibling;
    }
    old.forEach((n) => { const f = formOf(n); if (f) f.remove(); n.remove(); });
  }

  function row(e) {
    const [tone, label] = BADGE[e.state] || ["", e.state];
    const title = e.title
      ? `<span class="t">${U.esc(e.title)}</span>`
      : `<span class="t untitled">Untitled render</span>`;
    const facts = [
      when(e.created_at),
      e.publish_date && `for ${U.esc(e.publish_date)}`,
      e.character && U.esc(e.character),
      e.nas_path && `<span class="path" title="${U.esc(e.nas_path)}">${U.esc(e.nas_path)}</span>`,
      !e.title && e.video_id && `<span class="path">${U.esc(e.video_id)}</span>`,
    ].filter(Boolean).join(" · ");
    let why = "";
    if (e.state === "not_recoverable") why = `<span class="s why bad">${U.esc(e.reason)}</span>`;
    else if (e.state === "sendable" && e.reason) why = `<span class="s why">Last attempt failed: ${U.esc(e.reason)}</span>`;
    else if (e.state === "needs_details") why = `<span class="s why">No title or date on file — add them to send it to the NAS.</span>`;
    else if (e.state === "in_progress") why = `<span class="s why">${U.esc(e.stage || "Working")}</span>`;
    else if (e.state === "rendering") why = `<span class="s why">Still rendering on HeyGen.</span>`;

    const vid = U.esc(e.video_id || "");
    const acts = [];
    if (e.video_id && CAN_DOWNLOAD.has(e.state)) acts.push(`<button class="btn q" data-act="download">Download</button>`);
    if (e.state === "sendable") acts.push(`<button class="btn" data-act="send">Send to NAS</button>`);
    if (e.state === "needs_details") acts.push(`<button class="btn" data-act="details">Send to NAS…</button>`);

    const thumb = e.video_id && HAS_THUMB.has(e.state)
      ? `<img class="thumb" src="/video/heygen/library/${encodeURIComponent(e.video_id)}/thumbnail"
           loading="lazy" decoding="async" width="54" height="96" alt="" />`
      : `<span class="thumb none" aria-hidden="true"></span>`;
    return `<div class="row lib" data-vid="${vid}" data-key="${U.esc(e.video_id || e.job_id || "")}">
      ${thumb}
      <span class="tx">${title}<span class="s">${facts}</span>${why}</span>
      <span class="rt"><span class="badge ${tone}">${label}</span>${acts.join("")}</span>
    </div>`;
  }

  function detailsForm(rowEl) {
    const open = rowEl.nextElementSibling;
    if (open && open.classList.contains("libform")) { open.remove(); return; }
    const d = new Date();
    const today = [d.getDate(), d.getMonth() + 1].map((n) => String(n).padStart(2, "0")).join("-") + "-" + d.getFullYear();
    const form = document.createElement("form");
    form.className = "libform";
    form.innerHTML = `
      <label><span class="flbl">Title <span class="hint">— the NAS file name</span></span>
        <input class="fld" name="title" required autocomplete="off" /></label>
      <label><span class="flbl">Publish date</span>
        <input class="fld" name="publish_date" required placeholder="DD-MM-YYYY" pattern="\\d{2}-\\d{2}-\\d{4}" value="${today}" /></label>
      <label><span class="flbl">Character</span>
        <select class="fld" name="character"><option value="indian">Indian</option><option value="us">US</option></select></label>
      <div class="opts">
        <label><input type="checkbox" name="include_overlay" checked /> Overlay cards</label>
        <label><input type="checkbox" name="include_music" checked /> Music</label>
        <span class="grow"></span>
        <button type="button" class="btn q" data-act="cancel">Cancel</button>
        <button type="submit" class="btn p">Send to NAS</button>
      </div>
      <div class="err" role="alert"></div>`;
    rowEl.after(form);
    const f = form.elements;
    f.title.focus();
    form.addEventListener("click", (ev) => { if (ev.target.dataset.act === "cancel") form.remove(); });
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const body = {
        title: f.title.value.trim(), publish_date: f.publish_date.value.trim(),
        character: f.character.value,
        include_overlay: f.include_overlay.checked, include_music: f.include_music.checked,
      };
      const btn = form.querySelector("button[type=submit]");
      btn.disabled = true;
      const ok = await send(rowEl.dataset.vid, body.title, body, (msg) => { form.querySelector(".err").textContent = msg; });
      btn.disabled = false;
      if (ok) form.remove();
    });
  }

  // ── actions ──────────────────────────────────────────────────────────────
  async function send(videoId, title, body, onError) {
    try {
      const r = await U.fetchT(`/video/heygen/library/${encodeURIComponent(videoId)}/send-to-nas`, {
        method: "POST",
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined,
      }, 20000);
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        const detail = Array.isArray(d.detail) ? d.detail.map((x) => x.msg).join("; ") : d.detail;
        throw new Error(detail || `HTTP ${r.status}`);
      }
      watch(videoId, title);
      load(false, { quiet: true });
      return true;
    } catch (e) {
      const msg = String(e && e.message || e);
      if (onError) onError(msg);
      else U.toast(`Could not send ${title} to the NAS`, { kind: "error", detail: msg });
      return false;
    }
  }

  async function download(videoId, title, btn) {
    const label = btn.textContent;
    btn.disabled = true; btn.textContent = "Fetching…";
    try {
      // The server fetches it from HeyGen (once, then keeps it) and reports any
      // failure here; then the browser streams the file straight to disk.
      const r = await U.fetchT(`/video/heygen/library/${encodeURIComponent(videoId)}/prepare-download`,
        { method: "POST" }, 300000);
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
      const a = Object.assign(document.createElement("a"), { href: d.url, download: `${title || videoId}.mp4` });
      document.body.append(a); a.click(); a.remove();
    } catch (e) {
      U.toast("Download failed", { kind: "error", detail: String(e && e.message || e) });
    } finally {
      btn.disabled = false; btn.textContent = label;
    }
  }

  els.list.addEventListener("error", (ev) => {
    const img = ev.target;
    if (img instanceof HTMLImageElement && img.classList.contains("thumb")) {
      const ph = document.createElement("span");
      ph.className = "thumb none"; ph.setAttribute("aria-hidden", "true");
      img.replaceWith(ph);
    }
  }, true);

  els.list.addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-act]");
    const rowEl = btn && btn.closest(".row.lib");
    if (!rowEl) return;
    const e = items.find((x) => x.video_id === rowEl.dataset.vid);
    const title = (e && e.title) || rowEl.dataset.vid;
    if (btn.dataset.act === "download") download(rowEl.dataset.vid, e && e.title, btn);
    else if (btn.dataset.act === "send") { btn.disabled = true; send(rowEl.dataset.vid, title).finally(() => { btn.disabled = false; }); }
    else if (btn.dataset.act === "details") detailsForm(rowEl);
  });

  els.sendAll.addEventListener("click", () => {
    const n = counts.sendable || 0;
    U.confirm({
      title: `Send ${n} video${n === 1 ? "" : "s"} to the NAS?`,
      message: "These rendered on HeyGen but never reached the NAS. Sending re-downloads them two at a time — nothing is re-rendered, so it costs no credits. Untitled renders are not included; send those one at a time with their details.",
      confirmLabel: "Send", danger: false,
      onOk: async () => {
        try {
          const r = await U.fetchT("/video/heygen/recover-failed", { method: "POST" }, 20000);
          if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
          const d = await r.json();
          const titles = new Map(items.map((x) => [x.video_id, x.title]));
          (d.video_ids || []).forEach((id) => watch(id, titles.get(id)));
          U.toast(`Sending ${d.count} video${d.count === 1 ? "" : "s"} to the NAS`, { kind: "info",
            detail: "Each one is reported here as it lands or fails." });
          load(false, { quiet: true });
        } catch (e) {
          U.banner(els.banners, { kind: "error", id: "lib-sendall", title: "Could not start sending",
            raw: String(e && e.message || e) });
        }
      },
    });
  });

  U.bindSeg(els.filter, (data) => { filter = data.state || ""; items = []; load(false); });
  let searchTimer = null;
  els.search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { query = els.search.value.trim(); items = []; load(false); }, 250);
  });
  els.refresh.addEventListener("click", () => load(false, { refresh: true }));
  els.more.addEventListener("click", () => load(true));

  function setFilter(state) {
    els.filter.querySelectorAll("button").forEach((b) =>
      b.setAttribute("aria-pressed", String((b.dataset.state || "") === state)));
    filter = state; items = [];
    load(false);
  }

  window.PaneLibrary = {
    // Called by the shell each time the section is shown.
    activate() { if (!loaded) load(false); },
    setFilter,
  };
  // After the shell has loaded (it loads last), so the running indicator can
  // show sends a reload interrupted.
  setTimeout(schedulePoll, 0);
})();
