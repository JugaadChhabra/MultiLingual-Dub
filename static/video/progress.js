/* Video pane — a run's progress as the poll reports it: the bar, the counts
   and, for a batch, the row queue. */
(() => {
  "use strict";
  const U = window.UI, V = window.Video;
  const { els, state } = V;

  function renderSingle(p) {
    const done = p.status === "completed" ? 1 : 0;
    const failed = p.status === "failed" ? 1 : 0;
    els.pCount.innerHTML = `${done} <small>of 1 video</small>`;
    const over = done || failed || p.status === "cancelled";
    els.pDetail.textContent = p.cancel_requested && !over ? "Cancelling at the next step…"
      : p.stage_message || p.status || "";
    els.upN.textContent = done; els.doneN.textContent = over ? 0 : 1; els.badN.textContent = failed;
    U.setBar(els, { total: 1, uploaded: done, rendered: over ? 0 : 1, failed, live: !over });
    els.elapsed.textContent = state.startedAt ? `Started ${U.clock(state.startedAt)}` : "";
  }
  function renderBatch(p) {
    const rows = p.rows || [], total = p.total || rows.length || 1;
    const uploaded = rows.filter((r) => r.status === "completed" && r.nas_path).length;
    const doneNoNas = rows.filter((r) => r.status === "completed" && !r.nas_path).length;
    const failed = rows.filter((r) => r.status === "failed").length;
    els.pCount.innerHTML = `${uploaded + doneNoNas} <small>of ${total} videos</small>`;
    els.upN.textContent = uploaded; els.doneN.textContent = doneNoNas; els.badN.textContent = failed;
    U.setBar(els, { total, uploaded, rendered: doneNoNas, failed,
                    live: p.status === "running" && !p.paused && !p.cancel_requested });
    const left = p.paused || p.cancel_requested ? null : U.eta(state.startedAt, uploaded + doneNoNas, total);
    const held = p.cancel_requested ? "Cancelling — finishing the step each row is on"
      : p.paused ? "Paused — renders HeyGen already has will finish" : null;
    els.pDetail.textContent = [`${uploaded + doneNoNas} of ${total} rows`, held || left].filter(Boolean).join(" · ");
    els.elapsed.textContent = state.startedAt ? `Started ${U.clock(state.startedAt)}` : "";
    renderQueue(rows);
  }
  function renderQueue(rows) {
    if (!rows.length) return;
    els.queue.innerHTML = "";
    let latest = null;
    rows.forEach((row) => {
      const s = row.status || "pending";
      const badge = s === "completed" ? "g" : s === "failed" ? "r" : s === "running" ? "a" : "";
      const el = document.createElement("div");
      el.className = "qrow";
      el.innerHTML = `<span class="i">row ${row.row_index}</span>` +
        `<span class="nm">${U.esc(row.video_title || "untitled")}</span>` +
        // A failed row has no NAS path to show, so its error takes that slot:
        // first line only, the whole message on hover.
        (s === "failed" && row.error
          ? `<span class="path err" title="${U.esc(row.error)}">${U.esc(row.error.split("\n")[0])}</span>`
          : `<span class="path">${U.esc(row.nas_path || "—")}</span>`) +
        `<span class="badge ${badge}">${U.esc(s[0].toUpperCase() + s.slice(1))}</span>` +
        (row.video_local_url ? `<a class="btn q" href="${row.video_local_url}" download>Open</a>` : `<span></span>`);
      els.queue.appendChild(el);
      if (s === "completed" && row.video_local_url) latest = row.video_local_url;
    });
    // Once, after the loop, rather than once per completed row: calling it
    // inside meant the monitor was rebuilt twelve times a tick and settled on
    // whichever row happened to be last.
    if (latest) V.showVideo(latest);
  }

  Object.assign(V, { renderSingle, renderBatch });
})();
