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
    // The draft boxes size themselves to their text, which they cannot measure
    // while the pane is hidden. This is the moment it stops being hidden.
    if (section === "video" && window.PaneVideo && window.PaneVideo.fitDrafts) {
      window.PaneVideo.fitDrafts();
    }
    if (push) history.replaceState({}, "", section === "video" ? "/videogen" : "/");
  }
  document.querySelectorAll(".sbi[data-section]").forEach((b) =>
    b.addEventListener("click", () => show(b.dataset.section)));

  // ── sidebar ──────────────────────────────────────────────────────────────
  // Collapses to an icon rail, not to nothing, so the other pipeline is still
  // one click away. Remembered per browser: an editor who works with it closed
  // should not have to close it every morning.
  const SB_KEY = "autodub_sidebar";
  const app = document.querySelector(".app"), sbToggle = $("#sbToggle");
  let railed = false;

  function applySidebar(rail, persist) {
    railed = Boolean(rail);
    app.dataset.sidebar = railed ? "rail" : "full";
    sbToggle.setAttribute("aria-expanded", String(!railed));
    // Modifier is written the way the platform writes it, since this is the
    // tooltip an operator reads to learn the shortcut.
    const key = navigator.platform.toLowerCase().includes("mac") ? "⌘B" : "Ctrl+B";
    sbToggle.title = `${railed ? "Show" : "Hide"} sidebar (${key})`;
    // Labels only become tooltips once the rail has taken them away; leaving
    // them on would put a tooltip over text that is already on screen.
    document.querySelectorAll(".sbi .lbl").forEach((lbl) => {
      const btn = lbl.closest(".sbi");
      if (!railed) { btn.removeAttribute("title"); return; }
      // The count sits in .meta, which the rail also hides, so it joins the
      // label — otherwise collapsing silently drops how many are stranded.
      const meta = btn.querySelector(".meta");
      const count = meta && meta.textContent.trim();
      btn.title = lbl.textContent.trim() + (count ? ` (${count})` : "");
    });
    if (persist) { try { localStorage.setItem(SB_KEY, railed ? "rail" : "full"); } catch (_) {} }
  }
  sbToggle.addEventListener("click", () => applySidebar(!railed, true));
  window.addEventListener("keydown", (e) => {
    if (e.key !== "b" && e.key !== "B") return;
    if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
    // Never steal the keystroke from someone writing a script.
    const el = document.activeElement, tag = el ? el.tagName : "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (el && el.isContentEditable)) return;
    e.preventDefault();
    applySidebar(!railed, true);
  });

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
      // The rail shows this count only in a tooltip, so it has to be rewritten
      // when the count changes.
      if (railed) applySidebar(true, false);
    } catch (_) { recoverWrap.hidden = true; }
  }
  $("#recoverBtn").addEventListener("click", () => {
    const n = recoverN.textContent;
    U.confirm({
      title: `Recover ${n} stranded render${n === "1" ? "" : "s"}?`,
      message: "These rendered successfully but never reached the NAS. Recovering re-downloads them — it does not re-render, so it costs no credits.",
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

  let savedSidebar = null;
  try { savedSidebar = localStorage.getItem(SB_KEY); } catch (_) {}
  applySidebar(savedSidebar === "rail", false);
  show(location.pathname.startsWith("/videogen") ? "video" : "audio", false);
  refreshEnv();
  setInterval(refreshEnv, 20000);
  refreshRecoverable();
  setInterval(() => { if (!running.video) refreshRecoverable(); }, 30000);
})();
