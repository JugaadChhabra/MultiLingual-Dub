      const form = document.getElementById("uploadForm");
      const envStatus = document.getElementById("envStatus");
      const envBadge = document.getElementById("envBadge");
      const excelFile = document.getElementById("excelFile");
      const excelStatus = document.getElementById("excelStatus");
      const submitBtn = document.getElementById("submitBtn");
      const submitStatusWrap = document.getElementById("submitStatus");
      const submitStatusText = document.getElementById("submitStatusText");
      const spinner = document.getElementById("spinner");
      const result = document.getElementById("result");
      const multiResults = document.getElementById("multiResults");
      const logStatusText = document.getElementById("logStatusText");
      const logStatusBadge = document.getElementById("logStatusBadge");
      const logOutput = document.getElementById("logOutput");

      // Drop zone elements
      const dropZone = document.getElementById("dropZone");
      const dropZoneContent = document.getElementById("dropZoneContent");
      const dropZoneFile = document.getElementById("dropZoneFile");
      const dropFileName = document.getElementById("dropFileName");
      const dropFileSize = document.getElementById("dropFileSize");
      const dropClearBtn = document.getElementById("dropClearBtn");

      let envConfigured = false;
      let latestJobState = null;
      let durationTicker = null;
      let logPollTimer = null;
      let lastLogId = 0;
      const logLines = [];

      // ── Toast System ──

      function showToast(message, type = "info", durationMs = 4000) {
        const stack = document.getElementById("toastStack");
        const toast = document.createElement("div");
        toast.className = `toast toast-${type}`;
        const iconPaths = {
          success: '<path d="M5 13l4 4L19 7"/>',
          error: '<path d="M6 6l12 12M18 6L6 18"/>',
          info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
        };
        toast.innerHTML = `<svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">${iconPaths[type] || iconPaths.info}</svg><span>${message}</span>`;
        stack.appendChild(toast);
        setTimeout(() => {
          toast.classList.add("toast-out");
          toast.addEventListener("animationend", () => toast.remove());
        }, durationMs);
      }

      // ── Sound Feedback ──

      function isSoundEnabled() {
        return document.getElementById("soundToggle")?.checked || false;
      }

      function playTone(freq, type, duration) {
        if (!isSoundEnabled()) return;
        try {
          const ctx = new (window.AudioContext || window.webkitAudioContext)();
          const osc = ctx.createOscillator();
          const gain = ctx.createGain();
          osc.type = type;
          osc.frequency.value = freq;
          gain.gain.setValueAtTime(0.12, ctx.currentTime);
          gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + duration);
          osc.connect(gain).connect(ctx.destination);
          osc.start();
          osc.stop(ctx.currentTime + duration);
        } catch (_) {}
      }

      function playSuccessSound() {
        playTone(523, "sine", 0.15);
        setTimeout(() => playTone(659, "sine", 0.15), 100);
        setTimeout(() => playTone(784, "sine", 0.25), 200);
      }

      function playFailSound() {
        playTone(330, "sawtooth", 0.3);
        setTimeout(() => playTone(262, "sawtooth", 0.4), 200);
      }

      // Persist sound toggle
      const soundToggle = document.getElementById("soundToggle");
      soundToggle.checked = localStorage.getItem("autodub-sound") === "true";
      soundToggle.addEventListener("change", () => {
        localStorage.setItem("autodub-sound", soundToggle.checked);
      });

      // ── Particle Burst ──

      function emitParticles(x, y, color, count = 18) {
        const container = document.createElement("div");
        container.className = "particle-container";
        document.body.appendChild(container);
        for (let i = 0; i < count; i++) {
          const p = document.createElement("div");
          p.className = "particle";
          const angle = (Math.PI * 2 * i) / count + (Math.random() - 0.5) * 0.5;
          const dist = 40 + Math.random() * 80;
          p.style.left = `${x}px`;
          p.style.top = `${y}px`;
          p.style.background = color;
          p.style.setProperty("--px", `${Math.cos(angle) * dist}px`);
          p.style.setProperty("--py", `${Math.sin(angle) * dist}px`);
          p.style.width = `${3 + Math.random() * 4}px`;
          p.style.height = p.style.width;
          p.style.animationDuration = `${600 + Math.random() * 400}ms`;
          container.appendChild(p);
        }
        setTimeout(() => container.remove(), 1200);
      }

      function burstFromElement(el, status) {
        if (!el) return;
        const rect = el.getBoundingClientRect();
        const x = rect.left + rect.width / 2;
        const y = rect.top + rect.height / 2;
        const color = status === "completed"
          ? getComputedStyle(document.documentElement).getPropertyValue("--success").trim()
          : getComputedStyle(document.documentElement).getPropertyValue("--error").trim();
        emitParticles(x, y, color);
      }

      // ── Orb State ──

      function setOrbState(state) {
        document.body.classList.remove("orbs-processing", "orbs-failed", "orbs-completed");
        if (state) document.body.classList.add(`orbs-${state}`);
      }

      // ── ETA Calculation ──

      let etaStartTime = null;
      let etaStartRows = 0;

      function resetEta() { etaStartTime = null; etaStartRows = 0; }

      function computeEta(processed, total) {
        if (!total || processed <= 0) return null;
        if (!etaStartTime) { etaStartTime = Date.now(); etaStartRows = processed; return null; }
        const elapsed = (Date.now() - etaStartTime) / 1000;
        const rowsDone = processed - etaStartRows;
        if (rowsDone <= 0 || elapsed < 2) return null;
        const rowsPerSec = rowsDone / elapsed;
        const remaining = total - processed;
        const secsLeft = remaining / rowsPerSec;
        if (secsLeft < 1) return "< 1s";
        const m = Math.floor(secsLeft / 60);
        const s = Math.floor(secsLeft % 60);
        return m > 0 ? `~${m}m ${String(s).padStart(2,"0")}s` : `~${s}s`;
      }

      // Track previous metric values for animated counting + pulse
      let prevMetrics = {};

      function setStatus(el, message, isError = false, isSuccess = false) {
        if (el === submitStatusWrap) {
          submitStatusText.textContent = message;
          submitStatusWrap.className = "status status-inline" + (isError ? " error" : isSuccess ? " success" : "");
          spinner.classList.toggle("active", !isError && !isSuccess && message !== "");
          return;
        }
        el.textContent = message;
        el.className = "status" + (isError ? " error" : isSuccess ? " success" : "");
      }

      function stopSpinner() {
        spinner.classList.remove("active");
      }

      function setEnvBadgeState(configured) {
        if (configured) {
          envBadge.textContent = "Ready";
          envBadge.className = "badge ready";
          return;
        }
        envBadge.textContent = "Not loaded";
        envBadge.className = "badge pending";
      }

      function setLogStreamState(label, stateClass) {
        logStatusBadge.textContent = label;
        logStatusBadge.className = `status-pill ${stateClass}`;
      }

      function escapeHtml(value) {
        return String(value)
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;")
          .replaceAll("'", "&#39;");
      }

      function formatDuration(durationMs) {
        if (durationMs == null || Number.isNaN(durationMs)) return "--";
        const totalSeconds = Math.max(0, Math.floor(durationMs / 1000));
        const hours = Math.floor(totalSeconds / 3600);
        const minutes = Math.floor((totalSeconds % 3600) / 60);
        const seconds = totalSeconds % 60;
        if (hours > 0) {
          return `${hours}h ${String(minutes).padStart(2, "0")}m ${String(seconds).padStart(2, "0")}s`;
        }
        return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
      }

      function computeDurationMs(summary) {
        if (!summary) return null;
        if (summary.duration_ms != null) return summary.duration_ms;
        if (summary.started_at) {
          const started = Date.parse(summary.started_at);
          if (!Number.isNaN(started)) {
            return Date.now() - started;
          }
        }
        return null;
      }

      async function extractErrorMessage(resp, fallback) {
        let message = fallback;
        try {
          const payload = await resp.json();
          if (typeof payload?.detail === "string") {
            return payload.detail;
          }
          if (payload?.detail?.message) {
            return payload.detail.message;
          }
          if (Array.isArray(payload?.detail?.missing_keys)) {
            return `Missing keys: ${payload.detail.missing_keys.join(", ")}`;
          }
        } catch (_err) {
          return message;
        }
        return message;
      }

      async function fetchWithTimeout(url, options = {}, timeoutMs = 25000) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeoutMs);
        try {
          return await fetch(url, { ...options, signal: controller.signal });
        } finally {
          clearTimeout(timer);
        }
      }

      // ── Animated Counter ──

      function animateValue(el, from, to, duration = 400) {
        if (from === to) return;
        const start = performance.now();
        const step = (now) => {
          const progress = Math.min((now - start) / duration, 1);
          const eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
          el.textContent = Math.round(from + (to - from) * eased);
          if (progress < 1) requestAnimationFrame(step);
        };
        requestAnimationFrame(step);
      }

      // ── Empty State with Template Info ──

      function renderEmptyState() {
        prevMetrics = {};
        multiResults.innerHTML = `
          <div class="empty-state">
            <h3>Excel batch mode ready</h3>
            <p>Upload an .xlsx file with these columns:</p>
            <div class="empty-state-columns">
              <span class="empty-state-col">voiceover_text</span>
              <span class="empty-state-col">emotion</span>
              <span class="empty-state-col">activity_name</span>
              <span class="empty-state-col">voiceover_title</span>
            </div>
          </div>
        `;
      }

      // ── Metric pulse + animate helpers ──

      const METRIC_KEYS = [
        "total_rows", "rows_processed", "rows_succeeded", "rows_failed",
        "language_tasks_total", "language_tasks_succeeded",
        "placeholder_audio_generated", "uploads_succeeded",
        "filename_collisions_resolved"
      ];

      const PULSE_GREEN_KEYS = new Set(["rows_succeeded", "language_tasks_succeeded", "uploads_succeeded"]);
      const PULSE_RED_KEYS = new Set(["rows_failed"]);

      function applyMetricAnimations() {
        const isFirstRender = Object.keys(prevMetrics).length === 0;
        METRIC_KEYS.forEach((key, i) => {
          const el = document.querySelector(`[data-metric="${key}"] .value`);
          if (!el) return;
          const newVal = parseInt(el.dataset.to || "0", 10);
          const oldVal = prevMetrics[key] || 0;
          if (newVal !== oldVal) {
            const delay = isFirstRender ? i * 50 : 0;
            setTimeout(() => {
              animateValue(el, oldVal, newVal);
              const metricCard = el.closest(".metric");
              if (metricCard && newVal > oldVal && !isFirstRender) {
                if (PULSE_GREEN_KEYS.has(key)) {
                  metricCard.classList.remove("pulse-green");
                  void metricCard.offsetWidth;
                  metricCard.classList.add("pulse-green");
                } else if (PULSE_RED_KEYS.has(key)) {
                  metricCard.classList.remove("pulse-red");
                  void metricCard.offsetWidth;
                  metricCard.classList.add("pulse-red");
                }
              }
            }, delay);
            prevMetrics[key] = newVal;
          }
        });
      }

      // ── Batch Summary with Progress Bar ──

      function renderBatchSummary(summary, jobId, status, errorMessage = "") {
        const s = summary || {};
        const statusClass = status === "completed" ? "completed" : status === "failed" ? "failed" : "running";
        const duration = formatDuration(computeDurationMs(s));
        const failureBlock = errorMessage
          ? `<div class="status error" style="margin-top:8px;">${escapeHtml(errorMessage)}</div>`
          : "";

        const total = s.total_rows || 0;
        const succeeded = s.rows_succeeded || 0;
        const failed = s.rows_failed || 0;
        const remaining = Math.max(0, total - succeeded - failed);
        const pctSuccess = total ? ((succeeded / total) * 100).toFixed(1) : 0;
        const pctFail = total ? ((failed / total) * 100).toFixed(1) : 0;
        const processed = s.rows_processed || 0;

        const eta = computeEta(processed, total);
        const etaText = eta ? ` &middot; ${eta} left` : "";

        const progressBar = total > 0 ? `
          <div class="progress-bar-wrap">
            <div class="progress-bar">
              <div class="progress-bar-segment success" style="width:${pctSuccess}%"></div>
              <div class="progress-bar-segment failed" style="width:${pctFail}%"></div>
            </div>
            <div class="progress-bar-label">
              <span>${processed} / ${total} rows${etaText}</span>
              <span>${succeeded} OK &middot; ${failed} failed &middot; ${remaining} remaining</span>
            </div>
          </div>
        ` : "";

        multiResults.innerHTML = `
          <div class="summary-card" id="summaryCard">
            <div class="summary-top">
              <div>
                <div class="summary-title">Batch Job ${escapeHtml(jobId || "--")}</div>
                <div class="status" style="margin-top:4px;">Live execution summary</div>
              </div>
              <span class="status-pill ${statusClass}">${escapeHtml(status || "running")}</span>
            </div>
            ${progressBar}
            <div class="summary-grid">
              <div class="metric" data-metric="total_rows"><span class="label">Rows</span><span class="value" data-to="${s.total_rows ?? 0}">${prevMetrics.total_rows ?? 0}</span></div>
              <div class="metric" data-metric="rows_processed"><span class="label">Processed</span><span class="value" data-to="${s.rows_processed ?? 0}">${prevMetrics.rows_processed ?? 0}</span></div>
              <div class="metric" data-metric="rows_succeeded"><span class="label">Succeeded</span><span class="value" data-to="${s.rows_succeeded ?? 0}">${prevMetrics.rows_succeeded ?? 0}</span></div>
              <div class="metric" data-metric="rows_failed"><span class="label">Failed</span><span class="value" data-to="${s.rows_failed ?? 0}">${prevMetrics.rows_failed ?? 0}</span></div>
              <div class="metric" data-metric="language_tasks_total"><span class="label">Lang Tasks</span><span class="value" data-to="${s.language_tasks_total ?? 0}">${prevMetrics.language_tasks_total ?? 0}</span></div>
              <div class="metric" data-metric="language_tasks_succeeded"><span class="label">Tasks OK</span><span class="value" data-to="${s.language_tasks_succeeded ?? 0}">${prevMetrics.language_tasks_succeeded ?? 0}</span></div>
              <div class="metric" data-metric="placeholder_audio_generated"><span class="label">Placeholders</span><span class="value" data-to="${s.placeholder_audio_generated ?? 0}">${prevMetrics.placeholder_audio_generated ?? 0}</span></div>
              <div class="metric" data-metric="uploads_succeeded"><span class="label">Uploads OK</span><span class="value" data-to="${s.uploads_succeeded ?? 0}">${prevMetrics.uploads_succeeded ?? 0}</span></div>
              <div class="metric" data-metric="filename_collisions_resolved"><span class="label">Name Collisions</span><span class="value" data-to="${s.filename_collisions_resolved ?? 0}">${prevMetrics.filename_collisions_resolved ?? 0}</span></div>
              <div class="metric"><span class="label">Duration</span><span class="value">${duration}</span></div>
            </div>
            ${failureBlock}
          </div>
        `;

        // Trigger counter animations after DOM is painted
        requestAnimationFrame(() => applyMetricAnimations());
      }

      // ── Completion Flash ──

      function flashSummaryCard(status) {
        const card = document.getElementById("summaryCard");
        if (!card) return;
        const cls = status === "completed" ? "flash-success" : "flash-failed";
        card.classList.add(cls);
        setTimeout(() => card.classList.remove(cls), 1200);
      }

      // ── UI Lock ──

      let currentJobId = null;

      function lockUI() {
        form.classList.add("is-locked");
      }

      function unlockUI() {
        form.classList.remove("is-locked");
      }

      // ── Morphing Button ──

      function setMorphBtn(state, text) {
        const idle = submitBtn.querySelector(".morph-btn-idle");
        const loading = submitBtn.querySelector(".morph-btn-loading");
        const done = submitBtn.querySelector(".morph-btn-done");
        const failed = submitBtn.querySelector(".morph-btn-failed");
        const loadingText = submitBtn.querySelector(".morph-btn-loading-text");

        submitBtn.className = "morph-btn";
        idle.hidden = true;
        loading.hidden = true;
        done.hidden = true;
        failed.hidden = true;
        submitBtn.onclick = null;

        if (state === "idle") {
          idle.hidden = false;
          submitBtn.disabled = false;
          submitBtn.type = "submit";
          unlockUI();
        } else if (state === "loading") {
          loading.hidden = false;
          if (text) loadingText.textContent = text;
          submitBtn.classList.add("is-loading");
          submitBtn.disabled = false;
          submitBtn.type = "button";
          submitBtn.onclick = cancelCurrentJob;
          lockUI();
        } else if (state === "done") {
          done.hidden = false;
          submitBtn.classList.add("is-done");
          submitBtn.disabled = true;
          submitBtn.type = "button";
          setTimeout(() => setMorphBtn("idle"), 3000);
        } else if (state === "failed") {
          failed.hidden = false;
          submitBtn.classList.add("is-failed");
          submitBtn.disabled = true;
          submitBtn.type = "button";
          setTimeout(() => setMorphBtn("idle"), 3000);
        }
      }

      let isCancelling = false;

      async function cancelCurrentJob() {
        if (!currentJobId || isCancelling) return;
        isCancelling = true;
        setMorphBtn("loading", "Cancelling...");
        submitBtn.disabled = true;
        try {
          await fetchWithTimeout(`/batch/excel-jobs/${currentJobId}/cancel`, { method: "POST" });
          showToast("Cancel requested — finishing current row...", "info");
        } catch (_err) {
          // polling loop will pick up the final state regardless
        }
      }

      function startDurationTicker() {
        if (durationTicker) return;
        durationTicker = window.setInterval(() => {
          if (!latestJobState) return;
          if (latestJobState.status !== "running") return;
          renderBatchSummary(
            latestJobState.summary,
            latestJobState.job_id,
            latestJobState.status,
            latestJobState.error || ""
          );
        }, 1000);
      }

      function stopDurationTicker() {
        if (!durationTicker) return;
        clearInterval(durationTicker);
        durationTicker = null;
      }

      // ── Color-coded Log Lines ──

      function typewriterLine(span, text, speed = 8) {
        let i = 0;
        span.textContent = "";
        const tick = () => {
          const chunk = Math.min(i + 3, text.length);
          span.textContent = text.slice(0, chunk);
          i = chunk;
          if (i < text.length) requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
      }

      function appendLogLines(items) {
        for (const item of items) {
          const timestamp = item.timestamp ? new Date(item.timestamp).toLocaleTimeString() : "--:--:--";
          const level = item.level || "INFO";
          const logger = item.logger || "log";
          const message = item.message || "";
          const line = `[${timestamp}] ${level} ${logger} - ${message}`;
          logLines.push(line);

          const span = document.createElement("span");
          span.style.display = "block";
          if (level === "ERROR") span.className = "log-line-error";
          else if (level === "WARNING") span.className = "log-line-warning";

          logOutput.appendChild(span);
          typewriterLine(span, line);
        }
        if (logLines.length > 300) {
          logLines.splice(0, logLines.length - 300);
          // Trim oldest DOM nodes
          while (logOutput.children.length > 300) {
            logOutput.removeChild(logOutput.firstChild);
          }
        }
        logOutput.scrollTop = logOutput.scrollHeight;
      }

      async function primeLogCursor() {
        try {
          const resp = await fetchWithTimeout("/logs/important?since_id=0&limit=1");
          if (!resp.ok) return;
          const payload = await resp.json();
          if (typeof payload.latest_id === "number") {
            lastLogId = payload.latest_id;
          }
        } catch (_err) {}
      }

      async function fetchImportantLogs() {
        try {
          const resp = await fetchWithTimeout(`/logs/important?since_id=${lastLogId}&limit=200`);
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          const payload = await resp.json();
          const logs = Array.isArray(payload.logs) ? payload.logs : [];
          if (logs.length > 0) {
            appendLogLines(logs);
            logStatusText.textContent = `Streaming logs (${logs.length} new).`;
          } else if (logLines.length === 0) {
            logStatusText.textContent = "No important logs yet.";
          } else {
            logStatusText.textContent = "Listening for important logs...";
          }
          if (typeof payload.latest_id === "number") {
            lastLogId = Math.max(lastLogId, payload.latest_id);
          }
        } catch (error) {
          logStatusText.textContent = `Log stream issue: ${error.message || error}`;
        }
      }

      async function startLogPolling() {
        stopLogPolling(false);
        lastLogId = 0;
        logLines.length = 0;
        logOutput.textContent = "";
        setLogStreamState("live", "running");
        logStatusText.textContent = "Connecting log stream...";
        await primeLogCursor();
        await fetchImportantLogs();
        logPollTimer = window.setInterval(() => {
          void fetchImportantLogs();
        }, 2000);
      }

      function stopLogPolling(markFinal) {
        if (logPollTimer) {
          clearInterval(logPollTimer);
          logPollTimer = null;
        }
        if (!markFinal) {
          setLogStreamState("idle", "running");
          logStatusText.textContent = "Waiting for batch run...";
          return;
        }
        const finalStatus = latestJobState?.status || "idle";
        const finalClass = finalStatus === "completed" ? "completed" : finalStatus === "failed" ? "failed" : "running";
        setLogStreamState(finalStatus, finalClass);
      }

      // ── Config Health Dots ──

      const REQUIRED_ENV_KEYS = [
        "SARVAM_API_KEY", "GEMINI_API_KEY", "ELEVEN_LABS",
        "DESI_VOCAL_VOICE", "AI_STUDIO_VOICE",
        "AWS_ACCESS_KEY", "AWS_SECRET_KEY", "AWS_BUCKET", "AWS_REGION",
        "AWS_ENDPOINT_URL",
        "BATCH_ENABLE_S3_UPLOAD", "BATCH_ENABLE_QC",
        "GEMINI_QC_MODELS"
      ];

      function renderHealthDots(missingKeys) {
        const healthDots = document.getElementById("healthDots");
        if (!healthDots) return;

        const missingSet = new Set(missingKeys || []);
        const allOk = missingSet.size === 0;

        healthDots.innerHTML = "";
        healthDots.className = "health-dots" + (allOk ? " health-dots-ready" : "");

        for (const key of REQUIRED_ENV_KEYS) {
          const dot = document.createElement("div");
          dot.className = "health-dot " + (missingSet.has(key) ? "missing" : "ok");

          const tooltip = document.createElement("span");
          tooltip.className = "health-dot-tooltip";
          tooltip.textContent = key;
          dot.appendChild(tooltip);

          healthDots.appendChild(dot);
        }
      }

      function renderHealthDotsLoading() {
        const healthDots = document.getElementById("healthDots");
        if (!healthDots) return;
        healthDots.innerHTML = "";
        healthDots.className = "health-dots";
        for (let i = 0; i < REQUIRED_ENV_KEYS.length; i++) {
          const dot = document.createElement("div");
          dot.className = "health-dot loading";
          dot.style.animationDelay = `${i * 60}ms`;
          healthDots.appendChild(dot);
        }
      }

      async function refreshEnvStatus() {
        // Show skeleton while loading
        const envBox = document.getElementById("envStatusBox");
        if (envBox && !envBox.dataset.loaded) {
          envStatus.innerHTML = '<div class="skeleton skeleton-line" style="width:80%"></div><div class="skeleton skeleton-line" style="width:55%"></div>';
        }
        renderHealthDotsLoading();
        try {
          const resp = await fetch("/config/session-env/status");
          if (!resp.ok) {
            envConfigured = false;
            setEnvBadgeState(false);
            setStatus(envStatus, "Unable to read runtime config status.", true);
            renderHealthDots(REQUIRED_ENV_KEYS);
            return;
          }

          const payload = await resp.json();
          envConfigured = Boolean(payload.configured);
          setEnvBadgeState(envConfigured);
          if (envBox) envBox.dataset.loaded = "1";

          const missing = Array.isArray(payload.missing_keys) ? payload.missing_keys : [];
          renderHealthDots(missing);

          if (envConfigured) {
            setStatus(envStatus, "Runtime config ready.", false, true);
            return;
          }

          if (missing.length > 0) {
            setStatus(envStatus, `Missing keys: ${missing.join(", ")}`, true);
          } else {
            setStatus(envStatus, "Runtime config not ready.", true);
          }
        } catch (_err) {
          envConfigured = false;
          setEnvBadgeState(false);
          setStatus(envStatus, "Unable to read runtime config status.", true);
          renderHealthDots(REQUIRED_ENV_KEYS);
        }
      }

      async function pollBatchJob(jobId) {
        const terminalStates = new Set(["completed", "failed", "cancelled"]);
        let consecutiveErrors = 0;
        let consecutiveNotFound = 0;

        while (true) {
          try {
            const resp = await fetchWithTimeout(`/batch/excel-jobs/${jobId}`);

            if (resp.status === 404) {
              consecutiveNotFound += 1;
              const delaySeconds = Math.min(10, 2 ** Math.min(consecutiveNotFound, 3));
              if (consecutiveNotFound >= 3) {
                const missing = {
                  job_id: jobId,
                  status: "failed",
                  error: `Job ${jobId} was not found repeatedly. Server may have restarted.`,
                  summary: null,
                };
                latestJobState = missing;
                return missing;
              }
              setStatus(
                submitStatusWrap,
                `Polling issue: job not found (${consecutiveNotFound}/3). Retrying in ${delaySeconds}s...`
              );
              await new Promise((resolve) => setTimeout(resolve, delaySeconds * 1000));
              continue;
            }

            if (!resp.ok) throw new Error(`Failed to fetch job status (HTTP ${resp.status})`);
            const payload = await resp.json();
            latestJobState = payload;
            consecutiveErrors = 0;
            consecutiveNotFound = 0;

            renderBatchSummary(payload.summary, payload.job_id, payload.status, payload.error || "");
            updateRowFeed(payload.summary);
            recordJob(payload.job_id, payload.status, payload.summary);

            // Update morphing button with progress (skip if cancelling)
            if (!isCancelling) {
              const s = payload.summary || {};
              const processed = s.rows_processed || 0;
              const total = s.total_rows || 0;
              if (total > 0) {
                setMorphBtn("loading", `${processed}/${total} rows \u2022 click to cancel`);
              }
            }

            setStatus(submitStatusWrap, `Running batch... (${payload.status})`);
            if (terminalStates.has(payload.status)) return payload;
            await new Promise((resolve) => setTimeout(resolve, 2000));
          } catch (error) {
            consecutiveErrors += 1;
            const backoffSeconds = Math.min(30, 2 ** Math.min(consecutiveErrors, 5));
            const message =
              error?.name === "AbortError"
                ? "status request timed out"
                : error?.message || "unknown polling error";
            setStatus(submitStatusWrap, `Polling issue: ${message}. Retrying in ${backoffSeconds}s...`);
            await new Promise((resolve) => setTimeout(resolve, backoffSeconds * 1000));
          }
        }
      }

      // ── Drop Zone Logic ──

      function showDropFile(file) {
        dropZoneContent.hidden = true;
        dropZoneFile.hidden = false;
        dropFileName.textContent = file.name;
        dropFileSize.textContent = `(${(file.size / 1048576).toFixed(2)} MB)`;
        dropZone.classList.add("has-file");
        setStatus(excelStatus, `${file.name} (${(file.size / 1048576).toFixed(2)} MB)`, false, true);

        // Scan animation
        const existing = dropZoneFile.querySelector(".drop-zone-scan");
        if (existing) existing.remove();
        const scan = document.createElement("div");
        scan.className = "drop-zone-scan";
        dropZoneFile.appendChild(scan);
        scan.addEventListener("animationend", () => scan.remove());
      }

      function clearDropFile() {
        excelFile.value = "";
        dropZoneContent.hidden = false;
        dropZoneFile.hidden = true;
        dropFileName.textContent = "";
        dropFileSize.textContent = "";
        dropZone.classList.remove("has-file");
        setStatus(excelStatus, "");
        if (excelPreview) { excelPreview.hidden = true; excelPreview.innerHTML = ""; }
      }

      function handleDroppedFile(file) {
        if (!file.name.toLowerCase().endsWith(".xlsx")) {
          setStatus(excelStatus, "Only .xlsx files are allowed.", true);
          return;
        }
        const dt = new DataTransfer();
        dt.items.add(file);
        excelFile.files = dt.files;
        showDropFile(file);
        loadExcelPreview(file);
      }

      dropZone.addEventListener("dragover", (e) => {
        e.preventDefault();
        dropZone.classList.add("drag-over");
      });

      dropZone.addEventListener("dragleave", () => {
        dropZone.classList.remove("drag-over");
      });

      dropZone.addEventListener("drop", (e) => {
        e.preventDefault();
        dropZone.classList.remove("drag-over");
        const file = e.dataTransfer.files[0];
        if (file) handleDroppedFile(file);
      });

      dropClearBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        clearDropFile();
      });

      excelFile.addEventListener("change", () => {
        const file = excelFile.files[0];
        if (!file) {
          clearDropFile();
          return;
        }
        if (!file.name.toLowerCase().endsWith(".xlsx")) {
          setStatus(excelStatus, "Only .xlsx files are allowed.", true);
          excelFile.value = "";
          return;
        }
        showDropFile(file);
        loadExcelPreview(file);
      });

      // ── Language Bulk Actions ──

      const INDIAN_LANG_VALUES = new Set([
        "bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN",
        "ml-IN", "mr-IN", "od-IN", "pa-IN", "ta-IN", "te-IN"
      ]);

      function setLanguageChecks(filter) {
        document.querySelectorAll('input[name="target_language"]').forEach((cb) => {
          if (filter === "all") cb.checked = true;
          else if (filter === "none") cb.checked = false;
          else if (filter === "indian") cb.checked = INDIAN_LANG_VALUES.has(cb.value);
          else if (filter === "intl") cb.checked = !INDIAN_LANG_VALUES.has(cb.value);
        });
      }

      document.getElementById("selectAll").addEventListener("click", () => setLanguageChecks("all"));
      document.getElementById("selectIndian").addEventListener("click", () => setLanguageChecks("indian"));
      document.getElementById("selectIntl").addEventListener("click", () => setLanguageChecks("intl"));
      document.getElementById("selectNone").addEventListener("click", () => setLanguageChecks("none"));

      // ── Teaching Mode Status Text ──

      const teachingModeInput = document.getElementById("teachingMode");
      const teachingStatusText = document.querySelector(".teaching-status-text");
      teachingModeInput.addEventListener("change", () => {
        teachingStatusText.textContent = teachingModeInput.checked ? "ON" : "OFF";
      });

      // ── Form Submit ──

      form.addEventListener("submit", async (event) => {
        event.preventDefault();

        const selectedLanguages = Array.from(
          document.querySelectorAll('input[name="target_language"]:checked')
        ).map((checkbox) => checkbox.value);

        if (selectedLanguages.length === 0) {
          setStatus(submitStatusWrap, "Please select at least one target language.", true);
          return;
        }

        if (!envConfigured) {
          setStatus(submitStatusWrap, "Runtime config is not ready.", true);
          return;
        }

        const file = excelFile.files[0];
        if (!file) {
          setStatus(submitStatusWrap, "Please upload an .xlsx file.", true);
          return;
        }
        if (!file.name.toLowerCase().endsWith(".xlsx")) {
          setStatus(submitStatusWrap, "Only .xlsx files are allowed.", true);
          return;
        }

        currentJobId = null;
        isCancelling = false;
        resetEta();
        clearRowFeed();
        setOrbState("processing");
        setMorphBtn("loading", "Starting...");
        setStatus(submitStatusWrap, "Creating batch job...");
        result.hidden = true;
        result.textContent = "";
        renderEmptyState();
        latestJobState = null;
        stopDurationTicker();

        try {
          await startLogPolling();

          const formData = new FormData();
          formData.append("file", file);
          formData.append("max_language_parallelism", "3");
          const isTeachingMode = document.getElementById("teachingMode").checked;
          if (isTeachingMode) {
              formData.append("teaching_mode", "true");
          }
          selectedLanguages.forEach((language) => formData.append("target_languages", language));

          const createResp = await fetch("/batch/excel-jobs", {
            method: "POST",
            body: formData,
          });

          if (!createResp.ok) {
            const msg = await extractErrorMessage(createResp, "Failed to create batch job.");
            setStatus(submitStatusWrap, msg, true);
            stopLogPolling(false);
            setMorphBtn("failed");
            return;
          }

          const createPayload = await createResp.json();
          const jobId = createPayload.job_id;
          currentJobId = jobId;
          latestJobState = {
            job_id: jobId,
            status: "running",
            summary: { started_at: new Date().toISOString() },
            error: null,
          };
          renderBatchSummary(latestJobState.summary, jobId, "running");
          startDurationTicker();
          setMorphBtn("loading", "Processing...");
          setStatus(submitStatusWrap, `Job ${jobId} started.`);
          showToast(`Job ${jobId.slice(0,8)}... started`, "info");
          pulseBrandIcon();
          setProgressShimmer(true);

          const final = await pollBatchJob(jobId);
          latestJobState = final;
          stopDurationTicker();
          await fetchImportantLogs();
          stopLogPolling(true);
          stopSpinner();

          renderBatchSummary(final.summary, final.job_id || jobId, final.status, final.error || "");
          flashSummaryCard(final.status);
          recordJob(final.job_id || jobId, final.status, final.summary);

          currentJobId = null;
          isCancelling = false;
          setProgressShimmer(false);
          if (final.status === "completed") {
            setOrbState("completed");
            setMorphBtn("done");
            setStatus(submitStatusWrap, `Job ${jobId} completed.`, false, true);
            showToast("Batch job completed successfully", "success");
            playSuccessSound();
            burstFromElement(document.getElementById("summaryCard"), "completed");
          } else {
            setOrbState("failed");
            setMorphBtn("failed");
            setStatus(submitStatusWrap, final.error || `Job ${jobId} failed.`, true);
            showToast(final.error || "Batch job failed", "error");
            playFailSound();
            burstFromElement(document.getElementById("summaryCard"), "failed");
          }
        } catch (error) {
          latestJobState = {
            job_id: latestJobState?.job_id || "--",
            status: "failed",
            summary: latestJobState?.summary || null,
            error: error.message || String(error),
          };
          stopDurationTicker();
          stopLogPolling(true);
          stopSpinner();
          renderBatchSummary(
            latestJobState.summary,
            latestJobState.job_id,
            latestJobState.status,
            latestJobState.error
          );
          flashSummaryCard("failed");
          currentJobId = null;
          isCancelling = false;
          setProgressShimmer(false);
          setOrbState("failed");
          setMorphBtn("failed");
          setStatus(submitStatusWrap, error.message || "Request failed.", true);
          showToast(error.message || "Request failed", "error");
          playFailSound();
        }
      });

      // ── Language Chip Ripple ──

      document.querySelectorAll(".lang-chip label").forEach((label) => {
        label.addEventListener("click", (e) => {
          const rect = label.getBoundingClientRect();
          const ripple = document.createElement("span");
          ripple.className = "chip-ripple";
          const size = Math.max(rect.width, rect.height) * 2;
          ripple.style.width = ripple.style.height = `${size}px`;
          ripple.style.left = `${e.clientX - rect.left - size / 2}px`;
          ripple.style.top = `${e.clientY - rect.top - size / 2}px`;
          label.appendChild(ripple);
          ripple.addEventListener("animationend", () => ripple.remove());
        });
      });

      // ── Tab Underline Slide ──

      function updateTabUnderline() {
        const tabsContainer = document.querySelector(".output-tabs");
        const activeTab = tabsContainer?.querySelector(".output-tab.active");
        if (!tabsContainer || !activeTab) return;
        const containerRect = tabsContainer.getBoundingClientRect();
        const tabRect = activeTab.getBoundingClientRect();
        tabsContainer.style.setProperty("--tab-left", `${tabRect.left - containerRect.left}px`);
        tabsContainer.style.setProperty("--tab-width", `${tabRect.width}px`);
      }

      // Patch the output-tabs ::after to use CSS variables
      {
        const style = document.createElement("style");
        style.textContent = `.output-tabs::after { left: var(--tab-left, 0px); width: var(--tab-width, 60px); }`;
        document.head.appendChild(style);
      }

      // ── Brand Icon Pulse ──

      function pulseBrandIcon() {
        const icon = document.querySelector(".brand-icon");
        if (!icon) return;
        icon.classList.remove("pulse");
        void icon.offsetWidth;
        icon.classList.add("pulse");
      }

      // ── Progress Bar Shimmer Control ──

      function setProgressShimmer(active) {
        document.querySelectorAll(".progress-bar").forEach((bar) => {
          bar.classList.toggle("is-active", active);
        });
      }

      // ── Collapsible Panels ──

      document.querySelectorAll(".panel-head[data-panel]").forEach((head) => {
        head.addEventListener("dblclick", () => {
          const panel = document.getElementById(head.dataset.panel);
          if (panel) panel.classList.toggle("collapsed");
        });
      });

      // ── Resizable Panels ──

      function initResize(handleId, leftPanelId, rightPanelId) {
        const handle = document.getElementById(handleId);
        if (!handle) return;

        let startX, startLeftW, startRightW;

        handle.addEventListener("mousedown", (e) => {
          e.preventDefault();
          const left = document.getElementById(leftPanelId);
          const right = document.getElementById(rightPanelId);
          if (!left || !right) return;

          startX = e.clientX;
          startLeftW = left.offsetWidth;
          startRightW = right.offsetWidth;
          handle.classList.add("dragging");

          const onMove = (e) => {
            const dx = e.clientX - startX;
            const totalW = startLeftW + startRightW;
            const newLeft = Math.max(120, Math.min(totalW - 120, startLeftW + dx));
            const newRight = totalW - newLeft;
            const ws = form;
            const cols = getComputedStyle(ws).gridTemplateColumns.split(" ");
            if (handleId === "resizeHandle1") {
              cols[0] = newLeft + "px";
              cols[2] = newRight + "px";
            } else {
              cols[2] = newLeft + "px";
              cols[4] = newRight + "px";
            }
            ws.style.gridTemplateColumns = cols.join(" ");
          };

          const onUp = () => {
            handle.classList.remove("dragging");
            document.removeEventListener("mousemove", onMove);
            document.removeEventListener("mouseup", onUp);
          };

          document.addEventListener("mousemove", onMove);
          document.addEventListener("mouseup", onUp);
        });
      }

      initResize("resizeHandle1", "panel1", "panel2");
      initResize("resizeHandle2", "panel2", "panel3");

      // ── Output Tabs ──

      const outputTabs = document.querySelectorAll(".output-tab");
      const tabContents = {
        summary: document.getElementById("tabSummary"),
        feed: document.getElementById("tabFeed"),
      };

      function switchTab(name) {
        outputTabs.forEach(t => t.classList.toggle("active", t.dataset.tab === name));
        Object.entries(tabContents).forEach(([key, el]) => {
          if (el) el.hidden = key !== name;
        });
        requestAnimationFrame(updateTabUnderline);
      }

      document.querySelector(".output-tabs")?.addEventListener("click", (e) => {
        const tab = e.target.closest(".output-tab");
        if (tab) switchTab(tab.dataset.tab);
      });

      // ── Row-by-row Live Feed ──

      const rowFeed = document.getElementById("rowFeed");
      const rowFeedState = {};

      function updateRowFeed(summary) {
        if (!summary || !rowFeed) return;
        const processed = summary.rows_processed || 0;
        const total = summary.total_rows || 0;

        // Create items up to processed count
        for (let i = 1; i <= Math.min(processed + 1, total); i++) {
          const id = `row-feed-${i}`;
          if (rowFeedState[id]) continue;
          rowFeedState[id] = true;

          const item = document.createElement("div");
          item.className = "row-feed-item";
          item.id = id;

          const stage = i <= processed ? "done" : "translating";
          const stageLabel = i <= processed ? "Done" : "Processing";

          item.innerHTML = `
            <span class="row-feed-idx">Row ${i}</span>
            <span class="row-feed-text">Processing...</span>
            <span class="row-feed-stage ${stage}">${stageLabel}</span>
          `;
          rowFeed.appendChild(item);
          rowFeed.scrollTop = rowFeed.scrollHeight;
        }
      }

      function clearRowFeed() {
        if (rowFeed) rowFeed.innerHTML = "";
        Object.keys(rowFeedState).forEach(k => delete rowFeedState[k]);
      }

      // ── Excel Preview ──

      const excelPreview = document.getElementById("excelPreview");

      async function loadExcelPreview(file) {
        if (!excelPreview) return;
        excelPreview.hidden = true;
        excelPreview.innerHTML = "";

        const fd = new FormData();
        fd.append("file", file);

        try {
          const resp = await fetchWithTimeout("/batch/preview-excel", { method: "POST", body: fd }, 10000);
          if (!resp.ok) return;
          const data = await resp.json();
          const rows = data.rows || [];
          if (rows.length < 2) return;

          const headers = rows[0];
          const dataRows = rows.slice(1);

          let html = "<table><thead><tr>";
          headers.forEach(h => html += `<th>${escapeHtml(h)}</th>`);
          html += "</tr></thead><tbody>";
          dataRows.forEach(row => {
            html += "<tr>";
            row.forEach(c => html += `<td>${escapeHtml(c)}</td>`);
            html += "</tr>";
          });
          html += "</tbody></table>";

          excelPreview.innerHTML = html;
          excelPreview.hidden = false;
        } catch (_) {}
      }

      // ── Job History ──

      const historySidebar = document.getElementById("historySidebar");
      const historyList = document.getElementById("historyList");
      const historyBtn = document.getElementById("historyBtn");
      const historyClose = document.getElementById("historyClose");
      const jobHistory = [];

      function recordJob(jobId, status, summary) {
        const existing = jobHistory.find(j => j.id === jobId);
        if (existing) {
          existing.status = status;
          existing.summary = summary;
        } else {
          jobHistory.unshift({ id: jobId, status, summary, time: new Date().toLocaleTimeString() });
        }
        renderHistory();
      }

      function renderHistory() {
        if (!historyList) return;
        if (jobHistory.length === 0) {
          historyList.innerHTML = '<div class="empty-state" style="padding:16px;border:none;background:none;"><p>No jobs run this session.</p></div>';
          return;
        }

        historyList.innerHTML = jobHistory.map(j => {
          const s = j.summary || {};
          const statusClass = j.status === "completed" ? "completed" : j.status === "failed" ? "failed" : "running";
          return `
            <div class="history-item" data-job-id="${escapeHtml(j.id)}">
              <div class="history-item-top">
                <span class="history-item-id">${escapeHtml(j.id.slice(0, 12))}...</span>
                <span class="status-pill ${statusClass}">${escapeHtml(j.status)}</span>
              </div>
              <div class="history-item-meta">${j.time} &middot; ${s.rows_succeeded || 0}/${s.total_rows || 0} rows OK</div>
            </div>
          `;
        }).join("");

        historyList.querySelectorAll(".history-item").forEach(item => {
          item.addEventListener("click", () => {
            const job = jobHistory.find(j => j.id === item.dataset.jobId);
            if (job && job.summary) {
              renderBatchSummary(job.summary, job.id, job.status, "");
              switchTab("summary");
              historySidebar.hidden = true;
            }
          });
        });
      }

      historyBtn?.addEventListener("click", () => { historySidebar.hidden = !historySidebar.hidden; });
      historyClose?.addEventListener("click", () => { historySidebar.hidden = true; });

      // ── Command Palette ──

      const cmdOverlay = document.getElementById("cmdOverlay");
      const cmdInput = document.getElementById("cmdInput");
      const cmdResults = document.getElementById("cmdResults");

      const CMD_ACTIONS = [
        { label: "Run Pipeline", hint: "Start batch job", kbd: "Cmd+Enter", action: () => { form.requestSubmit(); } },
        { label: "Cancel Job", hint: "Stop current job", kbd: "Esc", action: () => { cancelCurrentJob(); } },
        { label: "Select All Languages", hint: "Check all language chips", kbd: "Cmd+A", action: () => { setLanguageChecks("all"); } },
        { label: "Select Indian Languages", hint: "Indian languages only", action: () => { setLanguageChecks("indian"); } },
        { label: "Select International", hint: "International languages only", action: () => { setLanguageChecks("intl"); } },
        { label: "Clear Languages", hint: "Uncheck all", kbd: "Cmd+Shift+A", action: () => { setLanguageChecks("none"); } },
        { label: "Upload File", hint: "Open file picker", kbd: "Cmd+K", action: () => { excelFile.click(); } },
        { label: "Toggle Teaching Mode", hint: "Mix English + Native", action: () => { teachingModeInput.checked = !teachingModeInput.checked; teachingModeInput.dispatchEvent(new Event("change")); } },
        { label: "Show Shortcuts", hint: "Keyboard shortcuts help", kbd: "?", action: () => { shortcutOverlay.hidden = false; } },
        { label: "Theme: Charcoal", hint: "Dark teal palette", action: () => applyTheme("charcoal", localStorage.getItem("autodub-style") || "glass") },
        { label: "Theme: Midnight Indigo", hint: "Purple palette", action: () => applyTheme("indigo", localStorage.getItem("autodub-style") || "glass") },
        { label: "Theme: Warm Obsidian", hint: "Amber/gold palette", action: () => applyTheme("obsidian", localStorage.getItem("autodub-style") || "glass") },
        { label: "Theme: Forest Noir", hint: "Emerald palette", action: () => applyTheme("forest", localStorage.getItem("autodub-style") || "glass") },
        { label: "Theme: Arctic Steel", hint: "Ice blue palette", action: () => applyTheme("arctic", localStorage.getItem("autodub-style") || "glass") },
        { label: "Theme: Blood Moon", hint: "Rose/crimson palette", action: () => applyTheme("blood", localStorage.getItem("autodub-style") || "glass") },
        { label: "Style: Glass", action: () => applyTheme(localStorage.getItem("autodub-palette") || "charcoal", "glass") },
        { label: "Style: Solid", action: () => applyTheme(localStorage.getItem("autodub-palette") || "charcoal", "solid") },
        { label: "Style: Neon", action: () => applyTheme(localStorage.getItem("autodub-palette") || "charcoal", "neon") },
        { label: "Style: Soft", action: () => applyTheme(localStorage.getItem("autodub-palette") || "charcoal", "soft") },
        { label: "Style: Brutalist", action: () => applyTheme(localStorage.getItem("autodub-palette") || "charcoal", "brutalist") },
        { label: "Job History", hint: "View past jobs", action: () => { historySidebar.hidden = !historySidebar.hidden; } },
      ];

      let cmdSelected = 0;

      function openCmdPalette() {
        cmdOverlay.hidden = false;
        cmdInput.value = "";
        cmdSelected = 0;
        renderCmdResults("");
        requestAnimationFrame(() => cmdInput.focus());
      }

      function closeCmdPalette() {
        cmdOverlay.hidden = true;
        cmdInput.value = "";
      }

      function renderCmdResults(query) {
        const q = query.toLowerCase().trim();
        const filtered = q
          ? CMD_ACTIONS.filter(a => a.label.toLowerCase().includes(q) || (a.hint || "").toLowerCase().includes(q))
          : CMD_ACTIONS;

        cmdSelected = Math.min(cmdSelected, Math.max(0, filtered.length - 1));

        cmdResults.innerHTML = filtered.map((a, i) => `
          <div class="cmd-item${i === cmdSelected ? " selected" : ""}" data-idx="${i}">
            <span class="cmd-item-label">${escapeHtml(a.label)}</span>
            ${a.hint ? `<span class="cmd-item-hint">${escapeHtml(a.hint)}</span>` : ""}
            ${a.kbd ? `<span class="cmd-item-kbd">${escapeHtml(a.kbd)}</span>` : ""}
          </div>
        `).join("");

        cmdResults.querySelectorAll(".cmd-item").forEach((el, i) => {
          el.addEventListener("click", () => {
            filtered[i].action();
            closeCmdPalette();
          });
        });

        return filtered;
      }

      cmdInput?.addEventListener("input", () => {
        cmdSelected = 0;
        renderCmdResults(cmdInput.value);
      });

      cmdInput?.addEventListener("keydown", (e) => {
        const q = cmdInput.value.toLowerCase().trim();
        const filtered = q
          ? CMD_ACTIONS.filter(a => a.label.toLowerCase().includes(q) || (a.hint || "").toLowerCase().includes(q))
          : CMD_ACTIONS;

        if (e.key === "ArrowDown") { e.preventDefault(); cmdSelected = Math.min(cmdSelected + 1, filtered.length - 1); renderCmdResults(cmdInput.value); }
        else if (e.key === "ArrowUp") { e.preventDefault(); cmdSelected = Math.max(cmdSelected - 1, 0); renderCmdResults(cmdInput.value); }
        else if (e.key === "Enter" && filtered[cmdSelected]) { e.preventDefault(); filtered[cmdSelected].action(); closeCmdPalette(); }
        else if (e.key === "Escape") { closeCmdPalette(); }
      });

      cmdOverlay?.addEventListener("click", (e) => {
        if (e.target === cmdOverlay) closeCmdPalette();
      });

      // ── Keyboard Shortcuts ──

      const shortcutOverlay = document.getElementById("shortcutOverlay");

      document.addEventListener("keydown", (e) => {
        const tag = (e.target.tagName || "").toLowerCase();
        const inInput = tag === "input" || tag === "textarea" || tag === "select";

        // ? key toggles help (only when not typing)
        if (e.key === "?" && !inInput) {
          e.preventDefault();
          shortcutOverlay.hidden = !shortcutOverlay.hidden;
          return;
        }

        // Escape: close overlay / cancel job / close theme panel
        if (e.key === "Escape") {
          if (!shortcutOverlay.hidden) { shortcutOverlay.hidden = true; return; }
          if (!themePanel.hidden) { themePanel.hidden = true; return; }
          if (currentJobId) { cancelCurrentJob(); return; }
          return;
        }

        // Cmd/Ctrl + Enter: submit form
        if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
          e.preventDefault();
          if (!submitBtn.disabled || submitBtn.type === "button") {
            submitBtn.click();
          } else {
            form.requestSubmit();
          }
          return;
        }

        // Cmd/Ctrl + K: open command palette
        if ((e.metaKey || e.ctrlKey) && e.key === "k") {
          e.preventDefault();
          if (cmdOverlay.hidden) openCmdPalette(); else closeCmdPalette();
          return;
        }

        // Cmd/Ctrl + A: select all languages (only when not typing)
        if ((e.metaKey || e.ctrlKey) && e.key === "a" && !inInput) {
          e.preventDefault();
          if (e.shiftKey) {
            setLanguageChecks("none");
          } else {
            setLanguageChecks("all");
          }
          return;
        }
      });

      // Click outside shortcut overlay to close
      shortcutOverlay.addEventListener("click", (e) => {
        if (e.target === shortcutOverlay) shortcutOverlay.hidden = true;
      });

      // ── Theme Switcher ──

      const PALETTES = ["charcoal", "indigo", "obsidian", "forest", "arctic", "blood"];
      const STYLES = ["glass", "solid", "neon", "soft", "brutalist"];

      function applyTheme(palette, style) {
        // Remove old palette/style classes
        document.body.className = document.body.className
          .split(" ")
          .filter(c => !c.startsWith("palette-") && !c.startsWith("style-"))
          .join(" ");

        document.body.classList.add(`palette-${palette}`);
        if (style !== "glass") {
          document.body.classList.add(`style-${style}`);
        }

        // Update active states in picker
        document.querySelectorAll(".theme-swatch").forEach(s => {
          s.classList.toggle("active", s.dataset.palette === palette);
        });
        document.querySelectorAll(".theme-style-btn").forEach(s => {
          s.classList.toggle("active", s.dataset.style === style);
        });

        // Persist
        localStorage.setItem("autodub-palette", palette);
        localStorage.setItem("autodub-style", style);
      }

      // FAB toggle
      const themeFab = document.getElementById("themeFab");
      const themePanel = document.getElementById("themePanel");
      const themePanelClose = document.getElementById("themePanelClose");

      themeFab.addEventListener("click", () => {
        themePanel.hidden = !themePanel.hidden;
      });

      themePanelClose.addEventListener("click", () => {
        themePanel.hidden = true;
      });

      // Palette picker
      document.getElementById("palettePicker").addEventListener("click", (e) => {
        const swatch = e.target.closest("[data-palette]");
        if (!swatch) return;
        const currentStyle = localStorage.getItem("autodub-style") || "glass";
        applyTheme(swatch.dataset.palette, currentStyle);
      });

      // Style picker
      document.getElementById("stylePicker").addEventListener("click", (e) => {
        const btn = e.target.closest("[data-style]");
        if (!btn) return;
        const currentPalette = localStorage.getItem("autodub-palette") || "charcoal";
        applyTheme(currentPalette, btn.dataset.style);
      });

      // ── Init ──

      window.addEventListener("DOMContentLoaded", () => {
        renderEmptyState();
        setLogStreamState("idle", "running");
        logStatusText.textContent = "Waiting for batch run...";
        setOrbState(null);
        refreshEnvStatus();
        requestAnimationFrame(updateTabUnderline);

        // Restore saved theme
        const savedPalette = localStorage.getItem("autodub-palette") || "charcoal";
        const savedStyle = localStorage.getItem("autodub-style") || "glass";
        applyTheme(savedPalette, savedStyle);
      });
