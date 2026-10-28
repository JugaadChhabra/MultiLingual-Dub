/* Video pane — the video editor's screen.
   An avatar and a script in, rendered video out, filed to the NAS.

   Three modes, in the order they cost the operator work: Auto writes the day's
   scripts from the category's brief and shows them to be read and edited, Single
   takes one typed script, Batch takes a spreadsheet of them. Auto and Batch end
   up at the same endpoint — reviewed drafts and spreadsheet rows are the same
   thing by the time they are submitted.

   Split across static/video/*.js, loaded in order. This file goes first: it
   owns the shipped presets, the category store, the elements and the state,
   and creates window.Video, the one object the other files share. A function
   another file needs is put on it and called through it (V.refreshRun()),
   late, so load order only matters for what runs at load. */
(() => {
  "use strict";
  const U = window.UI, $ = U.$;

  // Shipped preset. localStorage holds only the operator's changes on top of
  // this, so editing here ships new copy immediately unless they edited it too.
  const SEED = [{
    id: "sunsign", label: "Horoscope",
    // Written off the hand-authored reference sheet, so a generated day is
    // indistinguishable in shape from the days the editor wrote themselves.
    // The fortune line's FORMAT is fixed there too; its values are not, and
    // neither is the health line \u2014 a sentence identical across twelve signs
    // every day is the one duplicate the uniqueness rules used to step around.
    //
    // Areas are drawn from a per-sign pool rather than fixed, so ten days do not
    // produce ten variations on one script. Every pool carries at least six:
    // five would give exactly C(5,3)=10 combinations, which is zero slack over a
    // ten-day window and impossible on day eleven.
    script_brief: "A daily horoscope for one zodiac sign, for an Indian audience, voiced by a calm and authoritative astrologer.\n\nEach sign has a pool of life areas. Choose THREE from that sign's pool for today, and choose a combination this sign has not used on a recent day. Vary which three, and the order you name them in.\n\nमेष — नई शुरुआत, ऊर्जा, नेतृत्व, साहस, प्रतिस्पर्धा, आत्मविश्वास\nवृषभ — धन, रिश्ते, सुख-सुविधा, स्थिरता, भोजन, धैर्य\nमिथुन — संवाद, यात्रा, सामाजिक जीवन, सीखना, मित्रता, विचार\nकर्क — परिवार, घर, भावनात्मक जीवन, स्मृतियाँ, देखभाल, अंतर्ज्ञान\nसिंह — करियर, सम्मान, नेतृत्व, रचनात्मकता, आत्मप्रदर्शन, गर्व\nकन्या — स्वास्थ्य, कार्यक्षेत्र, योजना, अनुशासन, सेवा, विवरण\nतुला — रिश्ते, साझेदारी, संतुलन, न्याय, सौंदर्य, सामाजिक मेलजोल\nवृश्चिक — आंतरिक शक्ति, गोपनीयता, परिवर्तन, गहराई, जुनून, रहस्य\nधनु — यात्रा, ज्ञान, विस्तार, स्वतंत्रता, आशावाद, दर्शन\nमकर — करियर, अनुशासन, जिम्मेदारी, महत्वाकांक्षा, धैर्य, संरचना\nकुंभ — मित्रता, नवाचार, सामाजिक कार्य, स्वतंत्र सोच, समुदाय, विद्रोह\nमीन — आध्यात्म, कल्पनाशीलता, भावनाएं, करुणा, सपने, अंतर्ज्ञान\n\nHook paragraph: address the sign's natives and then name today's three areas, as a bare list with no connecting clause, ending in an exclamation — exactly this shape: \"<राशि> राशि के जातकों... आज नई शुरुआत, ऊर्जा और नेतृत्व!\"\n\nBody paragraph: one concrete, relatable prediction per area, in the order named. Keep them broad and human — no job titles, no industries, no technical specifics. Drive each prediction with a different narrative angle, rotating across these five and never using the same angle twice in one script: an opportunity arriving (\"एक अवसर मिलेगा...\"), a tangle resolving (\"एक उलझन सुलझेगी...\"), an unexpected turn (\"अप्रत्याशित रूप से...\"), support from someone close (\"किसी अपने का सहयोग मिलेगा...\"), an inner shift (\"आपके भीतर स्पष्टता आएगी...\").\n\nThen one line on health and mental wellbeing, phrased fresh every time \u2014 your own words, never a sentence you have used for another sign today or on a recent day. Then close the paragraph with, in this exact format: \"शुभ रंग: <a vivid specific colour, e.g. गहरा रूबी रेड> | जादुई अंक: <a number from 10 to 99 in words, with the Devanagari digits in brackets, e.g. सत्रह (१७)>।\"\n\nThe lucky number is always double-digit — never a single digit, never three digits.\n\nClosing paragraph: one line of direct encouragement to this sign, matching what was predicted.\n\nThe three areas, the colour and the number must differ across all twelve signs and must not repeat what recent days used. A different shade of a colour already used still counts as that colour.",
    // motion_prompt drives BODY MOTION and HAND GESTURES only — not facial
    // expression, lip-sync or eyes (the engine handles those from the audio),
    // and not energy level (that is the separate `expressiveness` control). So
    // this describes posture, head and hands and nothing the parameter ignores.
    motion_prompt: "The speaker faces the camera directly with a calm, grounded presence, holding an upright and mostly still posture with relaxed shoulders and only subtle, natural weight shifts — no walking or large body movements. The head moves gently in time with the speech: slow, affirming nods and occasional soft side-to-side tilts. The hands make slow, deliberate open-palm gestures near the chest — opening outward to reassure, then settling back to rest — unhurried, smooth, and never repetitive or fidgety. The overall motion is serene and warm, conveying quiet authority and compassion.",
  }];
  const SEED_IDS = new Set(SEED.map((c) => c.id));
  // A retired id is no longer in SEED_IDS, so without this the stored `user`
  // array would resurrect it as an operator-created category.
  const RETIRED = new Set(["birthday"]);
  const FIELDS = ["label", "motion_prompt", "script_brief"];
  const STORE = "autodub_video_cat_store_v1", ACTIVE = "autodub_video_active_cat";
  const CHAR = "autodub_video_character", JOB_KEY = "autodub_video_job";
  const MODE = "autodub_video_mode", LANG = "autodub_video_script_lang";
  const SET = "autodub_video_script_set", DRAFTS = "autodub_video_drafts";
  // Drafts older than this are not worth restoring — a horoscope for last week
  // is not a draft, it is history.
  const DRAFT_KEEP_DAYS = 7;
  // Matches MAX_SCRIPT_CHARS in services/script_writer.py and the maxlength on
  // the single-mode textarea: past this HeyGen will not render the script.
  // Checked here for fast feedback; the server enforces the same cap in
  // services/video_pipeline/intake.py.
  const MAX_SCRIPT = 1000;
  // Which languages get the native face in the draft editor. Latin-script
  // targets read worse in it than in the UI font.
  const NATIVE_LANGS = new Set(["bn-IN", "gu-IN", "hi-IN", "kn-IN", "ml-IN", "mr-IN",
                                "od-IN", "pa-IN", "ta-IN", "te-IN", "ru"]);

  // Reassigned by the category editor and the boot, so read through V, never copied.
  const V = window.Video = { cats: [], activeId: null };
  const seedById = (id) => SEED.find((c) => c.id === id);

  function loadStore() {
    try { const s = JSON.parse(localStorage.getItem(STORE)); if (s && typeof s === "object" && !Array.isArray(s)) return s; }
    catch (_) {}
    return {};
  }
  function loadCats() {
    const s = loadStore();
    const deleted = new Set(Array.isArray(s.deleted) ? s.deleted : []);
    const ov = s.overrides && typeof s.overrides === "object" ? s.overrides : {};
    const out = [];
    SEED.forEach((c) => { if (!deleted.has(c.id)) out.push({ ...c, ...(ov[c.id] || {}) }); });
    if (Array.isArray(s.user)) s.user.forEach((c) => { if (c && c.id && !RETIRED.has(c.id)) out.push({ ...c }); });
    return out;
  }
  function persist() {
    const overrides = {}, user = [], present = new Set();
    V.cats.forEach((c) => {
      if (SEED_IDS.has(c.id)) {
        present.add(c.id);
        const base = seedById(c.id), diff = {};
        FIELDS.forEach((k) => { if (c[k] !== base[k]) diff[k] = c[k]; });
        if (Object.keys(diff).length) overrides[c.id] = diff;
      } else user.push({ id: c.id, ...Object.fromEntries(FIELDS.map((k) => [k, c[k]])) });
    });
    const deleted = SEED.map((c) => c.id).filter((id) => !present.has(id));
    try {
      localStorage.setItem(STORE, JSON.stringify({ overrides, deleted, user }));
      localStorage.setItem(ACTIVE, V.activeId || "");
    } catch (_) {}
  }
  const activeCat = () => V.cats.find((c) => c.id === V.activeId) || V.cats[0] || null;
  const newId = (label) => (label || "cat").toLowerCase().replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "").slice(0, 24) + "-" + Math.floor(Math.random() * 1e6).toString(36);

  const els = {
    banners: $("#videoBanners"), monitor: $("#vMonitor"), body: $("#vBody"), tag: $("#vTag"),
    file: $("#vFile"), fileName: $("#vFileName"), fileSize: $("#vFileSize"), img: $("#vImg"),
    cats: $("#vCats"), mode: $("#vMode"), voice: $("#vVoice"), date: $("#vDate"),
    single: $("#vSingle"), batch: $("#vBatch"), script: $("#vScript"), count: $("#vCount"),
    auto: $("#vAuto"), lang: $("#vLang"), gen: $("#vGen"), genSub: $("#vGenSub"), drafts: $("#vDrafts"),
    set: $("#vSet"), autoTitleRow: $("#vAutoTitleRow"), autoTitle: $("#vAutoTitle"),
    inner: document.querySelector("#pane-video .pane-inner"),
    work: $("#vWork"), draftsWrap: $("#vDraftsWrap"), queueWrap: $("#vQueueWrap"),
    draftsN: $("#vDraftsN"),
    title: $("#vTitle"), overlay: $("#vOverlay"), music: $("#vMusic"), drop: $("#vDrop"), xls: $("#vXls"),
    dropTitle: $("#vDropTitle"), dropSub: $("#vDropSub"), queue: $("#vQueue"),
    progress: $("#videoProgress"), pCount: $("#vpCount"), pDetail: $("#vpDetail"),
    up: $("#vpUp"), done: $("#vpDone"), bad: $("#vpBad"),
    upN: $("#vpUpN"), doneN: $("#vpDoneN"), badN: $("#vpBadN"), elapsed: $("#vpElapsed"),
    run: $("#vRun"), ctl: $("#vCtl"), pause: $("#vPause"), cancel: $("#vCancel"), msg: $("#vMsg"), live: $("#videoLive"),
  };

  const state = { image: null, photoId: null, stillURL: null, excel: null, excelProblem: null,
                  mode: "auto", character: localStorage.getItem(CHAR) || "indian",
                  // Written scripts the operator is reviewing. Held here and not
                  // on the server: nothing is committed until Run.
                  drafts: [], writing: false, set: "zodiac",
                  running: false, jobId: null, kind: null, startedAt: null,
                  paused: false, cancelling: false };
  // Auto and Batch both submit rows, so everything about the run — the endpoint,
  // the progress rendering, the reattach — keys off this, not off the mode.
  const submitsRows = () => state.mode !== "single";

  // The run message line. Shared: the writer and the run both report on it.
  const say = (t, bad) => { els.msg.textContent = t || ""; els.msg.classList.toggle("err", !!bad); };
  Object.assign(V, {
    SEED, FIELDS, MAX_SCRIPT, NATIVE_LANGS, DRAFT_KEEP_DAYS,
    keys: { STORE, ACTIVE, CHAR, JOB_KEY, MODE, LANG, SET, DRAFTS },
    els, state, seedById, loadCats, persist, activeCat, newId, submitsRows, say,
  });
})();
