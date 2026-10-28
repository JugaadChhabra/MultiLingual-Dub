/* Story pane — a pasted story, broken into voiceover lines for review.
   Paste → Review. Generate arrives in a later ticket; nothing here spends
   text-to-speech credits. */
(() => {
  "use strict";
  const U = window.UI, $ = U.$;

  const els = {
    banners: $("#storyBanners"), paste: $("#sPaste"), review: $("#sReview"),
    title: $("#sTitle"), activity: $("#sActivity"), text: $("#sText"),
    langs: $("#sLangs"), selN: $("#sSelN"), groups: $("#sGroups"),
    split: $("#sSplit"), msg: $("#sMsg"), live: $("#storyLive"),
    lines: $("#sLines"), lineN: $("#sLineN"), reviewActivity: $("#sReviewActivity"),
    back: $("#sBack"), reviewMsg: $("#sReviewMsg"),
  };

  // emotions and maxLineChars come from the split response, so the dropdown and
  // the server's validator share one list
  const state = { splitting: false, activityEdited: false, lines: [], emotions: [], maxLineChars: 0 };

  const SEL = U.langPicker({ tokens: els.langs, groups: els.groups, count: els.selN });

  // ── paste ────────────────────────────────────────────────────────────────
  // The same rule the server applies; shown live so the operator can see and
  // change it before anything is sent.
  const activityFrom = (title) => title.toLowerCase().replace(/[^a-z0-9]/g, "") + "story";
  els.title.addEventListener("input", () => {
    if (!state.activityEdited) els.activity.value = activityFrom(els.title.value);
    refreshRun();
  });
  els.activity.addEventListener("input", () => {
    // clearing the field hands it back to the title
    state.activityEdited = els.activity.value.trim() !== "";
    if (!state.activityEdited) els.activity.value = activityFrom(els.title.value);
    refreshRun();
  });
  els.text.addEventListener("input", refreshRun);

  function blocker() {
    if (!els.title.value.trim()) return "Give the story a title";
    if (!els.text.value.trim()) return "Paste a story to start";
    return null;
  }
  function refreshRun() {
    if (state.splitting) { els.split.disabled = true; return; }
    const why = blocker();
    els.split.disabled = Boolean(why);
    els.msg.textContent = why || "";
  }
  const say = (text) => { els.msg.textContent = text || ""; };

  // ── break into lines ─────────────────────────────────────────────────────
  els.split.addEventListener("click", async () => {
    if (blocker() || state.splitting) return;
    state.splitting = true;
    U.clearBanners(els.banners);
    say("Gemini is reading the story…");
    els.split.innerHTML = `<span class="spin"></span>Breaking into lines…`;
    els.live.hidden = false;
    window.Shell.setRunning("story", true, "Story splitting");
    refreshRun();
    try {
      // Up to two Gemini calls when the first split is rejected, so this waits
      // longer than a normal request.
      const r = await U.fetchT("/batch/story/split", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: els.title.value, text: els.text.value }),
      }, 240000);
      if (!r.ok) {
        let detail = "";
        try { detail = (await r.json()).detail; } catch (_) { detail = `HTTP ${r.status}`; }
        // FastAPI's own 422s carry a list of problems, not a sentence
        if (typeof detail !== "string") detail = JSON.stringify(detail);
        throw Object.assign(new Error(detail || `HTTP ${r.status}`), { status: r.status });
      }
      const d = await r.json();
      state.lines = d.lines.map((l) => ({ text: l.text, emotion: l.emotion }));
      state.emotions = d.emotions || [];
      state.maxLineChars = d.max_line_chars || 0;
      if (!state.activityEdited) els.activity.value = d.activity_name;
      say("");
      showReview();
    } catch (e) {
      const raw = e.name === "AbortError" ? "Gemini took too long to answer." : String(e.message || e);
      // a 400 is about the story itself; anything else is the service
      U.banner(els.banners, {
        kind: "error", id: "story-split",
        title: e.status === 400 ? "The story could not be broken into lines" : "Gemini could not be reached",
        detail: e.status === 400 ? U.esc(raw) : "Nothing was spent. Try again in a minute.",
        raw: e.status === 400 ? undefined : raw,
      });
    } finally {
      state.splitting = false;
      els.split.textContent = "Break into lines";
      els.live.hidden = true;
      window.Shell.setRunning("story", false);
      refreshRun();
    }
  });

  // ── review ───────────────────────────────────────────────────────────────
  function showReview() {
    els.paste.hidden = true;
    els.review.hidden = false;
    els.reviewActivity.textContent = els.activity.value.trim();
    renderLines();
  }
  els.back.addEventListener("click", () => {
    els.review.hidden = true;
    els.paste.hidden = false;
    els.text.focus();
  });

  const tagged = (l) => `[${l.emotion}] ${l.text}`.length;
  const fit = (area) => { area.style.height = "auto"; area.style.height = area.scrollHeight + "px"; };

  function renderLines() {
    els.lines.innerHTML = "";
    els.lineN.textContent = state.lines.length;
    const options = state.emotions.map((e) => `<option value="${U.esc(e)}">${U.esc(e)}</option>`).join("");
    state.lines.forEach((line, i) => {
      const row = document.createElement("div");
      row.className = "qrow";
      row.innerHTML =
        `<span class="i">${i + 1}</span>` +
        `<textarea class="area" rows="1" aria-label="Line ${i + 1} text"></textarea>` +
        `<select class="fld" aria-label="Line ${i + 1} emotion">${options}</select>`;
      const area = row.querySelector("textarea"), pick = row.querySelector("select");
      area.value = line.text;
      pick.value = line.emotion;
      area.addEventListener("input", () => { line.text = area.value; fit(area); flag(row, line); });
      pick.addEventListener("change", () => { line.emotion = pick.value; flag(row, line); });
      els.lines.appendChild(row);
      fit(area);
      flag(row, line, false);
    });
    summarise();
  }

  // A line too long to voice, or emptied, is marked where it is.
  function flag(row, line, resummarise = true) {
    const bad = !line.text.trim() || (state.maxLineChars && tagged(line) > state.maxLineChars);
    row.dataset.flag = bad ? "bad" : "";
    if (resummarise) summarise();
  }
  function summarise() {
    const empty = state.lines.filter((l) => !l.text.trim()).length;
    const long = state.maxLineChars ? state.lines.filter((l) => tagged(l) > state.maxLineChars).length : 0;
    const problems = [];
    if (empty) problems.push(`${empty} empty line${empty === 1 ? "" : "s"}`);
    if (long) problems.push(`${long} line${long === 1 ? "" : "s"} over ${state.maxLineChars} characters`);
    els.reviewMsg.textContent = problems.length
      ? problems.join(" · ")
      : `${state.lines.length} lines × ${SEL.size} language${SEL.size === 1 ? "" : "s"}`;
    els.reviewMsg.classList.toggle("err", problems.length > 0);
  }

  // The line boxes size themselves to their text, which they cannot measure
  // while the pane is hidden.
  function fitLines() { els.lines.querySelectorAll("textarea").forEach(fit); }

  refreshRun();
  window.PaneStory = { refreshRun, fitLines, isSplitting: () => state.splitting };
})();
