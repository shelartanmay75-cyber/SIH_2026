/* Synthetic credential screens; all changes go through the shared CSRF wrapper. */
(() => {
  "use strict";
  const root = document.getElementById("credential-screen");
  if (!root) return;
  const N = window.Nexus;
  const get = id => document.getElementById(id);
  let selected = null;
  let revokeId = null;
  let searchTicket = 0;

  /** Data images are restricted to backend-generated PNGs, never arbitrary URLs. */
  function qrImage(uri) {
    const image = N.el("img", { alt: "Credential verification QR code", width: 200, height: 200, class: "credential-qr" });
    if (/^data:image\/png;base64,[A-Za-z0-9+/]+=*$/.test(uri)) image.src = uri;
    return image;
  }

  /** Render only server-approved summary fields with DOM-safe text. */
  function card(row) {
    const details = N.el("dl", { class: "kv" });
    Object.entries({ Credential: row.reference, Institution: row.institution_name, Programme: row.programme_name,
      "Award year": row.award_year, "Masked APAAR": row.apaar_masked, Status: row.result }).forEach(([key, value]) => details.append(N.el("dt", {}, key), N.el("dd", {}, value)));
    return N.el("div", {}, details, N.statusBadge(row.result), row.qr_data_uri ? qrImage(row.qr_data_uri) : null,
      N.el("p", {}, N.el("a", { href: row.verification_url }, "Open verification page")));
  }

  /** Load a scoped detail; the server computes verification and builds the QR. */
  async function detail(id) { return (await N.api(`/api/credentials/${encodeURIComponent(id)}`)).data; }

  if (root.dataset.view === "passport") {
    void (async () => {
      get("passport-status").textContent = "Loading passport...";
      try {
        const id = root.dataset.credentialId;
        if (id) {
          const row = await detail(id);
          get("passport-profile").replaceChildren(N.el("h2", {}, "Read-only credential"), card(row));
        } else {
          const { data } = await N.api("/api/passport/me");
          const profile = N.el("dl", { class: "kv" });
          Object.entries({ Name: data.profile.full_name, "Masked APAAR": data.profile.apaar_masked,
            Institution: data.profile.institution, Programme: data.profile.programme_name, Year: data.profile.academic_year,
            Enrolment: data.profile.enrolment_status }).forEach(([key, value]) => profile.append(N.el("dt", {}, key), N.el("dd", {}, value)));
          get("passport-profile").replaceChildren(N.el("h2", {}, "My academic record"), profile);
          const rows = await Promise.all(data.credentials.map(row => detail(row.id)));
          get("passport-credentials").replaceChildren(...rows.map(row => N.el("section", { class: "card" }, N.el("h2", {}, row.reference), card(row))));
          if (!rows.length) get("passport-credentials").append(N.el("p", { class: "empty-state" }, "No credentials issued yet."));
        }
        get("passport-status").textContent = "Status checked by the backend.";
      } catch (error) { get("passport-status").textContent = error.message; }
    })();
    return;
  }

  /** Search eligible records with stale-response protection and real pagination. */
  async function students(page = 1) {
    const query = get("credential-search").value.trim();
    if (query.length < 2) {
      get("credential-search-status").textContent = "Enter at least 2 characters of the student name.";
      get("credential-students").replaceChildren();
      get("credential-student-pages").replaceChildren();
      return;
    }
    const ticket = ++searchTicket;
    get("credential-search-status").textContent = "Loading students...";
    try {
      const { data, meta } = await N.api("/api/credentials/eligible-students", { params: { q: query, page } });
      if (ticket !== searchTicket) return;
      N.renderTable(get("credential-students"), { columns: [{ key: "full_name", label: "Name" }, { key: "apaar_masked", label: "Masked APAAR" }, { key: "programme_name", label: "Programme" },
        { key: "enrolment_status", label: "Status", render: N.statusBadge }, { key: "id", label: "Select", render: (id, row) => N.el("button", { type: "button", class: "btn btn-secondary", on: { click: () => {
          selected = row; get("credential-selection").textContent = `${row.full_name} — ${row.programme_name}`;
          get("credential-issue-submit").disabled = false; get("credential-warnings").hidden = true; get("credential-acknowledge").checked = false;
          get("credential-issue-error").textContent = "";
          get("credential-issue-step").scrollIntoView({ behavior: "auto", block: "start" });
        } } }, "Select student") }], rows: data, emptyText: "No eligible students match the search." });
      N.paginate(get("credential-student-pages"), meta, students);
      get("credential-search-status").textContent = `${meta.total} eligible students.`;
    } catch (error) { if (ticket === searchTicket) get("credential-search-status").textContent = error.message; }
  }

  /** Show real issuance history and only authorized human revocation controls. */
  async function history(page = 1) {
    get("credential-history-status").textContent = "Loading issuance history...";
    try {
      const { data, meta } = await N.api("/api/credentials", { params: { page } });
      N.renderTable(get("credential-history"), { columns: [{ key: "reference", label: "Credential", render: (value, row) => N.el("a", { href: `/credentials/${encodeURIComponent(row.id)}` }, value) },
        { key: "programme_name", label: "Programme" }, { key: "award_year", label: "Award year" }, { key: "result", label: "Status", render: N.statusBadge },
        { key: "id", label: "Action", render: (id, row) => row.status_stored === "ISSUED" ? N.el("button", { type: "button", class: "btn btn-secondary", on: { click: () => {
          revokeId = id; get("credential-revoke-form").reset(); get("credential-revoke-error").textContent = ""; N.openDialog("credential-revoke-dialog");
        } } }, "Revoke") : "Revoked" }], rows: data, emptyText: "No credentials issued." });
      N.paginate(get("credential-history-pages"), meta, history);
      get("credential-history-status").textContent = `${meta.total} credentials.`;
    } catch (error) { get("credential-history-status").textContent = error.message; }
  }
  get("credential-search-form").addEventListener("submit", event => { event.preventDefault(); void students(); });
  get("credential-issue-form").addEventListener("submit", event => {
    event.preventDefault();
    if (!selected) return;
    void N.withButtonLock(get("credential-issue-submit"), async () => {
      const student = selected;
      const confirmed = await N.confirmDialog({ title: "Issue credential", message: `Issue a signed synthetic credential for ${student.full_name}?`, confirmText: "Issue credential" });
      if (!confirmed) return;
      try {
        const { data } = await N.api("/api/credentials/issue", { method: "POST", body: { student_id: student.id, award_year: get("credential-year").value,
          valid_years: Number(get("credential-validity").value), acknowledge_warnings: get("credential-acknowledge").checked } });
        const row = await detail(data.id);
        get("credential-issued-card").replaceChildren(card(row)); get("credential-issued").hidden = false;
        get("credential-issue-error").textContent = ""; get("credential-warnings").hidden = true;
        N.toast("Credential issued.", "ok"); await history();
      } catch (error) {
        const warnings = error.details?.warnings;
        if (warnings?.length) {
          get("credential-warnings").hidden = false;
          get("credential-acknowledge").checked = false;
          get("credential-warning-list").replaceChildren(...warnings.map(item => N.el("li", {}, `${item.reference} — ${item.severity} — ${item.status}: ${item.message}`)));
        }
        get("credential-issue-error").textContent = error.message;
      }
    }).catch(error => N.toast(error.message || "Unable to issue credential.", "danger"));
  });
  get("credential-revoke-cancel").addEventListener("click", () => N.closeDialog("credential-revoke-dialog"));
  get("credential-revoke-form").addEventListener("submit", event => {
    event.preventDefault();
    const reason = get("credential-reason").value.trim();
    if (!reason) { get("credential-revoke-error").textContent = "A reason is required."; return; }
    void N.withButtonLock(get("credential-revoke-submit"), async () => {
      await N.api(`/api/credentials/${encodeURIComponent(revokeId)}/revoke`, { method: "POST", body: { reason } });
      N.closeDialog("credential-revoke-dialog"); get("credential-issued").hidden = true;
      N.toast("Credential revoked.", "ok"); await history();
    }).catch(error => { get("credential-revoke-error").textContent = error.message; });
  });
  void history();
})();
