/* Dynamic Seat Ledger client script (Task A10).
 * Handles real-time programme ledger summaries, intake utilisation bars,
 * admission pre-check simulations, commits, and admission event history.
 */
(() => {
  "use strict";

  const { api, toast, escapeHTML } = window.Nexus || {
    api: window.api,
    toast: window.toast,
    escapeHTML: str => String(str ?? "")
  };

  // State
  let currentProgrammes = [];
  let currentSummary = null;
  let currentPage = 1;
  let precheckedPayload = null;

  // DOM Elements
  const formFilter = document.getElementById("ledger-filter-form");
  const selectYear = document.getElementById("filter-year");
  const selectInst = document.getElementById("filter-institution");
  const selectProg = document.getElementById("filter-programme");
  const btnRefresh = document.getElementById("refresh-ledger-btn");

  const sectionSummary = document.getElementById("programme-summary-section");
  const statRegApproved = document.getElementById("stat-regular-approved");
  const statRegAdmitted = document.getElementById("stat-regular-admitted");
  const statRegRemaining = document.getElementById("stat-regular-remaining");
  const statSupAdmitted = document.getElementById("stat-supernumerary-admitted");
  const statSupTrend = document.getElementById("stat-supernumerary-trend");

  const utilBadge = document.getElementById("utilisation-badge");
  const utilText = document.getElementById("utilisation-text");
  const utilBar = document.getElementById("utilisation-progress-bar");
  const utilFill = document.getElementById("utilisation-progress-fill");
  const catTbody = document.getElementById("category-breakdown-tbody");

  const formPrecheck = document.getElementById("precheck-form");
  const inputStudentId = document.getElementById("adm-student-id");
  const inputFullName = document.getElementById("adm-full-name");
  const inputApaar = document.getElementById("adm-apaar-ref");
  const selectCategory = document.getElementById("adm-category");
  const selectClassification = document.getElementById("adm-classification");
  const checkNri = document.getElementById("flag-is-nri");
  const checkOci = document.getElementById("flag-is-oci");
  const checkFn = document.getElementById("flag-is-fn");

  const btnFillDemo = document.getElementById("fill-demo-btn");
  const btnRunPrecheck = document.getElementById("run-precheck-btn");
  const btnCommitAdmission = document.getElementById("commit-admission-btn");
  const boxPrecheckResult = document.getElementById("precheck-result-box");
  const msgPrecheck = document.getElementById("precheck-result-message");
  const detailsPrecheck = document.getElementById("precheck-result-details");

  const selectEventsResult = document.getElementById("events-result-filter");
  const tbodyEvents = document.getElementById("admission-events-tbody");
  const labelEventsCount = document.getElementById("events-count-label");
  const btnPrevEvents = document.getElementById("events-prev-btn");
  const btnNextEvents = document.getElementById("events-next-btn");
  const indicatorEventsPage = document.getElementById("events-page-indicator");

  // Helper: Format date/time
  function formatTimestamp(isoStr) {
    if (!isoStr) return "—";
    try {
      const d = new Date(isoStr);
      return d.toLocaleString("en-IN", {
        year: "numeric",
        month: "short",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit"
      });
    } catch {
      return isoStr;
    }
  }

  // Load available programmes for current filter selection
  async function loadProgrammes() {
    const year = selectYear.value;
    const instId = selectInst.value;

    selectProg.innerHTML = '<option value="">Loading programmes...</option>';
    selectProg.disabled = true;

    try {
      const params = { academic_year: year };
      if (instId) params.institution_id = instId;

      const res = await api("/api/seats/summary", { params });
      if (!res.ok) throw new Error(res.error?.message || "Failed to load programme summaries.");

      currentProgrammes = res.data || [];

      if (currentProgrammes.length === 0) {
        selectProg.innerHTML = '<option value="">No programmes found</option>';
        sectionSummary.style.display = "none";
        return;
      }

      selectProg.innerHTML = currentProgrammes.map(p => `
        <option value="${escapeHTML(p.programme_id)}">
          ${escapeHTML(p.programme_code)} — ${escapeHTML(p.programme_name)} (${escapeHTML(p.institution_code)})
        </option>
      `).join("");
      selectProg.disabled = false;

      // Automatically load ledger for the first programme
      await loadProgrammeLedger();
    } catch (err) {
      selectProg.innerHTML = '<option value="">Failed to load programmes</option>';
      toast(err.message || "Failed to load programme list.", "danger");
    }
  }

  // Load detailed ledger metrics for the currently selected programme
  async function loadProgrammeLedger() {
    const progId = selectProg.value;
    const year = selectYear.value;
    const instId = selectInst.value;

    if (!progId) {
      sectionSummary.style.display = "none";
      return;
    }

    try {
      const params = {
        programme_id: progId,
        academic_year: year
      };
      if (instId) params.institution_id = instId;

      const res = await api("/api/seats/ledger", { params });
      if (!res.ok) throw new Error(res.error?.message || "Failed to load detailed ledger.");

      currentSummary = res.data;
      renderProgrammeSummary(currentSummary);
      sectionSummary.style.display = "block";

      // Also refresh the admission events history
      loadAdmissionEvents(1);
    } catch (err) {
      toast(err.message || "Failed to load programme ledger.", "danger");
    }
  }

  // Render cards, utilisation progress bar, and category breakdown
  function renderProgrammeSummary(s) {
    statRegApproved.textContent = s.approved_regular;
    statRegAdmitted.textContent = s.regular_admitted;
    statRegRemaining.textContent = s.regular_remaining;
    statSupAdmitted.textContent = `${s.supernumerary_admitted} / ${s.supernumerary_capacity}`;
    statSupTrend.textContent = `Remaining: ${s.supernumerary_remaining}`;

    // Utilisation Bar
    const pct = Number(s.utilisation_pct) || 0;
    utilText.textContent = `${pct.toFixed(1)}%`;
    utilFill.style.width = `${Math.min(100, Math.max(0, pct))}%`;

    // Utilisation styling
    utilBar.className = "progress";
    if (pct >= 100) {
      utilBar.classList.add("progress-100");
      utilBadge.className = "badge badge-danger";
      utilBadge.textContent = "100% — FULL (Strict Block)";
    } else if (pct >= 90) {
      utilBar.classList.add("progress-90");
      utilBadge.className = "badge badge-warn";
      utilBadge.textContent = "90% — Critical Warning";
    } else if (pct >= 80) {
      utilBar.classList.add("progress-80");
      utilBadge.className = "badge badge-info";
      utilBadge.textContent = "80% — Early Warning";
    } else {
      utilBadge.className = "badge badge-ok";
      utilBadge.textContent = "Capacity Available";
    }

    // Category Breakdown Table: Full dynamic breakdown for OPEN, OBC, SC, ST, NRI, OCI, FN
    const catQuotas = s.category_quotas || {
      "OPEN": 90, "OBC": 50, "SC": 24, "ST": 16,
      "NRI": 6, "OCI": 5, "FN": 4
    };
    const catBreakdown = s.category_breakdown || {};
    const categoriesMeta = [
      { key: "OPEN", label: "OPEN (Regular)", isSuper: false },
      { key: "OBC", label: "OBC (Regular)", isSuper: false },
      { key: "SC", label: "SC (Regular)", isSuper: false },
      { key: "ST", label: "ST (Regular)", isSuper: false },
      { key: "NRI", label: "NRI Category (Supernumerary)", isSuper: true },
      { key: "OCI", label: "OCI Status (Supernumerary)", isSuper: true },
      { key: "FN", label: "Foreign National (Supernumerary)", isSuper: true },
    ];

    const rowsHtml = categoriesMeta.map(c => {
      const admitted = catBreakdown[c.key] ?? 0;
      const quota = catQuotas[c.key] ?? "—";
      const pct = typeof quota === "number" && quota > 0 ? ((admitted / quota) * 100).toFixed(1) : "—";
      const badgeClass = c.isSuper ? "badge-warn" : "badge-info";
      return `
        <tr>
          <td>
            <strong>${escapeHTML(c.label)}</strong>
          </td>
          <td style="text-align: right; font-variant-numeric: tabular-nums;">
            <span style="font-weight: 600; color: ${c.isSuper ? '#7c3aed' : 'var(--navy)'};">${admitted}</span>
            <span style="color: var(--muted); font-size: 12px;"> / ${quota}</span>
            <span class="badge ${badgeClass}" style="margin-left: 8px; font-size: 11px;">${pct}%</span>
          </td>
        </tr>
      `;
    }).join("");

    catTbody.innerHTML = rowsHtml;

    // Reset pre-check state when programme changes
    resetPrecheckState();
  }

  function resetPrecheckState() {
    precheckedPayload = null;
    boxPrecheckResult.style.display = "none";
    btnCommitAdmission.disabled = true;
    btnCommitAdmission.className = "btn";
  }

  // Pre-fill demo values helper: VESIT / B.Tech IT / 2026-27 (90/180 regular occupied, 50% half-filled)
  function fillDemoValues() {
    // Select 2026-27
    selectYear.value = "2026-27";

    // Select VESIT if AICTE dropdown exists
    if (selectInst && selectInst.tagName === "SELECT") {
      const vesitOpt = Array.from(selectInst.options).find(o => o.text.includes("VESIT"));
      if (vesitOpt) selectInst.value = vesitOpt.value;
    }

    // Ensure programmes are loaded, then select IT
    loadProgrammes().then(() => {
      const itOpt = Array.from(selectProg.options).find(o => 
        o.text.startsWith("BTECH-IT") || 
        o.text.includes("BTECH-IT") || 
        o.text.includes("Information Technology")
      );
      if (itOpt) {
        selectProg.value = itOpt.value;
        loadProgrammeLedger();
        loadAdmissionEvents(1);
      }

      // Generate next student ID above 98
      const nextRoll = 99;
      inputStudentId.value = `VESIT-26-BTECH-IT-${String(nextRoll).padStart(4, "0")}`;
      inputFullName.value = "Kavya Deshmukh";
      inputApaar.value = "912345678999";
      selectCategory.value = "OPEN";
      selectClassification.value = "REGULAR";
      if (checkNri) checkNri.checked = false;
      if (checkOci) checkOci.checked = false;
      if (checkFn) checkFn.checked = false;

      toast("Demo values loaded for VESIT B.Tech IT (2026-27). Remaining regular seats: 90 / 180 (50.0% utilised).", "info");
      resetPrecheckState();
    });
  }

  // Category and classification sync
  if (selectCategory && selectClassification) {
    selectCategory.addEventListener("change", () => {
      const isSuper = ["SUPERNUMERARY", "NRI", "OCI", "FN"].includes(selectCategory.value);
      selectClassification.value = isSuper ? "SUPERNUMERARY" : "REGULAR";
    });
  }

  // Build admission payload from form
  function getFormData() {
    const prog = currentProgrammes.find(p => p.programme_id === selectProg.value);
    const instId = prog?.institution_id || selectInst.value;
    const cat = selectCategory?.value || "OPEN";
    const isSuperCat = ["SUPERNUMERARY", "NRI", "OCI", "FN"].includes(cat);
    const classificationVal = selectClassification?.value || (isSuperCat ? "SUPERNUMERARY" : "REGULAR");

    return {
      institution_id: instId,
      programme_id: selectProg.value,
      academic_year: selectYear.value,
      institution_student_id: inputStudentId.value.trim(),
      full_name: inputFullName.value.trim(),
      apaar_ref: inputApaar.value.trim() || null,
      admission_category: cat,
      classification: classificationVal,
      is_nri: (cat === "NRI" || cat === "SUPERNUMERARY" || Boolean(checkNri?.checked)),
      is_oci: (cat === "OCI" || Boolean(checkOci?.checked)),
      is_foreign_national: (cat === "FN" || Boolean(checkFn?.checked))
    };
  }

  // Run Pre-Check simulation
  async function runPrecheck() {
    const payload = getFormData();

    if (!payload.programme_id || !payload.academic_year) {
      toast("Please select an institution and degree programme first.", "warn");
      return;
    }
    if (!payload.institution_student_id || !payload.full_name) {
      toast("Please fill Student Institutional ID and Full Name.", "warn");
      return;
    }

    btnRunPrecheck.disabled = true;
    btnRunPrecheck.textContent = "Checking...";

    try {
      const res = await api("/api/admissions/precheck", {
        method: "POST",
        body: payload
      });

      const outcome = res.data || {};
      boxPrecheckResult.style.display = "block";

      if (outcome.allowed) {
        // ALLOWED state
        boxPrecheckResult.style.backgroundColor = "var(--ok-bg)";
        boxPrecheckResult.style.borderColor = "var(--ok)";
        msgPrecheck.style.color = "var(--ok-ink)";
        msgPrecheck.textContent = `ALLOWED: ${outcome.message}`;

        const after = outcome.summary_after;
        if (after) {
          detailsPrecheck.textContent = (payload.classification === "REGULAR")
            ? `Projected Regular Admitted: ${after.regular_admitted} / ${after.approved_regular} (${after.regular_remaining} seats remaining, ${after.utilisation_pct}% utilisation)`
            : `Projected Supernumerary Admitted: ${after.supernumerary_admitted} / ${after.supernumerary_capacity} (${after.supernumerary_remaining} seats remaining)`;
        } else {
          detailsPrecheck.textContent = "Admission criteria met. You may now commit this admission.";
        }

        // Enable Commit button
        precheckedPayload = payload;
        btnCommitAdmission.disabled = false;
        btnCommitAdmission.className = "btn";
        toast("Pre-check passed: Admission is eligible and seats are available.", "ok");
      } else {
        // BLOCKED state
        boxPrecheckResult.style.backgroundColor = "var(--danger-bg)";
        boxPrecheckResult.style.borderColor = "var(--danger)";
        msgPrecheck.style.color = "var(--danger)";
        msgPrecheck.textContent = `BLOCKED [${escapeHTML(outcome.reason_code)}]: ${escapeHTML(outcome.message)}`;
        detailsPrecheck.textContent = "Admission cannot proceed under current institutional intake rules.";

        // Disable Commit button
        precheckedPayload = null;
        btnCommitAdmission.disabled = true;
        toast(`Admission blocked: ${outcome.reason_code}`, "danger");

        // Reload events to show audit record of blocked attempt
        loadAdmissionEvents(1);
      }
    } catch (err) {
      boxPrecheckResult.style.display = "block";
      boxPrecheckResult.style.backgroundColor = "var(--danger-bg)";
      boxPrecheckResult.style.borderColor = "var(--danger)";
      msgPrecheck.style.color = "var(--danger)";
      msgPrecheck.textContent = `ERROR: ${err.message}`;
      detailsPrecheck.textContent = "";
      btnCommitAdmission.disabled = true;
      toast(err.message || "Failed to complete admission pre-check.", "danger");
    } finally {
      btnRunPrecheck.disabled = false;
      btnRunPrecheck.textContent = "Run Pre-Check";
    }
  }

  // Commit Admission to the Ledger
  async function commitAdmission() {
    if (!precheckedPayload) {
      toast("Please perform an admission pre-check first.", "warn");
      return;
    }

    btnCommitAdmission.disabled = true;
    btnCommitAdmission.textContent = "Committing...";

    try {
      const res = await api("/api/admissions", {
        method: "POST",
        body: precheckedPayload
      });

      if (!res.ok) {
        throw new Error(res.error?.message || "Admission commit failed.");
      }

      toast(`Admission committed successfully for ${precheckedPayload.institution_student_id}!`, "ok");

      // Success UI update
      boxPrecheckResult.style.backgroundColor = "var(--ok-bg)";
      boxPrecheckResult.style.borderColor = "var(--ok)";
      msgPrecheck.style.color = "var(--ok-ink)";
      msgPrecheck.textContent = `COMMITTED: Student ${escapeHTML(precheckedPayload.institution_student_id)} successfully enrolled.`;
      detailsPrecheck.textContent = "Admission event recorded in immutable ledger.";

      // Refresh data
      await loadProgrammeLedger();

      // Clear student ID to prevent accidental double commit
      inputStudentId.value = "";
      inputFullName.value = "";
      inputApaar.value = "";
      resetPrecheckState();
    } catch (err) {
      // 409 Conflict handled here
      boxPrecheckResult.style.display = "block";
      boxPrecheckResult.style.backgroundColor = "var(--danger-bg)";
      boxPrecheckResult.style.borderColor = "var(--danger)";
      msgPrecheck.style.color = "var(--danger)";
      msgPrecheck.textContent = `COMMIT REJECTED: ${err.message}`;
      detailsPrecheck.textContent = "Transaction rolled back. Admission event persisted as REJECTED.";

      toast(err.message || "Admission was rejected by the Dynamic Seat Ledger.", "danger");
      await loadProgrammeLedger();
    } finally {
      btnCommitAdmission.textContent = "Commit Admission";
    }
  }

  // Load Paginated Admission Events History
  async function loadAdmissionEvents(page = 1) {
    currentPage = page;
    const progId = selectProg.value;
    const year = selectYear.value;
    const instId = selectInst.value;
    const resultFilter = selectEventsResult.value;

    tbodyEvents.innerHTML = '<tr><td colspan="8" class="align-center muted">Loading events...</td></tr>';

    try {
      const params = {
        page: currentPage,
        per_page: 15,
        academic_year: year
      };
      if (instId) params.institution_id = instId;
      if (progId) params.programme_id = progId;
      if (resultFilter) params.result = resultFilter;

      const res = await api("/api/admissions/events", { params });
      if (!res.ok) throw new Error(res.error?.message || "Failed to load admission events.");

      const events = res.data || [];
      const meta = res.meta || { total: 0, total_pages: 1 };

      if (events.length === 0) {
        tbodyEvents.innerHTML = '<tr><td colspan="8" class="align-center muted">No admission events recorded yet.</td></tr>';
        labelEventsCount.textContent = "Showing 0 events";
        btnPrevEvents.disabled = true;
        btnNextEvents.disabled = true;
        indicatorEventsPage.textContent = "Page 1 of 1";
        return;
      }

      tbodyEvents.innerHTML = events.map(ev => {
        const isAccepted = ev.result === "ACCEPTED";
        const badgeClass = isAccepted ? "badge-ok" : "badge-danger";
        const studentLabel = ev.student_name
          ? `<strong>${escapeHTML(ev.student_name)}</strong><br><small class="muted">${escapeHTML(ev.institution_student_id || "—")}</small>`
          : (ev.student_id ? `ID: ${escapeHTML(ev.student_id.substring(0, 8))}...` : '<span class="muted">—</span>');

        return `
          <tr>
            <td style="white-space: nowrap; font-size: .85rem;">${formatTimestamp(ev.created_at)}</td>
            <td>${studentLabel}</td>
            <td><small>${escapeHTML(ev.programme_code || ev.programme_id || "—")}</small></td>
            <td>
              <span class="badge badge-info" style="font-size: .75rem;">${escapeHTML(ev.classification)}</span>
              <small class="muted">${escapeHTML(ev.category)}</small>
            </td>
            <td><span class="badge ${badgeClass}">${escapeHTML(ev.result)}</span></td>
            <td><code>${escapeHTML(ev.reason_code)}</code></td>
            <td style="max-width: 280px; font-size: .85rem;" title="${escapeHTML(ev.message || '')}">
              ${escapeHTML(ev.message || "—")}
            </td>
            <td><small class="muted">${escapeHTML(ev.actor_id || "SYSTEM")}</small></td>
          </tr>
        `;
      }).join("");

      // Update Pagination
      labelEventsCount.textContent = `Showing ${events.length} of ${meta.total} events`;
      indicatorEventsPage.textContent = `Page ${meta.page} of ${meta.total_pages || 1}`;
      btnPrevEvents.disabled = (meta.page <= 1);
      btnNextEvents.disabled = (meta.page >= (meta.total_pages || 1));
    } catch (err) {
      tbodyEvents.innerHTML = `<tr><td colspan="8" class="align-center" style="color: var(--danger);">Failed to load events: ${escapeHTML(err.message)}</td></tr>`;
    }
  }

  // Event Listeners
  selectYear.addEventListener("change", () => loadProgrammes());
  if (selectInst && selectInst.tagName === "SELECT") {
    selectInst.addEventListener("change", () => loadProgrammes());
  }
  selectProg.addEventListener("change", () => loadProgrammeLedger());
  btnRefresh.addEventListener("click", () => loadProgrammeLedger());

  btnFillDemo.addEventListener("click", fillDemoValues);
  btnRunPrecheck.addEventListener("click", runPrecheck);
  btnCommitAdmission.addEventListener("click", commitAdmission);

  selectEventsResult.addEventListener("change", () => loadAdmissionEvents(1));
  btnPrevEvents.addEventListener("click", () => {
    if (currentPage > 1) loadAdmissionEvents(currentPage - 1);
  });
  btnNextEvents.addEventListener("click", () => {
    loadAdmissionEvents(currentPage + 1);
  });

  // Reset precheck button if user edits input values
  [inputStudentId, inputFullName, inputApaar, selectCategory, selectClassification, checkNri, checkOci, checkFn].forEach(input => {
    input.addEventListener("input", resetPrecheckState);
  });

  // Initial load on DOM ready
  document.addEventListener("DOMContentLoaded", () => {
    loadProgrammes();
  });
})();
