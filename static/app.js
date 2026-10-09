// ZweiGeeks web UI: starts a job, streams its websocket events into the
// activity log and the pixel office (office.js), then shows findings, fix
// attempts and the diff in the report dialog.
//
// Everything that comes from the server (file paths, model output) is put
// on the page with textContent, never innerHTML: finding titles and details
// are LLM output about untrusted repositories.
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const office = window.ZweiOffice;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  // Strings become text, {code: "x"} becomes <code>x</code>.
  function richText(parts) {
    const frag = document.createDocumentFragment();
    for (const part of parts) {
      if (part && typeof part === "object") frag.append(el("code", null, part.code));
      else frag.append(document.createTextNode(String(part)));
    }
    return frag;
  }

  const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;

  const storage = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
    remove(key) { try { localStorage.removeItem(key); } catch { /* private mode */ } },
  };
  const LAST_JOB = "zweigeeks.lastJob";
  const LAST_PATH = "zweigeeks.repoPath";

  // ---------- labels ----------

  const TOOL_LABELS = {
    "repo-hygiene": "Repo hygiene checks",
    bandit: "bandit security lint",
    "pip-audit": "pip-audit dependency scan",
    ruff: "ruff lint",
    pytest: "The repo's own tests",
    "api-routes": "API route checks",
    "ui-templates": "Template checks",
  };

  const ABORT_REASONS = {
    ollama_wedged: "the model kept timing out",
    ollama_error: "Ollama kept returning errors",
    no_files_reviewed: "no file review produced a usable reply",
  };

  const REJECT_REASONS = {
    timeout: "the model timed out",
    ollama_unreachable: "Ollama couldn't be reached",
    truncated: "the reply was cut off at the token limit",
    no_edits: "the reply had no usable edits",
    edit_mismatch: "the edits didn't match the file",
    no_change: "the edit changed nothing",
    php_syntax_error: "the edit introduced a PHP syntax error",
    exposure_still_present: "the leaking output was still there",
    breaks_behavior: "it would break existing code",
    undefined_function: "it called a function that doesn't exist",
    not_verified: "the verifier didn't accept it",
    verify_timeout: "the verifier timed out",
    verify_failed: "the verifier call failed",
    python_syntax_error: "it broke the Python syntax",
    removed_definition: "it deleted existing functions",
    breaks_import: "the module stopped importing, so it was rolled back",
    undefined_name: "it used a name that isn't defined anywhere",
    commented_out_code: "it commented out working code instead of fixing it",
    dismissal_refused: "it said nothing needed fixing, but the static tool still flags the line",
    dismissed: "JAX found nothing to fix (likely a false positive)",
    no_file: "the finding has no file to edit",
    file_missing: "the file isn't there any more",
    file_too_large: "the file is too large and the finding has no line to focus on",
  };

  const WHO = { auditor: "KAI", fixer: "JAX", system: "Run" };

  const humanize = (code) => String(code || "unknown").replace(/_/g, " ");

  // ---------- page state ----------

  const ui = {
    log: $("log"),
    form: $("run-form"),
    startBtn: $("start-btn"),
    formError: $("form-error"),
    repoPath: $("repo-path"),
    maxIter: $("max-iterations"),
    report: $("report"),
  };

  let run = null;

  function newRun(jobId, mode, maxIterations, live) {
    return {
      jobId,
      mode,
      maxIterations,
      live, // started from this page, as opposed to picked up after a reload
      finished: false,
      toolRows: new Map(),
      fixRows: new Map(),
      review: null,
      applied: 0,
      rejected: 0,
    };
  }

  // ---------- agent cards ----------

  const CHIP_ACTIVE = { auditor: "Scanning…", fixer: "Fixing…" };

  function setAgent(who, text, active) {
    $(`status-${who}`).textContent = text;
    $(`card-${who}`).classList.toggle("is-active", Boolean(active));
    const chip = $(`chip-${who}`);
    chip.textContent = active ? CHIP_ACTIVE[who] : (/^Done|Idle/.test(text) ? "Idle" : "Waiting…");
    chip.classList.toggle("is-active", Boolean(active));
  }

  function setStat(who, text) {
    $(`stat-${who}`).textContent = text || "";
  }

  function resetAgents() {
    setAgent("auditor", "Idle", false);
    setAgent("fixer", "Idle", false);
  }

  function fixerStat() {
    if (run.applied || run.rejected) setStat("fixer", `${run.applied} applied, ${run.rejected} not applied`);
  }

  // ---------- activity log ----------

  function nearBottom() {
    const log = ui.log;
    return log.scrollHeight - log.scrollTop - log.clientHeight < 48;
  }

  function append(li) {
    const stick = nearBottom();
    ui.log.append(li);
    if (stick) ui.log.scrollTop = ui.log.scrollHeight;
  }

  // A log row for one agent. Returns a handle for updating it in place, so
  // start/done event pairs share one row instead of stacking two.
  function logRow(who, state, text, subs = []) {
    const li = el("li", `log-row log-${who}`);
    const dot = el("span", "dot");
    dot.setAttribute("aria-hidden", "true");
    const body = el("div", "body");
    li.append(dot, body);

    const handle = {
      set(nextState, nextText, nextSubs) {
        li.className = `log-row log-${who} is-${nextState}`;
        body.replaceChildren(el("span", "who", WHO[who]));
        body.append(Array.isArray(nextText) ? richText(nextText) : document.createTextNode(nextText));
        for (const sub of nextSubs || []) {
          if (sub == null || sub === "") continue;
          if (sub instanceof Node) body.append(sub);
          else if (typeof sub === "object") body.append(el("span", "sub", sub.prose));
          else body.append(el("span", "sub path", sub));
        }
        return handle;
      },
    };
    handle.set(state, text, subs);
    append(li);
    return handle;
  }

  const systemRow = (parts, state = "neutral") => logRow("system", state, Array.isArray(parts) ? parts : [parts]);

  function handoffRow(to, text) {
    const handle = logRow(to === "fixer" ? "auditor" : "fixer", "ok", text);
    return handle;
  }

  function passRow(label) {
    append(el("li", "log-pass", label));
  }

  // Fix rows that never got a fix_applied/fix_rejected (the Fixer skips
  // some findings without emitting either) are closed off here.
  function settleFixRows() {
    for (const [id, row] of run.fixRows) {
      if (row.open) {
        row.handle.set("neutral", `${id} not fixed`, [row.file, { prose: "See Fix attempts in the report for why." }]);
        row.open = false;
      }
    }
  }

  // ---------- event handlers ----------

  const handlers = {
    worktree_created() {
      systemRow("Working on a private copy of your latest commit. Your files aren't touched until the run ends.");
    },

    iteration_start({ iteration }) {
      // Every fix pass is followed by an audit; the one after the last fix
      // pass only checks the fixes.
      const label = iteration > run.maxIterations
        ? "Final check of the fixes"
        : `Pass ${iteration} of ${run.maxIterations}`;
      passRow(label);
      setAgent("auditor", iteration > run.maxIterations ? "Checking the fixes" : "Starting the audit", true);
      setAgent("fixer", "Waiting for findings", false);
      setStat("auditor", label);
    },

    stack_detected({ stack }) {
      logRow("auditor", "ok", stack && stack.length ? `Detected ${stack.join(", ")}` : "No known stack detected");
    },

    tool_start({ tool }) {
      const label = TOOL_LABELS[tool] || tool;
      setAgent("auditor", `Running ${label}`, true);
      run.toolRows.set(tool, logRow("auditor", "running", `${label}…`));
    },

    tool_done(payload) {
      const label = TOOL_LABELS[payload.tool] || payload.tool;
      // pytest reports whether the suite passed (a boolean), not a count.
      const isTests = payload.tool === "pytest";
      let text;
      let state;
      if (isTests && payload.passed == null) {
        text = `${label}: skipped`;
        state = "neutral";
      } else {
        text = isTests ? `${label} ${payload.passed ? "pass" : "fail"}` : `${label}: ${plural(payload.count ?? 0, "finding")}`;
        state = (isTests ? !payload.passed : payload.count) ? "warn" : "ok";
      }
      const row = run.toolRows.get(payload.tool);
      const subs = isTests && payload.skipped ? [{ prose: payload.skipped }] : [];
      if (row) row.set(state, text, subs);
      else logRow("auditor", state, text, subs);
      run.toolRows.delete(payload.tool);
    },

    tool_skip({ tools, reason }) {
      logRow("auditor", "neutral", `Skipped ${tools.join(", ")}`, [{ prose: reason }]);
    },

    file_review_skip({ files, reason }) {
      logRow("auditor", "neutral", `Left ${plural(files.length, "file")} out of the model review`, [{ prose: reason }]);
    },

    file_review_start({ candidates }) {
      const meter = el("span", "meter");
      const fill = el("span");
      meter.append(fill);
      run.review = { total: candidates, seen: 0, fill, meter, handle: null };
      run.review.handle = logRow("auditor", "running", `Model review: 0 of ${candidates} files`, [meter]);
    },

    file_review_file({ file }) {
      const r = run.review;
      if (!r) return;
      r.seen += 1;
      r.fill.style.width = `${Math.round((r.seen / Math.max(r.total, 1)) * 100)}%`;
      r.handle.set("running", `Model review: ${r.seen} of ${r.total} files`, [file, r.meter]);
      setAgent("auditor", `Reviewing ${file}`, true);
    },

    file_review_timeout({ file, consecutive }) {
      logRow("auditor", "warn", "Model timed out", [file, { prose: `${consecutive} in a row` }]);
    },

    file_review_failed({ file, error }) {
      logRow("auditor", "warn", "Model call failed", [file, { prose: error }]);
    },

    file_review_unparseable({ file }) {
      logRow("auditor", "warn", "Couldn't read the model's reply", [file, { prose: "Counted as not reviewed, not as clean." }]);
    },

    audit_aborted({ reason, files_reviewed }) {
      logRow("auditor", "error", "Model review stopped early", [
        { prose: `${ABORT_REASONS[reason] || humanize(reason)}; ${plural(files_reviewed, "file")} reviewed` },
      ]);
    },

    file_review_done({ files_reviewed, files_failed }) {
      const r = run.review;
      const text = `Model review: ${plural(files_reviewed, "file")} reviewed`;
      const subs = files_failed ? [{ prose: `${files_failed} couldn't be reviewed` }] : [];
      const state = files_failed ? "warn" : "ok";
      if (r) {
        r.fill.style.width = "100%";
        r.handle.set(state, text, subs);
      } else {
        logRow("auditor", state, text, subs);
      }
      run.review = null;
    },

    improvements_start() {
      setAgent("auditor", "Writing broader suggestions", true);
      run.toolRows.set("_improvements", logRow("auditor", "running", "Writing broader suggestions…"));
    },

    improvements_done({ count }) {
      const row = run.toolRows.get("_improvements");
      const text = `${plural(count, "suggestion")} for the report`;
      if (row) row.set("ok", text);
      else logRow("auditor", "ok", text);
      run.toolRows.delete("_improvements");
      setAgent("auditor", "Audit pass finished", false);
    },

    loop_continue({ blocking_count }) {
      handoffRow("fixer", `Handed ${plural(blocking_count, "blocking finding")} to JAX`);
      setAgent("auditor", "Waiting for fixes", false);
      setAgent("fixer", "Picking up findings", true);
    },

    fix_start({ finding_id, file }) {
      settleFixRows();
      const handle = logRow("fixer", "running", `Fixing ${finding_id}`, [file]);
      run.fixRows.set(finding_id, { handle, file, open: true });
      setAgent("fixer", `Fixing ${finding_id} in ${file}`, true);
    },

    fix_retry({ finding_id, reason }) {
      const row = run.fixRows.get(finding_id);
      if (row) row.handle.set("running", `Retrying ${finding_id}`, [row.file, { prose: `First edit didn't apply: ${reason}` }]);
    },

    fix_verify_start({ finding_id }) {
      const row = run.fixRows.get(finding_id);
      if (row) row.handle.set("running", `Verifying ${finding_id}`, [row.file]);
      setAgent("fixer", `Verifying the edit for ${finding_id}`, true);
    },

    fix_applied({ finding_id, file }) {
      const row = run.fixRows.get(finding_id);
      const handle = row ? row.handle : logRow("fixer", "ok", "");
      handle.set("ok", `${finding_id} fixed and verified`, [file]);
      if (row) row.open = false;
      run.applied += 1;
      fixerStat();
    },

    fix_dismissed({ finding_id, reason }) {
      const row = run.fixRows.get(finding_id);
      const handle = row ? row.handle : logRow("fixer", "neutral", "");
      handle.set("neutral", `${finding_id}: nothing to fix`, [row && row.file, { prose: `Likely a false positive: ${reason}` }]);
      if (row) row.open = false;
    },

    fix_rejected({ finding_id, reason }) {
      const row = run.fixRows.get(finding_id);
      const handle = row ? row.handle : logRow("fixer", "rejected", "");
      handle.set("rejected", `${finding_id} not written`, [row && row.file, { prose: REJECT_REASONS[reason] || humanize(reason) }]);
      if (row) row.open = false;
      run.rejected += 1;
      fixerStat();
    },

    fix_pass_aborted() {
      settleFixRows();
      logRow("fixer", "error", "Fix pass stopped", [{ prose: "The model kept timing out." }]);
    },

    fix_pass_done({ iteration, changed }) {
      settleFixRows();
      handoffRow(
        "auditor",
        changed ? `Fix pass ${iteration} done, ${plural(changed, "fix", "fixes")}. Back to KAI` : `Fix pass ${iteration} changed nothing`,
      );
      setAgent("fixer", "Waiting for findings", false);
    },

    loop_end({ reason, remaining }) {
      settleFixRows();
      const messages = {
        clean: ["No blocking findings left", "ok"],
        budget_exhausted: [`Out of passes with ${plural(remaining ?? 0, "blocking finding")} open`, "warn"],
        no_progress: ["Stopped: that pass couldn't fix anything, so another audit wouldn't change anything", "warn"],
        needs_manual_fix: [`${plural(remaining ?? 0, "finding")} need a manual fix: JAX ran out of attempts`, "warn"],
        fixer_ollama_timeouts: ["Stopped: JAX's model calls kept timing out", "error"],
      };
      let entry = messages[reason];
      if (!entry && String(reason).startsWith("incomplete_")) {
        const why = reason.slice("incomplete_".length);
        entry = [`Stopped: review incomplete because ${ABORT_REASONS[why] || humanize(why)}`, "error"];
      }
      const [text, state] = entry || [humanize(reason), "neutral"];
      systemRow(text, state);
      setAgent("auditor", "Done", false);
      setAgent("fixer", "Done", false);
    },

    worktree_removed() { /* nothing to show */ },

    fixes_applied({ files, skipped }) {
      if (files.length) systemRow(workingTreeLine(files.length), "ok");
      if (skipped.length) {
        systemRow(`Skipped ${plural(skipped.length, "file")} you've changed yourself or that no longer match`, "warn");
      }
    },

    fixes_apply_failed({ error, patch }) {
      systemRow(`Couldn't apply the fixes to your working tree: ${error}. They're saved in ${patch}`, "error");
    },

    audit_only_done({ finding_count, blocking }) {
      systemRow(`Audit finished: ${plural(finding_count, "finding")}, ${blocking} high severity`, "ok");
      setAgent("auditor", "Done", false);
    },

    pdf_report() {
      systemRow("PDF report written", "ok");
    },

    pdf_report_failed({ error }) {
      systemRow(`PDF report failed: ${error}`, "error");
    },

    pipeline_done() { /* job_complete carries the same result */ },

    job_complete(result) {
      finishRun();
      renderResults(result);
    },

    job_error({ error }) {
      finishRun();
      systemRow(`Run failed: ${error}`, "error");
      renderFailure(error);
    },
  };

  function handleEvent(msg) {
    const handler = handlers[msg.event];
    if (handler) handler(msg.payload || {});
    else systemRow(humanize(msg.event));
    if (office) office.event(msg.event, msg.payload || {});
  }

  // ---------- running a job ----------

  function setRunning(running) {
    ui.startBtn.disabled = running;
    ui.startBtn.textContent = running ? "Running…" : "Start run";
    for (const input of ui.form.querySelectorAll("input, #pick-btn")) input.disabled = running;
    if (!running) syncMode();
  }

  function finishRun() {
    if (!run || run.finished) return;
    run.finished = true;
    setRunning(false);
    resetAgents();
  }

  function startRunView(mode, maxIterations) {
    ui.log.replaceChildren();
    $("outcome").hidden = true;
    if (ui.report.open) ui.report.close();
    resetAgents();
    setStat("auditor", "");
    setStat("fixer", "");
    if (office) office.reset({ mode, maxIterations });
    if (mode === "audit") passRow("Audit only");
    setAgent("auditor", "Starting the audit", true);
    setAgent("fixer", mode === "audit" ? "Not needed for an audit-only run" : "Waiting for findings", false);
  }

  function connect(jobId) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/${encodeURIComponent(jobId)}`);
    ws.addEventListener("message", (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch { return; }
      handleEvent(msg);
    });
    ws.addEventListener("close", () => {
      if (run && run.jobId === jobId && !run.finished) {
        systemRow("Lost the connection to the ZweiGeeks server. Reload the page to reconnect if the run is still going.", "error");
        finishRun();
      }
    });
  }

  function showFormError(message) {
    ui.formError.textContent = message;
    ui.formError.hidden = !message;
  }

  function describeHttpError(data, status) {
    if (data && typeof data.error === "string") return data.error;
    if (data && Array.isArray(data.detail)) {
      return data.detail.map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join("; ");
    }
    if (data && typeof data.detail === "string") return data.detail;
    return `The server answered with HTTP ${status}.`;
  }

  ui.form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showFormError("");
    const repoPath = ui.repoPath.value.trim();
    if (!repoPath) {
      showFormError("Enter the path to a local git repository.");
      ui.repoPath.focus();
      return;
    }
    const mode = ui.form.elements.mode.value;
    const maxIterations = Number(ui.maxIter.value) || 1;

    ui.startBtn.disabled = true;
    let data, response;
    try {
      response = await fetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
        repo_path: repoPath, max_iterations: maxIterations, mode,
      }),
      });
      data = await response.json().catch(() => null);
    } catch {
      ui.startBtn.disabled = false;
      showFormError("Can't reach the ZweiGeeks server. Check that `python run.py` is still running.");
      return;
    }
    if (!response.ok || !data || !data.job_id) {
      ui.startBtn.disabled = false;
      showFormError(describeHttpError(data, response.status));
      return;
    }

    storage.set(LAST_JOB, data.job_id);
    storage.set(LAST_PATH, repoPath);
    run = newRun(data.job_id, mode, maxIterations, true);
    if (office) office.setInstant(false);
    startRunView(mode, maxIterations);
    setRunning(true);
    connect(data.job_id);
  });

  // Picks a run back up after a page reload. The websocket replays every
  // event the job has emitted so far, finished or not; the office skips the
  // walking for that burst so it lands straight on the current state.
  async function resume() {
    const jobId = storage.get(LAST_JOB);
    if (!jobId) return;
    let data;
    try {
      data = await (await fetch(`/api/jobs/${encodeURIComponent(jobId)}`)).json();
    } catch {
      return;
    }
    if (!data || data.error) {
      storage.remove(LAST_JOB); // the server restarted; jobs only live in memory
      return;
    }
    if (data.repo_path) ui.repoPath.value = data.repo_path;
    const mode = data.mode || "fix";
    ui.form.elements.mode.value = mode;
    const maxIterations = data.max_iterations || Number(ui.maxIter.value) || 1;
    run = newRun(jobId, mode, maxIterations, false);
    if (office) office.setInstant(true);
    startRunView(mode, maxIterations);
    if (data.status === "running") setRunning(true);
    else syncMode();
    connect(jobId);
    setTimeout(() => { if (office) office.setInstant(false); }, 1000);
  }

  // ---------- folder picker ----------

  // The server shows the OS's own folder dialog (a browser can't reveal a
  // folder's real path), so this only works because ZweiGeeks is local.
  $("pick-btn").addEventListener("click", async () => {
    const btn = $("pick-btn");
    showFormError("");
    btn.disabled = true;
    btn.textContent = "Waiting for the dialog…";
    try {
      const data = await (await fetch("/api/pick-folder", { method: "POST" })).json();
      if (data.error) {
        showFormError(data.error);
      } else if (data.path) {
        ui.repoPath.value = data.path;
        if (!data.is_git_repo) showFormError("That folder isn't a git repository (no .git inside). Pick the project's top-level folder.");
      }
    } catch {
      showFormError("Can't reach the ZweiGeeks server.");
    } finally {
      btn.disabled = ui.startBtn.textContent === "Running…";
      btn.textContent = "Choose folder…";
    }
  });

  // ---------- mode toggle ----------

  function syncMode() {
    const audit = ui.form.elements.mode.value === "audit";
    ui.maxIter.disabled = audit;
    $("passes-field").classList.toggle("is-disabled", audit);
    $("mode-hint").replaceChildren(
      audit
        ? document.createTextNode("Read-only: you get findings and a PDF report. Nothing in the repository is changed.")
        : document.createTextNode(
          "Fixes are edited straight into your files and left uncommitted, so Source Control shows them. Files you've already changed are left alone. Nothing is committed, staged or pushed.",
        ),
    );
  }
  for (const radio of ui.form.querySelectorAll('input[name="mode"]')) {
    radio.addEventListener("change", syncMode);
  }

  // ---------- results ----------

  const isHigh = (f) => f.severity === "critical" || f.severity === "high";
  // Mirrors AuditReport.blocking_findings() in src/schemas.py.
  const isBlocking = (f) => !f.fixed && isHigh(f) && f.source_tool !== "repo-hygiene";
  const bucketOf = (f) => (isHigh(f) ? "high" : f.severity === "medium" ? "medium" : "low");

  function verdictFor(result) {
    const findings = result.findings || [];
    const applied = (result.fix_attempts || []).filter((a) => a.patch_applied).length;
    const blocking = findings.filter(isBlocking).length;
    const landed = (result.applied_files || []).length;
    const fixesLine = landed ? `${plural(landed, "file")} changed in your working tree. ` : "";

    if (reason.endsWith("_incomplete")) {
      const why = ABORT_REASONS[reason.replace(/_incomplete$/, "")] || humanize(reason);
      return {
        state: "error",
        title: "Review incomplete",
        detail: `The model review stopped early because ${why}. A short or empty findings list doesn't mean the code is clean. Check ollama ps, then run again.`,
      };
    }
    if (reason === "audit_only") {
      return {
        state: blocking ? "warn" : "ok",
        title: `Audit finished with ${plural(findings.length, "finding")}`,
        detail: `${blocking} of them are high severity and would go to JAX. Nothing in the repository was changed.`,
      };
    }
    if (reason === "no_blocking_findings") {
      return {
        state: "ok",
        title: "No blocking findings left",
        detail: applied
          ? `${fixesLine}Review them in Source Control before committing.`
          : "Nothing high severity needed fixing, so no changes were made.",
      };
    }
    if (reason === "max_iterations_reached") {
      return {
        state: "warn",
        title: `${plural(blocking, "blocking finding")} still open after the last pass`,
        detail: `${fixesLine}Fix the rest by hand, or run again with more passes.`,
      };
    }
    if (reason === "no_progress" || reason === "needs_manual_fix") {
      const manual = findings.filter((f) => f.needs_manual_fix).length;
      return {
        state: applied ? "ok" : "warn",
        title: applied
          ? `${plural(applied, "fix", "fixes")} applied, ${plural(manual, "finding")} left for you`
          : "JAX couldn't fix these findings",
        detail: `${fixesLine}Findings marked "Needs manual fix" used up their attempts; each one's notes say why the last edit was rejected.`,
      };
    }
    if (reason === "fixer_ollama_timeouts") {
      return {
        state: "error",
        title: "Stopped: JAX's model calls kept timing out",
        detail: `${fixesLine}Check ollama ps for a stuck model, then run again.`,
      };
    }
    return { state: "warn", title: reason ? humanize(reason) : "Run finished", detail: "" };
  }

  function renderVerdict(v) {
    $("outcome").className = `panel outcome is-${v.state}`;
    $("outcome-title").textContent = v.title;
    $("outcome-detail").textContent = v.detail;
    $("verdict").className = `verdict is-${v.state}`;
    $("report-title").textContent = v.title;
    $("verdict-detail").textContent = v.detail;
    $("outcome").hidden = false;
  }

  function renderFailure(error) {
    renderVerdict({ state: "error", title: "Run failed", detail: error });
    $("open-report").hidden = true;
    $("pdf-link").hidden = true;
    $("working-tree").hidden = true;
    $("skipped").hidden = true;
  }

  function renderResults(result) {
    renderVerdict(verdictFor(result));
    $("open-report").hidden = false;

    const pdf = $("pdf-link");
    pdf.hidden = !result.has_pdf;
    pdf.href = `/api/jobs/${encodeURIComponent(run.jobId)}/report`;

    renderWorkingTree(result);

    renderFindings(result.findings || []);
    renderAttempts(result.fix_attempts || [], result.mode);
    renderDiff(result.diff || "", result.mode);
    renderIdeas(result.improvements || []);
    selectTab($("tab-findings"), false);
    if (run.live) openReport();
  }

  function openReport() {
    if (!ui.report.open) ui.report.showModal();
  }

  $("open-report").addEventListener("click", openReport);

  const workingTreeLine = (n) => `${plural(n, "file")} changed in your working tree. Open Source Control to review.`;

  function renderWorkingTree(result) {
    const files = result.applied_files || [];
    const skipped = result.skipped_files || [];
    const line = $("working-tree");
    line.hidden = !files.length;
    line.textContent = files.length ? workingTreeLine(files.length) : "";

    const box = $("skipped");
    box.hidden = !skipped.length;
    if (skipped.length) {
      $("skipped-summary").textContent = `${plural(skipped.length, "file")} skipped, not overwritten`;
      const list = $("skipped-list");
      list.replaceChildren();
      for (const item of skipped) {
        const li = el("li");
        li.append(el("code", null, item.file), document.createTextNode(`: ${item.reason}`));
        list.append(li);
      }
      $("skipped-patch").textContent = result.skipped_patch ? `Their fixes are saved in ${result.skipped_patch}` : "";
    }
  }

  $("close-report").addEventListener("click", () => ui.report.close());
  // Clicking the dimmed backdrop closes the dialog too.
  ui.report.addEventListener("click", (e) => {
    if (e.target === ui.report) ui.report.close();
  });

  let currentFindings = [];
  let currentFilter = "all";

  function renderFindings(findings) {
    currentFindings = findings;
    $("count-findings").textContent = String(findings.length);
    const counts = { high: 0, medium: 0, low: 0 };
    for (const f of findings) counts[bucketOf(f)] += 1;
    for (const chip of document.querySelectorAll("#filters .chip")) {
      const b = chip.dataset.bucket;
      chip.textContent = b === "all" ? `All ${findings.length}` : `${b[0].toUpperCase()}${b.slice(1)} ${counts[b]}`;
    }
    drawFindings();
  }

  function drawFindings() {
    const list = $("findings-list");
    list.replaceChildren();
    if (!currentFindings.length) {
      list.append(el("p", "empty", "No findings."));
      return;
    }
    const titles = { high: "High", medium: "Medium", low: "Low" };
    for (const bucket of ["high", "medium", "low"]) {
      if (currentFilter !== "all" && currentFilter !== bucket) continue;
      const items = currentFindings.filter((f) => bucketOf(f) === bucket);
      if (!items.length) continue;
      const section = el("section", `bucket bucket-${bucket}`);
      section.append(el("h3", null, `${titles[bucket]} (${items.length})`));
      for (const f of items) section.append(findingCard(f));
      list.append(section);
    }
    if (!list.children.length) list.append(el("p", "empty", "No findings at this severity."));
  }

  function findingCard(f) {
    const details = el("details", "finding");
    const summary = el("summary");
    summary.append(el("span", "fid", f.id), el("span", "ftitle", f.title));

    let statusText = "Open";
    let statusClass = "status-open";
    if (f.fixed) {
      statusText = "Fixed";
      statusClass = "status-fixed";
    } else if (f.dismissed) {
      statusText = "Likely false positive";
      statusClass = "status-open";
    } else if (f.needs_manual_fix) {
      statusText = "Needs manual fix";
      statusClass = "status-human";
    } else if (f.source_tool === "repo-hygiene" && isHigh(f)) {
      // Never sent to the Fixer: rotating a key or restoring a file is a human job.
      statusText = "Needs a human";
      statusClass = "status-human";
    }
    summary.append(el("span", `status ${statusClass}`, statusText));
    if (f.file) summary.append(el("span", "floc", f.line ? `${f.file}:${f.line}` : f.file));
    details.append(summary);

    const body = el("div", "finding-body");
    body.append(el("h4", null, "What's wrong"), el("p", null, f.detail || "No detail given."));
    if (f.resolution) body.append(el("h4", null, "Suggested resolution"), el("p", null, f.resolution));
    if (f.fix_notes) body.append(el("h4", null, "JAX's notes"), el("p", null, f.fix_notes));
    body.append(el("p", "meta", `Found by ${f.source_tool} in the ${f.category} check, severity ${f.severity}.`));
    details.append(body);
    return details;
  }

  $("filters").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    currentFilter = chip.dataset.bucket;
    for (const c of document.querySelectorAll("#filters .chip")) {
      c.setAttribute("aria-pressed", String(c === chip));
    }
    drawFindings();
  });

  function renderAttempts(attempts, mode) {
    const list = $("attempts-list");
    list.replaceChildren();
    $("count-fixes").textContent = attempts.length ? String(attempts.length) : "";
    if (!attempts.length) {
      list.append(el("li", "empty", mode === "audit"
        ? "Audit-only runs don't attempt fixes."
        : "JAX had nothing to attempt."));
      return;
    }
    for (const a of attempts) {
      const li = el("li", `attempt ${a.patch_applied ? "applied" : "rejected"}`);
      li.append(
        el("span", "badge", a.patch_applied ? "Applied" : a.timed_out ? "Timed out" : "Not applied"),
        el("span", "what", a.file ? `${a.finding_id} in ${a.file}` : a.finding_id),
        el("span", "notes", a.notes),
      );
      list.append(li);
    }
  }

  const MAX_DIFF_LINES = 5000;

  function renderDiff(diff, mode) {
    const view = $("diff-view");
    view.replaceChildren();
    const lines = diff.trim() ? diff.replace(/\n$/, "").split("\n") : [];
    const files = lines.filter((l) => l.startsWith("diff --git")).length;
    $("count-diff").textContent = files ? String(files) : "";
    if (!lines.length) {
      view.append(el("p", "empty", mode === "audit"
        ? "Audit-only runs don't change any files."
        : "JAX didn't change any files."));
      return;
    }
    const pre = el("pre", "diff");
    const frag = document.createDocumentFragment();
    for (const line of lines.slice(0, MAX_DIFF_LINES)) {
      let cls = "";
      if (line.startsWith("diff --git")) cls = "d-file";
      else if (line.startsWith("@@")) cls = "d-hunk";
      else if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("index ")) cls = "d-meta";
      else if (line.startsWith("+")) cls = "d-add";
      else if (line.startsWith("-")) cls = "d-del";
      frag.append(el("span", cls, line || " "));
    }
    pre.append(frag);
    view.append(pre);
    if (lines.length > MAX_DIFF_LINES) {
      view.append(el("p", "empty", `Showing the first ${MAX_DIFF_LINES} of ${lines.length} lines. The full patch is in your run's audit_results folder (fixes.patch).`));
    }
  }

  function renderIdeas(ideas) {
    const list = $("ideas-list");
    list.replaceChildren();
    $("count-ideas").textContent = ideas.length ? String(ideas.length) : "";
    if (!ideas.length) {
      list.append(el("li", "empty", "No broader suggestions this run."));
      return;
    }
    for (const idea of ideas) list.append(el("li", null, idea));
  }

  // ---------- tabs (ARIA tabs pattern, arrow-key navigation) ----------

  const tabs = Array.from(document.querySelectorAll('[role="tab"]'));

  function selectTab(tab, focus = true) {
    for (const t of tabs) {
      const selected = t === tab;
      t.setAttribute("aria-selected", String(selected));
      t.tabIndex = selected ? 0 : -1;
      $(t.getAttribute("aria-controls")).hidden = !selected;
    }
    if (focus) tab.focus();
  }

  for (const tab of tabs) {
    tab.addEventListener("click", () => selectTab(tab));
    tab.addEventListener("keydown", (e) => {
      const i = tabs.indexOf(tab);
      let next = null;
      if (e.key === "ArrowRight") next = tabs[(i + 1) % tabs.length];
      else if (e.key === "ArrowLeft") next = tabs[(i - 1 + tabs.length) % tabs.length];
      else if (e.key === "Home") next = tabs[0];
      else if (e.key === "End") next = tabs[tabs.length - 1];
      if (next) {
        e.preventDefault();
        selectTab(next);
      }
    });
  }

  // ---------- boot ----------

  async function loadConfig() {
    try {
      const cfg = await (await fetch("/api/config")).json();
      $("cfg-auditor").textContent = cfg.auditor_model;
      $("cfg-fixer").textContent = cfg.fixer_model;
      $("cfg-host").textContent = cfg.ollama_host.replace(/^https?:\/\//, "");
      ui.maxIter.value = cfg.max_iterations;
    } catch {
      $("runtime").hidden = true;
    }
  }

  if (office) {
    office.mount($("office"));
    for (const c of document.querySelectorAll("canvas.portrait")) office.portrait(c, c.dataset.who);
  }
  // Drop PNGs into static/sprites/ and they replace the CSS placeholder props.
  for (const img of document.querySelectorAll("img[data-sprite]")) {
    const show = () => img.closest(".prop").classList.add("has-sprite");
    if (img.complete && img.naturalWidth) show();
    img.addEventListener("load", show);
    img.addEventListener("error", () => img.remove());
  }
  const savedPath = storage.get(LAST_PATH);
  if (savedPath) ui.repoPath.value = savedPath;
  syncMode();
  loadConfig().then(resume);
})();
