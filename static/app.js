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

      let envConfigured = false;
      let latestJobState = null;
      let durationTicker = null;
      let logPollTimer = null;
      let lastLogId = 0;
      const logLines = [];

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

      function renderEmptyState() {
        multiResults.innerHTML = `
          <div class="empty-state">
            <h3>Excel batch mode ready</h3>
            <p>Select languages, upload an .xlsx file, and run pipeline.</p>
          </div>
        `;
      }

      function renderBatchSummary(summary, jobId, status, errorMessage = "") {
        const s = summary || {};
        const statusClass = status === "completed" ? "completed" : status === "failed" ? "failed" : "running";
        const duration = formatDuration(computeDurationMs(s));
        const failureBlock = errorMessage
          ? `<div class="status error" style="margin-top:8px;">${escapeHtml(errorMessage)}</div>`
          : "";

        multiResults.innerHTML = `
          <div class="summary-card">
            <div class="summary-top">
              <div>
                <div class="summary-title">Batch Job ${escapeHtml(jobId || "--")}</div>
                <div class="status" style="margin-top:4px;">Live execution summary</div>
              </div>
              <span class="status-pill ${statusClass}">${escapeHtml(status || "running")}</span>
            </div>
            <div class="summary-grid">
              <div class="metric"><span class="label">Rows</span><span class="value">${s.total_rows ?? 0}</span></div>
              <div class="metric"><span class="label">Processed</span><span class="value">${s.rows_processed ?? 0}</span></div>
              <div class="metric"><span class="label">Succeeded</span><span class="value">${s.rows_succeeded ?? 0}</span></div>
              <div class="metric"><span class="label">Failed</span><span class="value">${s.rows_failed ?? 0}</span></div>
              <div class="metric"><span class="label">Lang Tasks</span><span class="value">${s.language_tasks_total ?? 0}</span></div>
              <div class="metric"><span class="label">Tasks OK</span><span class="value">${s.language_tasks_succeeded ?? 0}</span></div>
              <div class="metric"><span class="label">Placeholders</span><span class="value">${s.placeholder_audio_generated ?? 0}</span></div>
              <div class="metric"><span class="label">Uploads OK</span><span class="value">${s.uploads_succeeded ?? 0}</span></div>
              <div class="metric"><span class="label">Name Collisions</span><span class="value">${s.filename_collisions_resolved ?? 0}</span></div>
              <div class="metric"><span class="label">Duration</span><span class="value">${duration}</span></div>
            </div>
            ${failureBlock}
          </div>
        `;
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

      function appendLogLines(items) {
        for (const item of items) {
          const timestamp = item.timestamp ? new Date(item.timestamp).toLocaleTimeString() : "--:--:--";
          const level = item.level || "INFO";
          const logger = item.logger || "log";
          const message = item.message || "";
          logLines.push(`[${timestamp}] ${level} ${logger} - ${message}`);
        }
        if (logLines.length > 300) {
          logLines.splice(0, logLines.length - 300);
        }
        logOutput.textContent = logLines.join("\n");
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
        } catch (_err) {
          // ignore cursor prime issues; normal polling still handles updates
        }
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

      async function refreshEnvStatus() {
        try {
          const resp = await fetch("/config/session-env/status");
          if (!resp.ok) {
            envConfigured = false;
            setEnvBadgeState(false);
            setStatus(envStatus, "Unable to read runtime config status.", true);
            return;
          }

          const payload = await resp.json();
          envConfigured = Boolean(payload.configured);
          setEnvBadgeState(envConfigured);

          if (envConfigured) {
            setStatus(envStatus, "Runtime config ready.", false, true);
            return;
          }

          const missing = Array.isArray(payload.missing_keys) ? payload.missing_keys : [];
          if (missing.length > 0) {
            setStatus(envStatus, `Missing keys: ${missing.join(", ")}`, true);
          } else {
            setStatus(envStatus, "Runtime config not ready.", true);
          }
        } catch (_err) {
          envConfigured = false;
          setEnvBadgeState(false);
          setStatus(envStatus, "Unable to read runtime config status.", true);
        }
      }

      async function pollBatchJob(jobId) {
        const terminalStates = new Set(["completed", "failed"]);
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

        submitBtn.disabled = true;
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
            stopSpinner();
            return;
          }

          const createPayload = await createResp.json();
          const jobId = createPayload.job_id;
          latestJobState = {
            job_id: jobId,
            status: "running",
            summary: { started_at: new Date().toISOString() },
            error: null,
          };
          renderBatchSummary(latestJobState.summary, jobId, "running");
          startDurationTicker();
          setStatus(submitStatusWrap, `Job ${jobId} started.`);

          const final = await pollBatchJob(jobId);
          latestJobState = final;
          stopDurationTicker();
          await fetchImportantLogs();
          stopLogPolling(true);
          stopSpinner();

          renderBatchSummary(final.summary, final.job_id || jobId, final.status, final.error || "");
          if (final.status === "completed") {
            setStatus(submitStatusWrap, `Job ${jobId} completed.`, false, true);
          } else {
            setStatus(submitStatusWrap, final.error || `Job ${jobId} failed.`, true);
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
          setStatus(submitStatusWrap, error.message || "Request failed.", true);
        } finally {
          submitBtn.disabled = false;
        }
      });

      excelFile.addEventListener("change", () => {
        const file = excelFile.files[0];
        if (!file) {
          setStatus(excelStatus, "");
          return;
        }
        if (!file.name.toLowerCase().endsWith(".xlsx")) {
          setStatus(excelStatus, "Only .xlsx files are allowed.", true);
          excelFile.value = "";
          return;
        }
        setStatus(excelStatus, `${file.name} (${(file.size / 1048576).toFixed(2)} MB)`, false, true);
      });

      window.addEventListener("DOMContentLoaded", () => {
        renderEmptyState();
        setLogStreamState("idle", "running");
        logStatusText.textContent = "Waiting for batch run...";
        refreshEnvStatus();
      });
