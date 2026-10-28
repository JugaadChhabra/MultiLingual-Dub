/* Video pane — boot. Loaded last: everything it calls is defined by now. */
(() => {
  "use strict";
  const U = window.UI, V = window.Video;
  const { els, state } = V;
  const { ACTIVE, MODE, SET } = V.keys;

  // ── boot ─────────────────────────────────────────────────────────────────
  V.cats = V.loadCats();
  const savedActive = localStorage.getItem(ACTIVE);
  V.activeId = (savedActive && V.cats.some((c) => c.id === savedActive)) ? savedActive : (V.cats[0] ? V.cats[0].id : null);
  V.renderCats();
  els.voice.querySelectorAll("button").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.char === state.character)));
  let savedMode = null;
  try { savedMode = localStorage.getItem(MODE); } catch (_) {}
  let savedSet = null;
  try { savedSet = localStorage.getItem(SET); } catch (_) {}
  V.applySet(savedSet === "single" ? "single" : "zodiac");
  V.restoreDrafts();
  V.applyMode(["auto", "single", "batch"].includes(savedMode) ? savedMode : "auto");
  V.renderDrafts();
  V.showPlaceholder();
  V.reattach();

  window.PaneVideo = { refreshRun: V.refreshRun, isRunning: () => state.running, fitDrafts: V.fitAll };
})();
