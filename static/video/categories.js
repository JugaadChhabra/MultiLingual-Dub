/* Video pane — prompt categories: the chips, and the drawer that edits one. */
(() => {
  "use strict";
  const U = window.UI, $ = U.$, V = window.Video;
  const { els, FIELDS } = V;

  // ── categories ───────────────────────────────────────────────────────────
  // Chip and edit button are one visual control split by a hairline — the edit
  // affordance as its own floating card read as a mystery third button.
  function renderCats() {
    els.cats.innerHTML = "";
    V.cats.forEach((c) => {
      const wrap = document.createElement("div");
      wrap.className = "chipwrap" + (c.id === V.activeId ? " on" : "");
      const chip = document.createElement("button");
      chip.type = "button"; chip.className = "token chip"; chip.dataset.id = c.id;
      chip.setAttribute("aria-pressed", String(c.id === V.activeId));
      chip.textContent = c.label;
      chip.addEventListener("click", () => { V.activeId = c.id; V.persist(); renderCats(); V.refreshRun(); });
      const pen = document.createElement("button");
      pen.type = "button"; pen.className = "pen";
      pen.setAttribute("aria-label", `Edit ${c.label} prompts`);
      pen.title = `Edit ${c.label} prompts`;
      pen.innerHTML = U.icon("edit");
      pen.addEventListener("click", () => openEditor(c.id));
      wrap.append(chip, pen);
      els.cats.appendChild(wrap);
    });
    const add = document.createElement("button");
    add.type = "button"; add.className = "token add"; U.label(add, "New", "add");
    add.setAttribute("aria-label", "New prompt category");
    add.addEventListener("click", () => openEditor(null));
    els.cats.appendChild(add);
    V.refreshRun();
  }

  // ── category editor ──────────────────────────────────────────────────────
  const drawer = $("#catDrawer");
  const name = $("#catName"), mprompt = $("#catMotion");
  const brief = $("#catBrief");
  const errEl = $("#catErr"), dirtyEl = $("#catDirty");
  let editingId = null, snapshot = null;
  const fields = () => ({ label: name.value.trim(),
                          motion_prompt: mprompt.value.trim(), script_brief: brief.value.trim() });
  const dirty = () => snapshot !== null && JSON.stringify(fields()) !== JSON.stringify(snapshot);

  const catOv = U.overlay(drawer, $("#catScrim"), {
    focus: () => name,
    fallbackFocus: () => els.cats.querySelector("button"),
    beforeClose() {
      if (!dirty()) return true;
      U.confirm({ title: "Discard your changes?",
        message: "This category has edits that have not been saved yet.",
        confirmLabel: "Discard", onOk: () => { snapshot = null; catOv.close(); } });
      return false;
    },
  });
  function refreshEditor() {
    dirtyEl.hidden = !dirty();
    $("#catMotionN").textContent = mprompt.value.length || "";
    $("#catBriefN").textContent = brief.value.length || "";
    const seed = editingId ? V.seedById(editingId) : null;
    const cur = editingId ? V.cats.find((c) => c.id === editingId) : null;
    $("#catReset").hidden = !(seed && cur && FIELDS.some((k) => cur[k] !== seed[k]));
  }
  [name, mprompt, brief].forEach((el) => el.addEventListener("input", refreshEditor));

  function openEditor(id, from) {
    editingId = id;
    const c = id ? V.cats.find((x) => x.id === id) : from || null;
    $("#catTitle").textContent = id ? "Edit category" : "New category";
    $("#catSub").textContent = id ? c.label : from ? `Copy of ${from.label}` : "Motion prompt preset";
    name.value = c ? (id ? c.label : `${c.label} copy`) : "";
    mprompt.value = c ? c.motion_prompt || "" : "";
    brief.value = c ? c.script_brief || "" : "";
    $("#catDelete").hidden = !id; $("#catDuplicate").hidden = !id;
    errEl.hidden = true;
    snapshot = fields(); refreshEditor();
    catOv.open();
  }
  $("#catClose").addEventListener("click", catOv.close);
  $("#catSave").addEventListener("click", save);
  drawer.addEventListener("keydown", (e) => {
    // textareas, so plain Return must stay a newline
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); save(); }
  });
  function save() {
    const label = name.value.trim();
    if (!label) { errEl.textContent = "A name is required."; errEl.hidden = false; name.focus(); return; }
    if (V.cats.some((c) => c.id !== editingId && c.label.toLowerCase() === label.toLowerCase())) {
      errEl.textContent = "Another category already uses that name."; errEl.hidden = false; name.focus(); return;
    }
    errEl.hidden = true;
    const data = fields();
    if (editingId) Object.assign(V.cats.find((x) => x.id === editingId), data);
    else { const id = V.newId(label); V.cats.push({ id, ...data }); V.activeId = id; }
    V.persist(); renderCats(); V.refreshRun();
    snapshot = null;              // saving means done — the drawer closes
    catOv.close();
  }
  $("#catDuplicate").addEventListener("click", () => {
    const c = V.cats.find((x) => x.id === editingId); if (!c) return;
    snapshot = null; openEditor(null, c);
  });
  $("#catReset").addEventListener("click", () => {
    const seed = V.seedById(editingId); if (!seed) return;
    U.confirm({ title: "Reset to the shipped default?",
      message: `“${seed.label}” goes back to the prompts this build ships with. Your edits to it are lost.`,
      confirmLabel: "Reset",
      onOk: () => {
        name.value = seed.label;
        mprompt.value = seed.motion_prompt; brief.value = seed.script_brief || "";
        Object.assign(V.cats.find((x) => x.id === editingId), { ...seed });
        V.persist(); renderCats(); snapshot = fields(); refreshEditor();
      } });
  });
  $("#catDelete").addEventListener("click", () => {
    if (!editingId) return;
    const id = editingId, c = V.cats.find((x) => x.id === id);
    U.confirm({ title: "Delete this category?",
      message: `“${c ? c.label : "This category"}” and its prompts will be removed. This cannot be undone.`,
      confirmLabel: "Delete",
      onOk: () => {
        V.cats = V.cats.filter((x) => x.id !== id);
        if (V.activeId === id) V.activeId = V.cats[0] ? V.cats[0].id : null;
        V.persist(); renderCats(); snapshot = null; catOv.close();
      } });
  });

  Object.assign(V, { renderCats, openEditor });
})();
