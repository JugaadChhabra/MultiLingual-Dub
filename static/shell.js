/* Shell — sidebar, section switching, config status, stranded renders.
   Loads last: the panes register themselves on window before this runs. */
(() => {
  "use strict";
  const U = window.UI, $ = U.$;

  let envConfigured = false;
  const running = { audio: false, video: false };

  // ── sections ─────────────────────────────────────────────────────────────
  // /videogen is a real, linkable path for the Video section. The old /heygen
  // redirects to it, so existing bookmarks still land in the right place.
  function show(section, push = true) {
    document.querySelectorAll(".sbi[data-section]").forEach((b) => {
      const on = b.dataset.section === section;
      if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    });
    $("#pane-audio").hidden = section !== "audio";
    $("#pane-video").hidden = section !== "video";
    document.title = section === "video" ? "AutoDub — Video" : "AutoDub — Audio";
    if (push) history.replaceState({}, "", section === "video" ? "/videogen" : "/");
  }
  document.querySelectorAll(".sbi[data-section]").forEach((b) =>
    b.addEventListener("click", () => show(b.dataset.section)));

  // ── config status ────────────────────────────────────────────────────────
  // One function owns dot and label, so they cannot disagree — the old UI left
  // a green "session ready" on screen while the status call was failing.
  const dot = $("#cfgDot"), text = $("#cfgText");
  async function refreshEnv() {
    try {
      const r = await U.fetchT("/config/session-env/status", {}, 10000);
      if (!r.ok) throw new Error("status " + r.status);
      const p = await r.json();
      const missing = Array.isArray(p.missing_keys) ? p.missing_keys : [];
      const total = (Array.isArray(p.required_keys) ? p.required_keys : []).length;
      envConfigured = Boolean(p.configured);
      if (envConfigured) {
        dot.className = "dot g"; text.textContent = total ? `${total}/${total} configured` : "Configured";
        dot.title = "";
      } else {
        dot.className = "dot o";
        text.textContent = `${total - missing.length}/${total} configured`;
        dot.title = "Missing: " + missing.join(", ");
      }
    } catch (_) {
      envConfigured = false;
      dot.className = "dot r"; text.textContent = "Config unreachable";
      dot.title = "Could not read /config/session-env/status";
    }
    if (window.PaneAudio) window.PaneAudio.refreshRun();
    if (window.PaneVideo) window.PaneVideo.refreshRun();
  }

  // ── toolbar running indicator ────────────────────────────────────────────
  function setRunning(section, on, label) {
    running[section] = on;
    const any = running.audio || running.video;
    $("#tbRun").hidden = !any;
    $("#tbSep").hidden = !any;
    if (any && label) $("#tbRunText").textContent = label;
    else if (any) $("#tbRunText").textContent = running.audio ? "Audio running" : "Video rendering";
  }

  // ── a classified failure, rendered ───────────────────────────────────────
  // The backend classified it (services/errors.py); this only presents it.
  function showCause(host, cause) {
    if (!cause) return;
    const actions = [];
    if (cause.action_url) actions.push({ label: cause.action_label || "Open", href: cause.action_url, quiet: true });
    U.banner(host, {
      kind: cause.severity === "warn" ? "warn" : cause.severity === "info" ? "info" : "error",
      id: "cause-" + cause.kind,
      title: cause.title,
      detail: cause.detail,
      raw: cause.raw,
      actions,
    });
  }

  // ── stranded renders ─────────────────────────────────────────────────────
  // A render that finished on HeyGen but died on the download/NAS tail is
  // re-runnable; these endpoints previously had no way in but curl.
  const recoverWrap = $("#recoverWrap"), recoverN = $("#recoverN");
  async function refreshRecoverable() {
    try {
      const r = await U.fetchT("/video/heygen/jobs/recoverable", {}, 10000);
      if (!r.ok) throw new Error();
      const n = (await r.json()).count || 0;
      recoverWrap.hidden = n === 0;
      recoverN.textContent = n;
    } catch (_) { recoverWrap.hidden = true; }
  }
  $("#recoverBtn").addEventListener("click", () => {
    const n = recoverN.textContent;
    U.confirm({
      title: `Recover ${n} stranded render${n === "1" ? "" : "s"}?`,
      message: "These finished on HeyGen but never reached the NAS. Recovering re-downloads them — it does not re-render, so it costs no credits.",
      confirmLabel: "Recover", danger: false,
      onOk: async () => {
        show("video");
        try {
          const r = await U.fetchT("/video/heygen/recover-failed", { method: "POST" }, 20000);
          if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`);
          const d = await r.json();
          U.banner($("#videoBanners"), { kind: "info", title: `Recovering ${d.count} render${d.count === 1 ? "" : "s"}`,
            detail: "They are being re-downloaded and filed to the NAS." });
        } catch (e) {
          U.banner($("#videoBanners"), { kind: "error", title: "Could not start recovery",
            raw: String(e && e.message || e) });
        }
        setTimeout(refreshRecoverable, 4000);
      },
    });
  });

  // ── boot ─────────────────────────────────────────────────────────────────
  window.Shell = {
    configured: () => envConfigured,
    setRunning, showCause, refreshRecoverable, show,
  };

  show(location.pathname.startsWith("/videogen") ? "video" : "audio", false);
  refreshEnv();
  setInterval(refreshEnv, 20000);
  refreshRecoverable();
  setInterval(() => { if (!running.video) refreshRecoverable(); }, 30000);
})();
