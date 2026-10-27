/* National and institution dashboards use only scoped server results. */
(() => {
  "use strict";
  const root = document.getElementById("governance-screen");
  if (!root) return;
  const N = window.Nexus;
  const get = id => document.getElementById(id);
  const national = root.dataset.mode === "governance";
  const filters = get("governance-filters");
  const charts = new Map();
  const colors = {
    navy: "#14315c",
    navyDark: "#0e2444",
    teal: "#14747a",
    tealLight: "#e6f7f8",
    blue: "#2b6cb0",
    grey: "#cbd5e1",
    orange: "#e8892b",
    warnInk: "#805410",
    red: "#c0392b",
    green: "#1a7f4b",
    greenLight: "#eaf5ee"
  };
  let institutions = [];
  let programmesMap = new Map();
  const current = () => Object.fromEntries([...new FormData(filters)].filter(([, value]) => value !== ""));
  const cell = (text, href) => href ? N.el("a", { href }, text) : N.el("span", {}, text);

  /** Smooth count-up animation for numeric metrics with reduced-motion support. */
  function animateCountUp(element, endVal, duration = 800, suffix = "", decimals = 0) {
    if (!element) return;
    const prefersReducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (prefersReducedMotion || typeof endVal !== "number" || isNaN(endVal)) {
      element.textContent = (typeof endVal === "number" ? endVal.toFixed(decimals) : endVal) + suffix;
      return;
    }
    const startVal = parseFloat(element.dataset.currentVal) || 0;
    element.dataset.currentVal = endVal;
    if (startVal === endVal) {
      element.textContent = endVal.toFixed(decimals) + suffix;
      return;
    }
    const startTime = performance.now();
    function step(now) {
      const elapsed = now - startTime;
      const progress = Math.min(1, elapsed / duration);
      // easeOutCubic
      const ease = 1 - Math.pow(1 - progress, 3);
      const currentVal = startVal + (endVal - startVal) * ease;
      element.textContent = currentVal.toFixed(decimals) + suffix;
      if (progress < 1) {
        requestAnimationFrame(step);
      } else {
        element.textContent = endVal.toFixed(decimals) + suffix;
      }
    }
    requestAnimationFrame(step);
  }

  /** Replace one chart only after its current data request succeeds. */
  function chart(id, type, labels, datasets, options = {}) {
    const canvas = get(id);
    if (!canvas || !window.Chart) return;
    charts.get(id)?.destroy();
    const prefersReducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    charts.set(id, new Chart(canvas, {
      type,
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: prefersReducedMotion ? false : { duration: 750, easing: "easeOutQuart" },
        plugins: {
          legend: {
            display: datasets.length > 1,
            position: "bottom",
            labels: { boxWidth: 12, padding: 14, font: { family: "'Segoe UI', system-ui, sans-serif", size: 12 } }
          },
          tooltip: {
            padding: 10,
            cornerRadius: 6,
            backgroundColor: "#0e2444",
            titleFont: { weight: "600" }
          }
        },
        scales: type === "doughnut" ? {} : {
          x: {
            grid: { display: false },
            title: { display: !!options.xTitle, text: options.xTitle || "" },
            stacked: !!options.stacked
          },
          y: {
            beginAtZero: true,
            grid: { color: "rgba(221, 227, 234, 0.6)" },
            title: { display: !!options.yTitle, text: options.yTitle || "" },
            stacked: !!options.stacked
          }
        },
        ...options
      }
    }));
  }

  /** Assemble select options from real institution and capacity endpoints. */
  async function loadOptions() {
    const { data } = await N.api("/api/institutions", { params: { per_page: 100 } });
    institutions = data;
    const instSelect = get("dash-institution");
    if (instSelect) data.forEach(row => instSelect.append(N.el("option", { value: row.id }, `${row.code} — ${row.name}`)));
    const ids = national ? data.map(row => row.id) : [root.dataset.institution];
    const programmeSelect = get("dash-programme");
    const years = new Set();
    const seen = new Set();
    for (const id of ids) {
      if (!id) continue;
      const [programmes, capacities] = await Promise.allSettled([
        N.api(`/api/institutions/${encodeURIComponent(id)}/programmes`),
        N.api(`/api/institutions/${encodeURIComponent(id)}/capacities`)
      ]);
      if (programmes.status === "fulfilled") {
        programmes.value.data.forEach(row => {
          programmesMap.set(row.id, row.name);
          if (!seen.has(row.id)) {
            seen.add(row.id);
            programmeSelect.append(N.el("option", { value: row.id }, `${institutions.find(i => i.id === id)?.code || ""} ${row.name}`));
          }
        });
      }
      if (capacities.status === "fulfilled") capacities.value.data.forEach(row => years.add(row.academic_year));
    }
    const orderedYears = [...years].sort().reverse();
    orderedYears.forEach(year => get("dash-year").append(N.el("option", { value: year }, year)));
    if (!national && orderedYears.length) get("dash-year").value = orderedYears[0];
  }

  /** Compact statistics for national governance view. */
  function stats(entries) {
    const container = get("governance-stats");
    if (!container) return;
    const meta = {
      "Institutions": { icon: "🏛️", cls: "kpi-navy" },
      "Student records": { icon: "🎓", cls: "kpi-blue" },
      "Validated": { icon: "✅", cls: "kpi-green" },
      "Pending validation": { icon: "⏳", cls: "kpi-amber" },
      "Active enrolments": { icon: "📚", cls: "kpi-purple" },
      "Identity conflicts": { icon: "⚠️", cls: "kpi-red" },
      "Missing / invalid APAAR": { icon: "🔍", cls: "kpi-crimson" },
      "Capacity ≥ 80%": { icon: "📊", cls: "kpi-sky" },
      "Capacity ≥ 90%": { icon: "⚠️", cls: "kpi-orange" },
      "Capacity ≥ 100%": { icon: "🚨", cls: "kpi-darkred" },
      "Excess attempts": { icon: "🛑", cls: "kpi-red" }
    };
    container.replaceChildren(...entries.map(([label, value, hint], idx) => {
      const m = meta[label] || { icon: "📈", cls: "kpi-navy" };
      return N.el("div", { class: `card stat dash-kpi-card ${m.cls}` },
        N.el("div", { class: "dash-kpi-top" },
          N.el("span", { class: "stat-label" }, label),
          N.el("span", { class: "dash-kpi-icon", "aria-hidden": "true" }, m.icon)
        ),
        N.el("div", { class: "stat-number", id: `nat-stat-${idx}` }, N.fmtNumber(value)),
        N.el("div", { class: "stat-trend" }, hint || "System verified")
      );
    }));
  }

  function table(id, columns, rows, emptyText) {
    const el = get(id);
    if (el) N.renderTable(el, { columns, rows, emptyText });
  }

  /** Render active filter chips that can be removed individually. */
  function updateFilterChips() {
    const container = get("dash-active-chips");
    if (!container) return;
    const active = current();
    const chips = [];
    const labels = {
      academic_year: "Year",
      institution_id: "Institution",
      programme_id: "Programme",
      category: "Category",
      validation_status: "Validation",
      risk_level: "Risk"
    };

    for (const [key, val] of Object.entries(active)) {
      if (!val) continue;
      let displayVal = val;
      if (key === "programme_id") displayVal = programmesMap.get(val) || val.slice(0, 8);
      if (key === "institution_id") displayVal = institutions.find(i => i.id === val)?.code || val.slice(0, 8);
      const chip = N.el("span", { class: "filter-chip" },
        N.el("span", {}, `${labels[key] || key}: `),
        N.el("strong", {}, displayVal),
        N.el("button", {
          class: "filter-chip-remove",
          type: "button",
          "aria-label": `Remove ${labels[key] || key} filter`,
          on: {
            click: () => {
              const field = filters.elements[key];
              if (field) field.value = "";
              void refresh();
            }
          }
        }, "×")
      );
      chips.push(chip);
    }

    if (chips.length > 0) {
      container.replaceChildren(
        N.el("span", { class: "dash-active-chips-label" }, "Active filters:"),
        ...chips
      );
      container.classList.remove("u-hidden");
    } else {
      container.replaceChildren();
      container.classList.add("u-hidden");
    }
  }

  /** 1. HERO SUMMARY STRIP: Animated Radial Gauge + Narrative + Status Chip */
  function renderGaugeHero(s, inst) {
    const hero = get("dash-hero-card");
    if (!hero) return;

    const pct = Math.max(0, s.regular_utilisation_pct != null ? s.regular_utilisation_pct : 0);
    const radius = 54;
    const circumference = 2 * Math.PI * radius; // ~339.29
    const clampedPct = Math.min(100, pct);
    const strokeDashoffset = circumference - (clampedPct / 100) * circumference;

    let chipClass = "healthy";
    let chipLabel = "Healthy Capacity";
    let strokeColor = colors.green;

    if (pct < 70) {
      chipClass = "under-filled";
      chipLabel = "Under-filled (<70%)";
      strokeColor = colors.blue;
    } else if (pct <= 95) {
      chipClass = "healthy";
      chipLabel = "Healthy (70–95%)";
      strokeColor = colors.green;
    } else if (pct <= 100) {
      chipClass = "near-capacity";
      chipLabel = "Near Capacity (>95%)";
      strokeColor = colors.orange;
    } else {
      chipClass = "over-capacity";
      chipLabel = "Over Capacity (>100%)";
      strokeColor = colors.red;
    }

    hero.innerHTML = `
      <div class="dash-hero-gauge-area">
        <svg class="dash-hero-svg-gauge" viewBox="0 0 130 130" role="img" aria-label="Seat utilisation gauge: ${pct.toFixed(1)}%">
          <circle class="dash-hero-gauge-track" cx="65" cy="65" r="54" />
          <circle class="dash-hero-gauge-fill" id="hero-gauge-circle" cx="65" cy="65" r="54"
                  stroke-dasharray="${circumference.toFixed(2)}"
                  stroke-dashoffset="${strokeDashoffset.toFixed(2)}"
                  stroke="${strokeColor}" />
        </svg>
        <div class="dash-hero-gauge-text">
          <span class="dash-hero-gauge-val" id="hero-gauge-pct">${pct.toFixed(1)}%</span>
          <span class="dash-hero-gauge-lbl">REGULAR</span>
        </div>
      </div>
      <div class="dash-hero-narrative">
        <div class="dash-hero-top">
          <span class="dash-hero-institution-badge">${inst.code || "INSTITUTION"} • ${inst.name || "Operations"}</span>
          <span class="dash-status-chip ${chipClass}">
            <span class="dash-refresh-dot"></span>
            ${chipLabel}
          </span>
        </div>
        <h2 class="dash-hero-title">${s.regular_admitted} of ${s.approved_regular_intake} regular seats filled</h2>
        <p class="dash-hero-sentence">${s.regular_remaining} remaining seats. ${s.supernumerary_admitted} supernumerary admitted (${s.supernumerary_utilisation_pct || 0}% of ${s.supernumerary_capacity || 0} quota).</p>
        <div class="dash-hero-meta-row">
          <div class="dash-hero-meta-item"><span>📅 Session:</span> <strong>${current().academic_year || "2026-27"}</strong></div>
          <div class="dash-hero-meta-item"><span>🏛️ Approved regular:</span> <strong>${s.approved_regular_intake} seats</strong></div>
          <div class="dash-hero-meta-item"><span>🌐 Supernumerary:</span> <strong>${s.supernumerary_capacity} quota</strong></div>
        </div>
      </div>
    `;

    const pctSpan = get("hero-gauge-pct");
    if (pctSpan) animateCountUp(pctSpan, pct, 800, "%", 1);
  }

  /** 2. ATTENTION NEEDED CARD: Actionable Alerts & Insights */
  function renderAttentionCard(s, pending, riskCases) {
    const card = get("dash-attention-card");
    if (!card) return;

    const rc = riskCases || { total: 0, CRITICAL: 0, HIGH: 0 };
    const alerts = [];

    if (rc.total > 0) {
      alerts.push({
        type: rc.CRITICAL > 0 ? "danger" : "warning",
        title: `${rc.total} risk anomaly ${rc.total === 1 ? "case" : "cases"} require review`,
        subtext: `${rc.CRITICAL || 0} critical, ${rc.HIGH || 0} high priority`,
        href: "/risk-cases",
        actionText: "Review cases →"
      });
    }

    if (s.regular_remaining <= 10 && s.regular_remaining >= 0) {
      alerts.push({
        type: s.regular_remaining <= 3 ? "danger" : "warning",
        title: `${s.regular_remaining} regular seats remaining (${s.regular_utilisation_pct}% capacity)`,
        subtext: "Intake nearing approved AICTE ceiling",
        href: "/seat-ledger",
        actionText: "View ledger →"
      });
    }

    if (pending.flagged > 0) {
      alerts.push({
        type: "danger",
        title: `${pending.flagged} student records flagged in validation`,
        subtext: "Cross-institution or data mismatch detected",
        href: "/students?validation_status=FLAGGED",
        actionText: "Inspect records →"
      });
    }

    if (pending.pending > 0) {
      alerts.push({
        type: "warning",
        title: `${pending.pending} student records pending validation`,
        subtext: "Awaiting scheduled identity scan",
        href: "/students",
        actionText: "Open students →"
      });
    }

    if (alerts.length === 0) {
      alerts.push({
        type: "success",
        title: "All operational indicators clear",
        subtext: "Capacity on target, 0 flagged checks, no pending risk cases.",
        href: "/seat-ledger",
        actionText: "Seat ledger →"
      });
    }

    const badgeClass = alerts.some(a => a.type === "danger") ? "badge-danger" :
                       alerts.some(a => a.type === "warning") ? "badge-warn" : "badge-ok";

    card.innerHTML = `
      <div class="dash-attention-header">
        <h3 class="dash-attention-title">
          <span>⚡ Actionable Insights</span>
        </h3>
        <span class="badge ${badgeClass} dash-attention-count-badge">${alerts.length} ${alerts.length === 1 ? "alert" : "alerts"}</span>
      </div>
      <ul class="dash-attention-list">
        ${alerts.slice(0, 3).map(a => `
          <li class="dash-attention-item ${a.type}">
            <div>
              <span class="dash-attention-text">${a.title}</span>
              <span class="dash-attention-subtext">${a.subtext}</span>
            </div>
            <a href="${a.href}" class="dash-attention-action">${a.actionText}</a>
          </li>
        `).join("")}
      </ul>
    `;
  }

  /** 3. RESTORED 9 KPI CARDS: Unified responsive 9-grid with count-up, semantic accents, and tooltips */
  function renderKpiCards(s, pending, riskCases, credCount) {
    const container = get("governance-stats");
    if (!container) return;

    const rc = riskCases || { total: 0, CRITICAL: 0, HIGH: 0 };
    const cards = [
      {
        label: "Approved intake",
        value: s.approved_regular_intake,
        icon: "🏛️",
        accent: "navy",
        tooltip: "Total approved regular seat intake sanctioned by AICTE",
        context: "Sanctioned regular quota"
      },
      {
        label: "Regular admissions",
        value: s.regular_admitted,
        icon: "🎓",
        accent: "ok",
        tooltip: "Active and enrolled students in regular approved seats",
        context: `${s.regular_utilisation_pct}% filled`,
        progressPct: Math.min(100, s.regular_utilisation_pct)
      },
      {
        label: "Remaining seats",
        value: s.regular_remaining,
        icon: "⏳",
        accent: s.regular_remaining <= 5 ? "warn" : "ok",
        tooltip: "Vacant regular seats available for new candidate enrolment",
        context: s.regular_remaining > 0 ? "Available for intake" : "Capacity filled"
      },
      {
        label: "Supernumerary admitted",
        value: s.supernumerary_admitted,
        icon: "🌐",
        accent: "info",
        tooltip: "Admissions under NRI, OCI, FN, and supernumerary quotas",
        context: `of ${s.supernumerary_capacity} quota seats`
      },
      {
        label: "Utilisation %",
        value: s.regular_utilisation_pct,
        unit: "%",
        decimals: 1,
        icon: "📊",
        accent: s.regular_utilisation_pct > 95 ? "warn" : "ok",
        tooltip: "Proportion of approved regular capacity currently utilised",
        context: "Regular capacity density",
        progressPct: Math.min(100, s.regular_utilisation_pct),
        progressClass: s.regular_utilisation_pct >= 95 ? "warn" : ""
      },
      {
        label: "Pending identity checks",
        value: pending.pending,
        icon: "🔍",
        accent: pending.pending > 0 ? "info" : "ok",
        tooltip: "Enrolled records awaiting identity scan and duplicate verification",
        isCalm: pending.pending === 0,
        calmText: "All scanned",
        context: pending.pending === 0 ? "All records verified" : "Awaiting scan"
      },
      {
        label: "Flagged checks",
        value: pending.flagged,
        icon: "⚠️",
        accent: pending.flagged > 0 ? "danger" : "ok",
        tooltip: "Records flagged with duplicate APAAR or multi-institution conflicts",
        isCalm: pending.flagged === 0,
        calmText: "0 flagged",
        context: pending.flagged === 0 ? "No anomalies" : "Requires review"
      },
      {
        label: "Risk cases",
        value: rc.total || 0,
        icon: "🚨",
        accent: (rc.total || 0) > 0 ? "warn" : "ok",
        tooltip: "Open identity and capacity risk cases awaiting human review",
        isCalm: (rc.total || 0) === 0,
        calmText: "Zero cases",
        context: (rc.total || 0) > 0 ? `${rc.CRITICAL || 0} critical, ${rc.HIGH || 0} high` : "No open cases"
      },
      {
        label: "Credentials issued",
        value: credCount,
        icon: "🛡️",
        accent: "ok",
        tooltip: "Cryptographically signed Ed25519 academic credentials issued to students",
        context: "Verifiable via QR & link"
      }
    ];

    container.innerHTML = cards.map((c, idx) => `
      <div class="dash-kpi-card" data-accent="${c.accent}" title="${c.tooltip}" tabindex="0">
        <div class="dash-kpi-card-header">
          <span class="dash-kpi-label">${c.label}</span>
          <span class="dash-kpi-icon-badge" aria-hidden="true">${c.icon}</span>
        </div>
        <div class="dash-kpi-value-row">
          <span class="dash-kpi-number" id="kpi-num-${idx}">${typeof c.value === "number" ? c.value : 0}</span>
          ${c.unit ? `<span class="dash-kpi-unit">${c.unit}</span>` : ""}
          ${c.isCalm ? `<span class="dash-kpi-calm-chip">✓ ${c.calmText}</span>` : ""}
        </div>
        <div class="dash-kpi-context-row">
          <span>${c.context}</span>
        </div>
        ${c.progressPct != null ? `
          <div class="dash-mini-progress" role="progressbar" aria-valuenow="${c.progressPct}" aria-valuemin="0" aria-valuemax="100">
            <div class="dash-mini-progress-fill ${c.progressClass || ''}" data-pct="${c.progressPct}"></div>
          </div>
        ` : ""}
      </div>
    `).join("");

    // Set progress percentages safely on elements
    container.querySelectorAll(".dash-mini-progress-fill[data-pct]").forEach(el => {
      el.style.setProperty("--progress-pct", `${el.dataset.pct}%`);
    });

    // Trigger count-ups
    cards.forEach((c, idx) => {
      const numEl = get(`kpi-num-${idx}`);
      if (numEl && typeof c.value === "number") {
        animateCountUp(numEl, c.value, 800, "", c.decimals || 0);
      }
    });
  }

  /** 4. PIPELINE FUNNEL COMPONENT */
  function renderFunnel(totalAdmitted, pending, credCount) {
    const container = get("dash-funnel-container");
    if (!container) return;

    const verified = Math.max(0, totalAdmitted - (pending.total || 0));
    const base = Math.max(1, totalAdmitted);

    const steps = [
      { name: "1. Admitted", count: totalAdmitted, pct: 100, color: colors.navy },
      { name: "2. Identity Verified", count: verified, pct: Math.round((verified / base) * 100), color: colors.teal },
      { name: "3. Credential Issued", count: credCount, pct: Math.round((credCount / base) * 100), color: colors.green }
    ];

    container.replaceChildren(...steps.map(step => {
      const stepEl = N.el("div", { class: "dash-funnel-step" },
        N.el("div", { class: "dash-funnel-step-header" },
          N.el("span", { class: "dash-funnel-step-name" }, step.name),
          N.el("span", { class: "dash-funnel-step-number" },
            `${step.count} `,
            N.el("small", { class: "muted" }, `(${step.pct}%)`)
          )
        ),
        N.el("div", { class: "dash-funnel-bar-track" },
          N.el("div", { class: "dash-funnel-bar-fill" })
        )
      );
      const fill = stepEl.querySelector(".dash-funnel-bar-fill");
      if (fill) {
        fill.style.setProperty("--funnel-pct", `${Math.min(100, step.pct)}%`);
        fill.style.setProperty("--funnel-bg", step.color);
      }
      return stepEl;
    }));

    const conversionPct = Math.round((credCount / base) * 100);
    const footerEl = N.el("div", { class: "dash-funnel-footer" },
      N.el("span", {}, "Pipeline conversion rate:"),
      N.el("strong", {}, `${conversionPct}% issued`)
    );
    container.append(footerEl);
  }

  /** 5. CHARTS: Category, Donut, Programmes, Risk */
  function renderCharts(data, seatsData) {
    const s = data.seat_summary || {};
    const pending = data.pending_identity_checks || { pending: 0, flagged: 0, total: 0 };
    const rc = data.risk_cases_by_severity || { LOW: 0, MEDIUM: 0, HIGH: 0, CRITICAL: 0, total: 0 };
    const categories = Object.values(s.category_capacity || {});

    // a. Category-wise Capacity Chart
    const catLabels = categories.map(row => row.category);
    const catAdmitted = categories.map(row => row.admitted);
    const catRows = categories.map(row => {
      const intake = row.capacity != null ? row.capacity : (data.capacity_by_category ? data.capacity_by_category[row.category] : null);
      const admitted = row.admitted != null ? row.admitted : 0;
      const remaining = intake != null ? Math.max(0, intake - admitted) : "—";
      return {
        category: row.category,
        intake: intake != null ? intake : "Open",
        admitted,
        remaining
      };
    });

    chart("chart-category", "bar", catLabels, [
      {
        label: "Approved Intake",
        data: catRows.map(r => typeof r.intake === "number" ? r.intake : 0),
        backgroundColor: colors.grey,
        borderRadius: 4
      },
      {
        label: "Admitted Students",
        data: catAdmitted,
        backgroundColor: [
          "#2563eb",
          "#0284c7",
          "#059669",
          "#d97706",
          "#7c3aed",
          "#9333ea",
          "#c026d3"
        ],
        borderRadius: 4
      }
    ], {
      xTitle: "Admission quota / category",
      yTitle: "Students"
    });

    table("category-capacity", [
      { key: "category", label: "Category" },
      { key: "intake", label: "Intake" },
      { key: "admitted", label: "Admitted" },
      { key: "remaining", label: "Remaining" }
    ], catRows, "No category capacity records.");

    // b. Regular Intake Utilisation Donut
    const regularRemaining = Math.max(0, s.regular_remaining);
    chart("chart-utilisation-donut", "doughnut", ["Admitted Regular", "Remaining Seats"], [
      {
        data: [s.regular_admitted, regularRemaining],
        backgroundColor: [s.regular_utilisation_pct >= 95 ? colors.orange : colors.navy, colors.grey],
        borderWidth: 0,
        hoverOffset: 4
      }
    ], {
      cutout: "75%",
      plugins: { legend: { display: false } }
    });

    const centerPct = get("donut-center-pct");
    if (centerPct) animateCountUp(centerPct, s.regular_utilisation_pct, 800, "%", 1);

    const progress = N.el("div", {
      class: `progress ${s.regular_utilisation_pct >= 100 ? "progress-100" : s.regular_utilisation_pct >= 90 ? "progress-90" : s.regular_utilisation_pct >= 80 ? "progress-80" : ""}`
    }, N.el("span", { class: "progress-fill" }));
    progress.firstElementChild.style.setProperty("--value", `${Math.max(0, Math.min(100, s.regular_utilisation_pct))}%`);

    const miniStats = N.el("div", { class: "dash-util-mini-grid" },
      N.el("div", { class: "dash-util-mini-card" },
        N.el("span", { class: "dash-util-mini-label" }, "Remaining seats"),
        N.el("span", { class: "dash-util-mini-val" }, String(s.regular_remaining)),
        N.el("span", { class: "dash-util-mini-sub" }, s.regular_remaining > 0 ? "Quota available" : "Capacity filled")
      ),
      N.el("div", { class: "dash-util-mini-card" },
        N.el("span", { class: "dash-util-mini-label" }, "Supernumerary"),
        N.el("span", { class: "dash-util-mini-val" }, `${s.supernumerary_admitted} / ${s.supernumerary_capacity}`),
        N.el("span", { class: "dash-util-mini-sub" }, `${s.supernumerary_utilisation_pct || 0}% quota filled`)
      ),
      N.el("div", { class: "dash-util-mini-card" },
        N.el("span", { class: "dash-util-mini-label" }, "Regular utilisation"),
        N.el("span", { class: "dash-util-mini-val" }, `${s.regular_utilisation_pct}%`),
        N.el("span", { class: "dash-util-mini-sub" }, `${s.regular_admitted} of ${s.approved_regular_intake} seats`)
      ),
      N.el("div", { class: "dash-util-mini-card" },
        N.el("span", { class: "dash-util-mini-label" }, "Total intake"),
        N.el("span", { class: "dash-util-mini-val" }, String(s.total_admitted || (s.regular_admitted + s.supernumerary_admitted))),
        N.el("span", { class: "dash-util-mini-sub" }, `Sanctioned: ${s.total_approved_intake || (s.approved_regular_intake + s.supernumerary_capacity)}`)
      )
    );

    get("institution-utilisation").replaceChildren(
      N.el("p", { class: "dash-utilisation-lead" }, `${s.regular_admitted} of ${s.approved_regular_intake} regular seats filled`),
      progress,
      miniStats
    );

    // c. Programme-wise Admissions Horizontal Bar Chart
    let progList = [];
    if (Array.isArray(seatsData) && seatsData.length > 0) {
      progList = seatsData.map(p => ({
        name: p.programme_name,
        admitted: p.regular_admitted,
        capacity: p.approved_regular
      }));
    } else {
      // Fallback from credentials or default
      const counts = new Map();
      data.credential_issuance_history.forEach(c => {
        if (c.programme_name) counts.set(c.programme_name, (counts.get(c.programme_name) || 0) + 1);
      });
      progList = [...counts.entries()].map(([name, admitted]) => ({ name, admitted, capacity: admitted }));
    }

    progList.sort((a, b) => b.admitted - a.admitted);

    chart("chart-programmes", "bar", progList.map(p => p.name.length > 25 ? p.name.slice(0, 24) + "…" : p.name), [
      {
        label: "Regular Admissions",
        data: progList.map(p => p.admitted),
        backgroundColor: progList.map((_, i) => ["#2563eb", "#7c3aed", "#059669", "#d97706", "#dc2626", "#0284c7"][i % 6]),
        borderRadius: 4
      }
    ], {
      indexAxis: "y",
      xTitle: "Admitted students",
      yTitle: ""
    });

    // d. Risk Level Breakdown Donut
    chart("chart-risk-donut", "doughnut", ["Low", "Medium", "High", "Critical"], [
      {
        data: [rc.LOW || 0, rc.MEDIUM || 0, rc.HIGH || 0, rc.CRITICAL || 0],
        backgroundColor: ["#10b981", "#f59e0b", "#ea580c", "#dc2626"],
        borderWidth: 0,
        hoverOffset: 4
      }
    ], {
      cutout: "70%",
      plugins: { legend: { display: false } }
    });

    const riskTotalEl = get("donut-risk-total");
    if (riskTotalEl) animateCountUp(riskTotalEl, rc.total || 0);

    table("institution-risk", [
      { key: "severity", label: "Severity", render: N.statusBadge },
      { key: "count", label: "Cases" }
    ], ["LOW", "MEDIUM", "HIGH", "CRITICAL"].map(severity => ({ severity, count: rc[severity] || 0 })), "No cases.");

    // e. Funnel
    renderFunnel(s.total_admitted || (s.regular_admitted + s.supernumerary_admitted), pending, data.credential_issuance_history.length);
  }

  /** National view remains unchanged for AICTE admins and auditors. */
  function renderNational(data) {
    const t = data.totals;
    stats([["Institutions", t.institutions], ["Student records", t.student_records], ["Validated", t.validated],
      ["Pending validation", t.pending_validations], ["Active enrolments", t.active_enrolments], ["Identity conflicts", t.identity_conflicts],
      ["Missing / invalid APAAR", t.missing_invalid_apaar], ["Capacity ≥ 80%", t.capacity_80],
      ["Capacity ≥ 90%", t.capacity_90], ["Capacity ≥ 100%", t.capacity_100], ["Excess attempts", t.excess_admission_attempts]]);
    const severity = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];
    chart("chart-risk", "bar", severity, [{ label: "Cases", data: severity.map(key => data.risk_cases_by_severity[key] || 0), backgroundColor: [colors.green, "#f59e0b", "#ea580c", "#dc2626"] }],
      { xTitle: "Risk severity", yTitle: "Cases", onClick: (_, active) => { if (active.length) window.location.assign(`/risk-cases?severity=${encodeURIComponent(severity[active[0].index])}`); } });
    const catEnrol = data.category_wise_enrolment || {};
    const catLabels = ["OPEN", "OBC", "SC", "ST", "NRI", "OCI", "FN"];
    const catData = catLabels.map(cat => catEnrol[cat] || 0);
    const catColors = [
      "#2563eb",
      "#0284c7",
      "#059669",
      "#d97706",
      "#7c3aed",
      "#9333ea",
      "#c026d3"
    ];
    chart("chart-category", "bar", catLabels, [{ label: "Enrolled Students", data: catData, backgroundColor: catColors, borderRadius: 4 }], { xTitle: "Admission quota / category", yTitle: "Students" });
    const years = [...new Set(data.institution_admission_trend.map(row => row.academic_year))].sort();
    const codes = [...new Set(data.institution_admission_trend.map(row => row.institution))].sort();
    chart("chart-trend", "line", years, codes.map((code, index) => ({ label: code,
      data: years.map(year => data.institution_admission_trend.find(row => row.academic_year === year && row.institution === code)?.admissions || 0),
      borderColor: ["#2563eb", "#ea580c", "#10b981", "#7c3aed"][index % 4], tension: 0 })), { xTitle: "Academic year", yTitle: "Admissions" });
    chart("chart-capacity", "bar", data.capacity_utilisation.map(row => row.code), [{ label: "Utilisation %", data: data.capacity_utilisation.map(row => row.percent),
      backgroundColor: data.capacity_utilisation.map(row => row.percent >= 100 ? "#ef4444" : row.percent >= 90 ? "#f59e0b" : row.percent >= 80 ? "#3b82f6" : "#10b981") }], { xTitle: "Institution", yTitle: "Utilisation %" });
    table("capacity-list", [{ key: "name", label: "Institution" }, { key: "percent", label: "Utilisation", render: value => N.badge(`${value}%`, value >= 100 ? "danger" : value >= 90 ? "warn" : "info") }],
      data.capacity_utilisation.filter(row => row.percent >= 80), "No institution has reached 80% of its selected capacity.");
    table("high-cases", [{ key: "reference", label: "Case", render: (value, row) => cell(value, `/risk-cases/${encodeURIComponent(row.id)}`) },
      { key: "severity", label: "Severity", render: N.statusBadge }, { key: "score", label: "Score" }, { key: "summary", label: "Summary" }],
      data.recent_high_risk_cases, "No high or critical cases match the filters.");
    table("recent-audit", [{ key: "time", label: "Time", render: N.fmtDateTime }, { key: "actor", label: "Actor" }, { key: "action", label: "Action" }],
      data.recent_audit_events, "No recent audit events.");
  }

  /** Complete upgraded Institution Operations View */
  function renderInstitution(data, seatsData) {
    const s = data.seat_summary;
    const pending = data.pending_identity_checks;
    const rc = data.risk_cases_by_severity;
    const credCount = data.credential_issuance_history.length;

    // 1. Hero Strip
    renderGaugeHero(s, data.institution || {});

    // 2. Actionable Insights
    renderAttentionCard(s, pending, rc);

    // 3. Upgraded 9 KPI Cards (Restored below filter bar & hero)
    renderKpiCards(s, pending, rc, credCount);

    // 4. Charts, Donut with Mini Stats, Programmes, Risk, and Funnel
    renderCharts(data, seatsData);

    // 5. Recent Activity & Validation Lists (all previous tables preserved)
    table("submission-list", [
      { key: "reference", label: "Submission", render: (value, row) => cell(value, `/upload`) },
      { key: "filename", label: "File" },
      { key: "status", label: "Status", render: N.statusBadge },
      { key: "valid_rows", label: "Valid" },
      { key: "error_rows", label: "Errors" }
    ], data.submission_history, "No submissions match this institution.");

    table("validation-errors", [
      { key: "code", label: "Error" },
      { key: "count", label: "Occurrences" }
    ], Object.entries(data.validation_error_counts || {}).map(([code, count]) => ({ code, count })), "No validation errors.");

    table("identity-checks", [
      { key: "status", label: "Status" },
      { key: "count", label: "Records" }
    ], [
      { status: "PENDING", count: pending.pending },
      { status: "FLAGGED", count: pending.flagged }
    ], "No identity checks.");

    table("credential-history", [
      { key: "reference", label: "Credential", render: (value, row) => cell(value, `/credentials/${encodeURIComponent(row.id)}`) },
      { key: "student_name", label: "Student" },
      { key: "programme_name", label: "Programme" },
      { key: "status", label: "Status", render: N.statusBadge }
    ], data.credential_issuance_history, "No credentials have been issued.");
  }

  /** Refresh data from server with filter integration. */
  async function refresh() {
    const statusEl = get("governance-status");
    const dot = get("dash-refresh-dot");
    if (statusEl) statusEl.textContent = "Updating...";
    if (dot) dot.classList.add("updating");

    try {
      updateFilterChips();
      if (national) {
        const { data } = await N.api("/api/dashboard/governance", { params: current() });
        renderNational(data);
      } else {
        const [dashRes, seatsRes] = await Promise.allSettled([
          N.api("/api/dashboard/institution", { params: current() }),
          N.api("/api/seats/summary", { params: current() })
        ]);

        if (dashRes.status !== "fulfilled") throw dashRes.reason;
        const dashData = dashRes.value.data;
        const seatsData = seatsRes.status === "fulfilled" && seatsRes.value.ok ? seatsRes.value.data : [];
        renderInstitution(dashData, seatsData);
      }
      if (statusEl) statusEl.textContent = "Updated just now";
      const tsEl = get("dash-last-timestamp");
      if (tsEl) {
        const now = new Date();
        const timeStr = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
        tsEl.textContent = `• ${timeStr}`;
      }
    } catch (error) {
      if (statusEl) statusEl.textContent = error.message;
      N.toast(error.message, "danger");
    } finally {
      if (dot) dot.classList.remove("updating");
    }
  }

  filters.addEventListener("submit", event => {
    event.preventDefault();
    void refresh();
  });

  get("dash-reset").addEventListener("click", () => {
    filters.reset();
    void refresh();
  });

  void (async () => {
    try {
      await loadOptions();
      await refresh();
    } catch (error) {
      if (get("governance-status")) get("governance-status").textContent = error.message;
      N.toast(error.message, "danger");
    }
  })();
})();
