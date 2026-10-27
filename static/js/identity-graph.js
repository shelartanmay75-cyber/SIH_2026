/* Offline, accessible identity explorer. Uses only Nexus and vendored Cytoscape. */
(() => {
  "use strict";
  const root = document.getElementById("identity-explorer");
  if (!root) return;
  const N = window.Nexus;
  const byId = id => document.getElementById(id);
  const form = byId("graph-filters");
  const canvas = byId("graph-canvas");
  const inspector = byId("graph-inspector");
  const caseId = root.dataset.caseId;
  const shortTypes = { ENROLLED_AT: "Institution", APAAR_REFERENCE: "APAAR", DUPLICATE_APAAR: "Same APAAR",
    SIMULTANEOUS_ENROLMENT: "Simultaneous enrolment", CONFLICTING_ACADEMIC_DATA: "Academic conflict",
    SIMILAR_NAME_CORROBORATED: "Corroborated name", SHARED_CONTACT: "Contact signal", SHARED_FEE_PAYER: "Payer signal",
    DUPLICATE_INSTITUTE_ID: "Institutional ID conflict" };
  let cy = null;
  let graphRequest = 0;
  let panelRequest = 0;
  let searchTimer;
  let view = { nodes: [], edges: [] };
  let activeParams = {};
  const graphControls = ["graph-layout", "graph-zoom-in", "graph-zoom-out", "graph-fit", "graph-reset"].map(byId);

  /** Display plain key/value evidence recursively, without treating data as HTML. */
  function keyValues(value) {
    if (value === null || typeof value !== "object") return N.el("span", {}, value == null ? "—" : String(value));
    if (Array.isArray(value)) return N.el("ul", {}, value.map(item => N.el("li", {}, keyValues(item))));
    const list = N.el("dl", { class: "kv" });
    Object.entries(value).forEach(([key, item]) => list.append(N.el("dt", {}, key.replaceAll("_", " ")), N.el("dd", {}, keyValues(item))));
    return list;
  }

  /** Read filter values and keep full identifiers out of request URLs. */
  function parameters() {
    const params = Object.fromEntries(new FormData(form));
    params.include_signals = byId("graph-signals").checked;
    params.case_id = caseId;
    const search = byId("graph-search").value.trim();
    if (/\d{12}/.test(search)) throw new Error("Use a masked APAAR reference, never a full reference.");
    if (/^XXXXXXXX\d{4}$/i.test(search)) params.apaar_masked = search.toUpperCase();
    else if (search) params.q = search;
    return params;
  }

  /** Use a built-in layout only, without animated movement. */
  function arrange() {
    if (!cy?.nodes().length) return;
    cy.layout({ name: byId("graph-layout").value, animate: false, fit: true, padding: 48,
      nodeDimensionsIncludeLabels: true, idealEdgeLength: 110, nodeRepulsion: 9000,
      numIter: 600, avoidOverlap: true, directed: false }).run();
  }

  /** Keep the current selection synchronized between canvas and text controls. */
  function selectElement(id) {
    if (!cy) return;
    cy.elements().unselect();
    cy.getElementById(id).select();
  }

  /** Render every edge's explanation and JSON evidence as accessible text. */
  function showEdge(edge) {
    panelRequest++;
    selectElement(edge.id);
    inspector.setAttribute("aria-busy", "false");
    inspector.replaceChildren(N.el("h3", {}, shortTypes[edge.type] || edge.type), N.el("p", {}, edge.explanation),
      keyValues({ source: labelFor(edge.source), target: labelFor(edge.target), type: edge.type }),
      N.el("h3", {}, "Evidence"), keyValues(edge.evidence || {}));
  }

  /** Resolve graph labels rather than showing opaque IDs in evidence tables. */
  function labelFor(id) {
    return view.nodes.find(node => node.id === id)?.label || id;
  }

  /** Keep related-case links useful before the dedicated case screen is installed. */
  async function casePreview(item, previousNode) {
    const ticket = ++panelRequest;
    inspector.setAttribute("aria-busy", "true");
    inspector.replaceChildren(N.el("p", { role: "status" }, "Loading case..."));
    try {
      const { data } = await N.api(`/api/risk/cases/${encodeURIComponent(item.id)}`);
      if (ticket !== panelRequest) return;
      inspector.replaceChildren(N.el("h3", {}, data.reference), N.el("p", {}, "Case details (read only)"),
        keyValues({ score: data.score, severity: data.severity, status: data.status, reviewer: data.assigned_to || "Unassigned" }),
        N.el("p", {}, data.explanation), N.el("h3", {}, "Recommended next action"), N.el("p", {}, data.recommended_action),
        N.el("button", { type: "button", class: "btn btn-secondary", on: { click: () => void showNode(previousNode) } }, "Back to node"));
    } catch (error) {
      if (ticket === panelRequest) inspector.replaceChildren(N.el("p", {}, error.message || "Unable to load this case."));
    } finally {
      if (ticket === panelRequest) inspector.setAttribute("aria-busy", "false");
    }
  }

  /** Discover the real case page from the shared route inventory, without guessing. */
  function casePageAvailable() {
    try { return JSON.parse(document.querySelector('meta[name="nexus-api-routes"]')?.content || "[]").some(path => path.startsWith("/risk-cases/") && path.includes("<")); }
    catch { return false; }
  }

  /** Fetch the authorized node neighborhood; ignore stale responses after selection changes. */
  async function showNode(node) {
    const ticket = ++panelRequest;
    selectElement(node.id);
    inspector.setAttribute("aria-busy", "true");
    inspector.replaceChildren(N.el("p", { role: "status" }, "Loading inspector..."));
    try {
      const { q, apaar_masked, ...scope } = activeParams;
      const { data } = await N.api(`/api/identity/node/${encodeURIComponent(node.id)}`, { params: scope });
      if (ticket !== panelRequest) return;
      const institutions = new Map(data.institutions.map(item => [item.institution, item.label]));
      const table = N.el("div");
      N.renderTable(table, { columns: [{ key: "full_name", label: "Name" },
        { key: "institution_id", label: "Institution", render: id => institutions.get(id) || "Institution outside this bounded panel" },
        { key: "programme_name", label: "Programme" }, { key: "academic_year", label: "Year" },
        { key: "enrolment_status", label: "Status", render: value => N.statusBadge(value) }],
        rows: data.linked_records, emptyText: "No linked student records." });
      const relationships = N.el("ul", { class: "identity-evidence-list" }, data.edges.map(edge => N.el("li", {},
        N.el("strong", {}, shortTypes[edge.type] || edge.type), N.el("p", {}, edge.explanation),
        N.el("div", { class: "identity-evidence-fields" }, N.el("strong", {}, "Evidence fields"), keyValues(edge.evidence || {})))));
      const cases = N.el("ul", {}, data.linked_cases.map(item => N.el("li", {},
        N.el("a", { href: `/risk-cases/${encodeURIComponent(item.id)}`, on: { click: event => {
          if (!casePageAvailable() && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
            event.preventDefault(); void casePreview(item, node);
          }
        } } }, item.reference), " ", N.statusBadge(item.severity))));
      const { case_ids, ...readableMeta } = data.node.meta;
      inspector.replaceChildren(N.el("h3", {}, data.node.label), keyValues({ type: data.node.type,
        severity: data.node.severity || "Not scored", ...readableMeta }),
        N.el("h3", {}, "Linked records"), table, N.el("h3", {}, "Connected relationships"), relationships,
        N.el("h3", {}, "Related risk cases"), data.linked_cases.length ? cases : N.el("p", {}, "No linked cases."));
      if (data.truncated) inspector.append(N.el("p", { class: "identity-notice" }, "Inspector truncated. Narrow the filters to see a smaller neighborhood."));
    } catch (error) {
      if (ticket !== panelRequest) return;
      inspector.replaceChildren(N.el("p", {}, error.message || "Unable to load this node."));
      if (error.status !== 401) N.toast(error.message || "Unable to load this node.", "danger");
    } finally {
      if (ticket === panelRequest) inspector.setAttribute("aria-busy", "false");
    }
  }

  /** Supply keyboard-accessible Inspect buttons for all visible nodes and edges. */
  function textView() {
    const button = (label, onClick) => N.el("button", { type: "button", class: "btn btn-secondary", on: { click: onClick } }, label);
    N.renderTable(byId("graph-node-list"), { columns: [{ key: "type", label: "Type" }, { key: "label", label: "Label" },
      { key: "severity", label: "Severity", render: value => value ? N.statusBadge(value) : "Not scored" },
      { key: "id", label: "Inspect", render: (id, node) => button(`Inspect ${node.label}`, () => void showNode(node)) }], rows: view.nodes, emptyText: "No nodes to inspect." });
    N.renderTable(byId("graph-edge-list"), { columns: [
      { key: "source", label: "From", render: labelFor }, { key: "target", label: "To", render: labelFor },
      { key: "explanation", label: "Explanation" },
      { key: "id", label: "Inspect", render: (id, edge) => button(`Inspect ${shortTypes[edge.type] || edge.type}`, () => showEdge(edge)) }],
      rows: view.edges, emptyText: "No relationships. Run a scan to build evidence." });
  }

  /** Draw a limited, labelled palette. Text view remains usable if the vendor fails. */
  function draw() {
    cy?.destroy();
    cy = null;
    canvas.replaceChildren();
    if (!view.nodes.length) return;
    if (typeof window.cytoscape !== "function") {
      canvas.append(N.el("p", {}, "Graph renderer unavailable. Use the records and evidence text view below."));
      N.toast("The local Cytoscape library could not be loaded.", "danger");
      return;
    }
    cy = window.cytoscape({ container: canvas, minZoom: 0.15, maxZoom: 3,
      elements: [...view.nodes.map(node => ({ data: node, classes: caseId ? "case-cluster" : "" })),
        ...view.edges.map(edge => ({ data: { ...edge, shortType: shortTypes[edge.type] || edge.type,
          signal: !!edge.evidence?.is_signal || ["SHARED_CONTACT", "SHARED_FEE_PAYER"].includes(edge.type) } }))],
      layout: { name: "preset" }, style: [
        { selector: "node", style: { label: "data(label)", "background-color": "#14315c", "border-color": "#14315c",
          "border-width": 2, width: 42, height: 42, color: "#1d2733", "font-size": 12, "text-valign": "bottom",
          "text-margin-y": 8, "text-wrap": "wrap", "text-max-width": 120, "overlay-opacity": 0 } },
        { selector: 'node[type = "INSTITUTION"]', style: { shape: "rectangle", "background-color": "#61758c", "border-color": "#61758c", width: 44, height: 44 } },
        { selector: 'node[type = "APAAR"]', style: { shape: "diamond", "background-color": "#ffffff", "border-color": "#e8892b", "border-width": 3, width: 52, height: 52 } },
        { selector: 'node[type = "CONTACT"]', style: { shape: "hexagon", "background-color": "#a3adba", "border-color": "#5b6776", width: 28, height: 28 } },
        { selector: 'node[type = "FEE_PAYER"]', style: { shape: "tag", "background-color": "#a3adba", "border-color": "#5b6776", width: 30, height: 26 } },
        { selector: 'node[type = "STUDENT"][severity = "LOW"]', style: { "border-color": "#1a7f4b", "border-width": 3 } },
        { selector: 'node[type = "STUDENT"][severity = "MEDIUM"]', style: { "border-color": "#8a5a11", "border-width": 3 } },
        { selector: 'node[type = "STUDENT"][severity = "HIGH"]', style: { "border-color": "#c0392b", "border-width": 3 } },
        { selector: 'node[type = "STUDENT"][severity = "CRITICAL"]', style: { "border-color": "#c0392b", "border-width": 5 } },
        { selector: "edge", style: { width: 1.5, "line-color": "#61758c", "curve-style": "bezier", "control-point-step-size": 70, label: "data(shortType)",
          "font-size": 11, color: "#1d2733", "text-wrap": "wrap", "text-max-width": 100, "text-rotation": "autorotate",
          "text-background-color": "#ffffff", "text-background-opacity": 1, "text-background-padding": 2, "overlay-opacity": 0 } },
        { selector: "edge[?signal]", style: { "line-style": "dashed", "line-color": "#8793a3" } },
        { selector: "node.case-cluster", style: { "underlay-color": "#2b6cb0", "underlay-opacity": 0.12, "underlay-padding": 6 } },
        { selector: ":selected", style: { "overlay-color": "#14315c", "overlay-opacity": 0.14, "overlay-padding": 6 } }
      ] });
    cy.on("tap", "node", event => void showNode(event.target.data()));
    cy.on("tap", "edge", event => showEdge(event.target.data()));
    cy.on("mouseover", "edge", event => { byId("graph-edge-hint").textContent = event.target.data("explanation"); });
    cy.on("mouseout", "edge", () => { byId("graph-edge-hint").textContent = "Select a node or edge to inspect its evidence."; });
    arrange();
  }

  /** Fetch current filters; sequence checks prevent stale filter responses taking over. */
  async function reload() {
    let params;
    try { params = parameters(); }
    catch (error) { N.toast(error.message, "warn"); return; }
    const ticket = ++graphRequest;
    panelRequest++;
    inspector.setAttribute("aria-busy", "false");
    inspector.replaceChildren(N.el("p", {}, "Select a node or edge to inspect its evidence."));
    byId("graph-status").textContent = "Loading graph...";
    byId("graph-warning").hidden = true;
    canvas.setAttribute("aria-busy", "true");
    graphControls.forEach(button => { button.disabled = true; });
    try {
      const { data } = await N.api("/api/identity/graph", { params });
      if (ticket !== graphRequest) return;
      activeParams = params;
      view = data;
      byId("graph-empty").hidden = !!data.nodes.length;
      canvas.hidden = !data.nodes.length;
      byId("graph-warning").hidden = !data.truncated;
      textView();
      draw();
      byId("graph-status").textContent = `${data.nodes.length} nodes · ${data.edges.length} relationships${caseId ? " · case cluster highlighted" : ""}`;
    } catch (error) {
      if (ticket !== graphRequest) return;
      cy?.destroy(); cy = null;
      view = { nodes: [], edges: [] }; textView();
      canvas.hidden = true;
      byId("graph-empty").hidden = true;
      byId("graph-status").textContent = error.message || "Unable to load the graph. Apply filters to retry.";
      if (error.status !== 401) N.toast(error.message || "Unable to load the graph.", "danger");
    } finally {
      if (ticket === graphRequest) {
        canvas.setAttribute("aria-busy", "false");
        graphControls.forEach(button => { button.disabled = !cy?.nodes().length; });
      }
    }
  }

  form.addEventListener("submit", event => { event.preventDefault(); clearTimeout(searchTimer); void reload(); });
  form.querySelectorAll("select, input[type=checkbox]").forEach(control => control.addEventListener("change", () => { clearTimeout(searchTimer); void reload(); }));
  byId("graph-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => void reload(), 300); });
  byId("graph-layout").addEventListener("change", arrange);
  byId("graph-reset").addEventListener("click", arrange);
  byId("graph-fit").addEventListener("click", () => cy?.fit(undefined, 48));
  const zoom = factor => { if (cy) cy.zoom({ level: Math.max(cy.minZoom(), Math.min(cy.maxZoom(), cy.zoom() * factor)), renderedPosition: { x: canvas.clientWidth / 2, y: canvas.clientHeight / 2 } }); };
  byId("graph-zoom-in").addEventListener("click", () => zoom(1.25));
  byId("graph-zoom-out").addEventListener("click", () => zoom(0.8));
  canvas.addEventListener("keydown", event => {
    if (["+", "=", "-", "0"].includes(event.key)) {
      event.preventDefault();
      if (event.key === "0") cy?.fit(undefined, 48);
      else zoom(event.key === "-" ? 0.8 : 1.25);
    }
  });
  byId("graph-scan")?.addEventListener("click", event => {
    void N.withButtonLock(event.currentTarget, async () => {
      const params = parameters();
      const confirmed = await N.confirmDialog({ title: "Run identity scan", message: "Scan synthetic admission records and update investigation cases for the selected institution and academic year? Student records and credentials will not change.", confirmText: "Run scan" });
      if (!confirmed) return;
      const body = {};
      ["institution_id", "academic_year"].forEach(key => { if (params[key]) body[key] = params[key]; });
      const { data } = await N.api("/api/identity/scan", { method: "POST", body });
      N.toast(`Scan complete: ${data.cases_created} cases created, ${data.cases_updated} updated.`, "ok");
      await reload();
    }).catch(error => { if (error.status !== 401) N.toast(error.message || "Unable to run identity scan.", "danger"); });
  });
  new ResizeObserver(() => cy?.resize()).observe(canvas);
  window.addEventListener("pagehide", () => { clearTimeout(searchTimer); graphRequest++; panelRequest++; cy?.destroy(); });
  window.addEventListener("pageshow", event => { if (event.persisted) void reload(); });
  void reload();
})();
