/* Video pane — a run: what blocks it, submitting it, pausing or cancelling it,
   polling it to the end, and picking it back up after a reload. */
(() => {
  "use strict";
  const U = window.UI, V = window.Video;
  const { els, state } = V;
  const { JOB_KEY } = V.keys;

  // ── validity ─────────────────────────────────────────────────────────────
  function blocker() {
    if (!V.cats.length) return "Create a prompt category to start";
    if (!V.activeCat()) return "Pick a category";
    if (state.mode === "auto") {
      if (!state.image && !state.photoId) return "Add an avatar to start";
      if (state.writing) return "Waiting for the scripts";
      if (!state.drafts.length) return "Write the scripts first";
      if (state.drafts.some((d) => !d.script.trim())) return "One of the drafts is empty";
      if (state.drafts.some((d) => !d.video_title.trim())) return "Every draft needs a file name";
      const titles = state.drafts.map((d) => d.video_title.trim().toLowerCase());
      if (new Set(titles).size !== titles.length) return "Two drafts share a file name — one would overwrite the other";
    } else if (state.mode === "batch") {
      if (!state.image && !state.photoId) return "Add an avatar to start";
      if (!state.excel) return "Drop a spreadsheet of scripts";
      if (state.excelProblem) return state.excelProblem;
    } else {
      if (!state.image && !state.photoId) return "Add an avatar to start";
      if (!els.script.value.trim()) return "Write the line the avatar should speak";
      if (!V.safeTitle()) return "Name the file so it does not overwrite an earlier render";
    }
    if (!window.Shell.configured()) return "Runtime config is not ready";
    return null;
  }
  function refreshRun() {
    if (state.mode === "auto") { V.refreshGen(); V.refreshRunLabel(); }
    els.ctl.hidden = !state.running;
    if (state.running) { els.run.disabled = true; refreshControls(); return; }
    const why = blocker();
    els.run.disabled = Boolean(why);
    if (!els.msg.classList.contains("err")) els.msg.textContent = why || "";
  }

  // ── pause / cancel ───────────────────────────────────────────────────────
  // Pause is for a batch only: one video has no "next row" to hold back. Both
  // act at the next step of the pipeline, never inside a render HeyGen already
  // has — that one is paid for and finishes on their side.
  const ctlBase = () => state.kind === "batch"
    ? `/video/heygen/batch/${state.jobId}` : `/video/heygen/${state.jobId}`;
  function refreshControls() {
    els.pause.hidden = state.kind !== "batch";
    els.pause.disabled = !state.jobId || state.cancelling;
    U.label(els.pause, state.paused ? "Resume" : "Pause", state.paused ? "play" : "pause");
    els.cancel.disabled = !state.jobId || state.cancelling;
    U.label(els.cancel, state.cancelling ? "Cancelling…" : "Cancel", state.cancelling ? null : "stop");
  }
  els.pause.addEventListener("click", async () => {
    const want = !state.paused;
    els.pause.disabled = true;
    try {
      const r = await U.fetchT(`${ctlBase()}/${want ? "pause" : "resume"}`, { method: "POST" });
      if (r.ok) setPaused(want);
    } catch (_) {}
    refreshControls();
  });
  els.cancel.addEventListener("click", () => {
    const batch = state.kind === "batch";
    U.confirm({
      title: batch ? "Cancel this batch?" : "Cancel this render?",
      message: batch
        ? "Videos already on the NAS stay there. Nothing else is filed — renders HeyGen has already started are still billed."
        : "It will not be filed to the NAS. If HeyGen has already started it, the render is still billed.",
      confirmLabel: batch ? "Cancel batch" : "Cancel render", cancelLabel: "Keep going",
      onOk: async () => {
        state.cancelling = true; refreshControls();
        try {
          const r = await U.fetchT(`${ctlBase()}/cancel`, { method: "POST" });
          if (!r.ok) { state.cancelling = false; refreshControls(); }
        } catch (_) { state.cancelling = false; refreshControls(); }
      },
    });
  });
  function setPaused(on) {
    if (state.paused === on) return;
    state.paused = on;
    els.tag.textContent = on ? "Paused" : "Rendering";
    window.Shell.setRunning("video", true, on ? "Video paused" : "Video rendering");
  }
  // The server's word wins: another tab may have paused or cancelled this run.
  function syncControls(p) {
    setPaused(Boolean(p.paused));
    if (p.cancel_requested) state.cancelling = true;
    refreshControls();
  }

  // ── run ──────────────────────────────────────────────────────────────────
  els.run.addEventListener("click", submit);
  async function submit() {
    const why = blocker();
    if (why) { V.say(why, true); return; }
    if (!V.dateSet()) return;   // the date prints on the card and files the render
    V.say(""); U.clearBanners(els.banners);
    U.askNotify();   // not awaited — see pane-audio.js

    const c = V.activeCat();
    state.running = true; state.kind = V.submitsRows() ? "batch" : "single";
    state.paused = false; state.cancelling = false;
    state.startedAt = new Date().toISOString();
    els.live.hidden = false; refreshRun();
    window.Shell.setRunning("video", true, "Video rendering");
    els.tag.textContent = "Rendering"; els.progress.hidden = false;

    const fd = new FormData();
    if (state.image) fd.append("image", state.image);
    else fd.append("talking_photo_id", state.photoId);
    fd.append("character", state.character);
    // The date names the NAS folder, prints on the card, and in Auto is the
    // history key. Required at submit (dateSet), so no fallback here.
    fd.append("publish_date", els.date.value);
    if (c.motion_prompt) fd.append("motion_prompt", c.motion_prompt);
    // Every mode: a one-off written in Auto is no horoscope and wants no cards.
    fd.append("include_overlay", U.isOn(els.overlay) ? "true" : "false");
    fd.append("include_music", U.isOn(els.music) ? "true" : "false");

    try {
      let url, id;
      if (V.submitsRows()) {
        // One endpoint, two ways in: a spreadsheet, or the drafts as they were
        // left on screen. Titles are scrubbed again here in case a draft came
        // straight from the writer without being touched.
        if (state.mode === "auto") {
          fd.append("rows", JSON.stringify(state.drafts.map((d) => ({
            script: d.script.trim(), video_title: V.scrubTitle(d.video_title),
          }))));
          // Which history these rows belong to. Sent so the server can replace
          // the drafts it recorded when they were written with what is actually
          // being rendered — the edits are the version viewers see.
          fd.append("category", c.id);
          fd.append("language", els.lang.value);
        } else fd.append("excel", state.excel);
        const r = await fetch("/video/heygen/batch", { method: "POST", body: fd });
        if (!r.ok) throw await refusal(r);
        id = (await r.json()).batch_id; url = `/video/heygen/batch/${id}`;
      } else {
        fd.append("script", els.script.value.trim());
        fd.append("video_title", V.safeTitle());
        const r = await fetch("/video/heygen", { method: "POST", body: fd });
        if (!r.ok) throw await refusal(r);
        id = (await r.json()).job_id; url = `/video/heygen/${id}`;
      }
      state.jobId = id; refreshControls();
      try { localStorage.setItem(JOB_KEY, JSON.stringify({ kind: state.kind, id })); } catch (_) {}
      poll(url);
    } catch (e) {
      // A refusal reached the server and was turned down (a title, a length, a
      // duplicate): say why. Anything else never got an answer at all.
      U.banner(els.banners, e && e.refused
        ? { kind: "error", title: "The server refused the render", detail: e.message, raw: e.raw }
        : { kind: "error", title: "Could not start the render",
            detail: "The request never reached the render service.", raw: String(e && e.message || e) });
      finish("failed", null);
    }
  }

  // A non-2xx answer as an Error carrying the server's own words. FastAPI puts
  // them in `detail`: a string for our 400s, a list for its 422 validation.
  async function refusal(r) {
    const raw = await r.text();
    let detail = null;
    try { detail = JSON.parse(raw).detail; } catch (_) {}
    if (Array.isArray(detail)) detail = detail.map((d) => (d && d.msg) || String(d)).join("; ");
    const e = new Error(typeof detail === "string" && detail ? detail : `HTTP ${r.status}`);
    e.refused = true; e.raw = raw;
    return e;
  }

  const ENDED = ["completed", "failed", "partial", "cancelled"];
  async function poll(url) {
    while (state.running) {
      try {
        const r = await U.fetchT(url);
        if (!r.ok) throw new Error("HTTP " + r.status);
        const p = await r.json();
        if (state.kind === "batch") V.renderBatch(p); else V.renderSingle(p);
        if (ENDED.includes(p.status)) { finish(p.status, p); return; }
        syncControls(p);
      } catch (_) {}
      await new Promise((res) => setTimeout(res, 4000));
    }
  }

  function finish(status, p) {
    state.running = false; state.jobId = null; state.paused = false; state.cancelling = false;
    try { localStorage.removeItem(JOB_KEY); } catch (_) {}
    els.live.hidden = true;
    window.Shell.setRunning("video", false);
    refreshRun();
    window.Shell.refreshRecoverable();

    const cause = p && p.cause;
    if (cause) window.Shell.showCause(els.banners, cause);

    if (status === "completed") {
      const url = p && (p.video_local_url || (p.summary && p.summary.video_url));
      if (url) V.showVideo(url); else V.showStill("Complete");
      const nas = p && p.summary && p.summary.nas_path;
      U.toast("Render finished", { kind: "ok",
        detail: nas ? `Filed to <code>${U.esc(nas)}</code>` : "Finished." });
      U.notify("Video render finished", nas ? `Filed to ${nas}` : "Finished", false);
    } else if (status === "cancelled") {
      const filed = p && state.kind === "batch"
        ? (p.rows || []).filter((r) => r.status === "completed").length : 0;
      if (!filed) V.showStill("Cancelled");
      U.toast(state.kind === "batch" ? "Batch cancelled" : "Render cancelled", { kind: "info",
        detail: filed ? `${filed} video${filed === 1 ? " was" : "s were"} filed before it stopped.`
          : "Nothing was filed to the NAS." });
    } else if (status === "partial") {
      const failed = (p.rows || []).filter((r) => r.status === "failed").length;
      U.notify(`Video batch finished with ${failed} failures`,
        cause ? cause.title : `${(p.done || 0)} of ${p.total || 0} filed to the NAS`, true);
    } else {
      V.showStill("Failed");
      if (!cause) U.banner(els.banners, { kind: "error", title: "The render did not finish",
        detail: "No cause was reported by the server." });
      U.notify("Video render failed", cause ? cause.title : "See the app for details", true);
    }
  }

  // ── survive a reload ─────────────────────────────────────────────────────
  async function reattach() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(JOB_KEY)); } catch (_) {}
    if (!saved || !saved.id) return;
    const url = saved.kind === "batch" ? `/video/heygen/batch/${saved.id}` : `/video/heygen/${saved.id}`;
    try {
      const r = await U.fetchT(url);
      if (!r.ok) { localStorage.removeItem(JOB_KEY); return; }
      const p = await r.json();
      if (ENDED.includes(p.status)) { localStorage.removeItem(JOB_KEY); return; }
      state.running = true; state.kind = saved.kind; state.jobId = saved.id;
      state.paused = false; state.cancelling = false; syncControls(p);
      state.startedAt = state.startedAt || new Date().toISOString();
      els.live.hidden = false; els.progress.hidden = false; refreshRun();
      U.toast("Reattached to a render already in progress", { kind: "info",
        detail: "Nothing was lost when the page reloaded." });
      poll(url);
    } catch (_) {}
  }

  Object.assign(V, { refreshRun, reattach });
})();
