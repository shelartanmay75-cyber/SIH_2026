/* APAAR Nexus shared browser helpers. No dependencies or build step. */
(() => {
  "use strict";
  if (window.Nexus) return;

  const kinds = new Set(["ok", "warn", "danger", "info", "muted"]);
  const lockedButtons = new WeakSet();
  const filterCleanups = new WeakMap();
  let filterSequence = 0;
  let confirmation = null;
  let settleConfirmation = null;
  let redirecting = false;
  const missing = "—";

  /** A typed API failure; never logs payloads, tokens or server response bodies. */
  class ApiError extends Error {
    constructor(code, message, details = {}, status = 0) {
      super(message);
      this.name = "ApiError";
      this.code = code;
      this.details = details;
      this.status = status;
    }
  }

  /** Resolve a CSS selector or an existing DOM container. */
  function containerNode(container) {
    const node = typeof container === "string" ? document.querySelector(container) : container;
    if (!(node instanceof Element)) throw new TypeError("A valid container is required.");
    return node;
  }

  /** Escape plain text when a caller needs an HTML-safe string. Prefer el(). */
  function escapeHTML(value) {
    return String(value ?? "").replace(/[&<>"']/g, char => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[char]);
  }

  /** Build a node with text/Node children; on:{click: fn} adds event listeners. */
  function el(tag, attrs = {}, ...children) {
    if (!/^[a-z][a-z0-9-]*$/i.test(tag) || /^(script|style|iframe|object|embed)$/i.test(tag)) {
      throw new TypeError("Use a non-executable HTML element.");
    }
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined) continue;
      if (key === "on") {
        for (const [event, listener] of Object.entries(value)) node.addEventListener(event, listener);
      } else if (key === "dataset") {
        for (const [name, item] of Object.entries(value)) node.dataset[name] = String(item);
      } else if (/^on/i.test(key) || /^(innerhtml|outerhtml|srcdoc|style)$/i.test(key)) {
        throw new TypeError("HTML, inline handlers and inline style are not supported.");
      } else {
        if (["href", "src", "action", "formaction"].includes(key.toLowerCase())) {
          const url = new URL(String(value), window.location.href);
          if (!["http:", "https:", "mailto:", "tel:"].includes(url.protocol)) throw new TypeError("Unsafe URL.");
        }
        if (key === "className" || key === "class") node.className = String(value);
        else if (key === "value") node.value = String(value);
        else if (typeof value === "boolean" && !key.startsWith("aria-") && !key.startsWith("data-")) {
          if (value) node.setAttribute(key, "");
        } else node.setAttribute(key, String(value));
      }
    }
    for (const child of children.flat(Infinity)) {
      if (child !== null && child !== undefined && child !== false) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  /** Redirect an expired session to the local login page, preserving a local next URL. */
  function requireLogin() {
    if (!redirecting) {
      redirecting = true;
      window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
    }
  }

  /** Fetch same-origin API envelopes; raw=true returns a successful Response for downloads. */
  async function api(path, { method = "GET", body, params = {}, raw = false } = {}) {
    if (typeof path !== "string" || !path.trim()) throw new ApiError("INVALID_URL", "Use a valid local API URL.");
    let url;
    try { url = new URL(path, window.location.origin); }
    catch { throw new ApiError("INVALID_URL", "Use a valid local API URL."); }
    if (!["http:", "https:"].includes(url.protocol) || url.origin !== window.location.origin || url.username || url.password) {
      throw new ApiError("INVALID_URL", "API requests must use the same origin.");
    }
    url.hash = "";
    for (const [key, value] of Object.entries(params || {})) {
      const values = (Array.isArray(value) ? value : [value]).filter(item => item !== null && item !== undefined && String(item).trim() !== "");
      if (!values.length) continue;
      url.searchParams.delete(key);
      values.forEach(item => url.searchParams.append(key, String(item)));
    }
    method = String(method).toUpperCase();
    const headers = { Accept: "application/json" };
    if (method !== "GET") {
      const token = document.querySelector('meta[name="csrf-token"]')?.content;
      if (!token) throw new ApiError("CSRF_MISSING", "Refresh this page before submitting changes.");
      headers["X-CSRFToken"] = token;
    }
    const options = { method, headers, credentials: "same-origin" };
    if (body !== undefined) {
      if (["GET", "HEAD"].includes(method)) throw new ApiError("INVALID_REQUEST", "GET and HEAD requests cannot have a body.");
      if (body instanceof FormData) options.body = body;
      else {
        headers["Content-Type"] = "application/json";
        try { options.body = JSON.stringify(body); }
        catch { throw new ApiError("INVALID_REQUEST", "The request body cannot be encoded as JSON."); }
      }
    }
    let response;
    try { response = await fetch(url.href, options); }
    catch { throw new ApiError("NETWORK_ERROR", "Unable to reach the server. Check your connection and try again."); }
    if (response.status === 401 || (response.redirected && new URL(response.url).pathname === "/login")) {
      let error = {};
      try {
        const unauthorized = await response.clone().json();
        if (unauthorized?.ok === false) error = unauthorized.error || {};
      } catch { /* Login HTML and empty 401 responses use the safe defaults. */ }
      requireLogin();
      throw new ApiError(error.code || "UNAUTHENTICATED", error.message || "Please sign in to continue.", error.details || {}, 401);
    }
    if ([204, 205].includes(response.status) || (method === "HEAD" && response.ok)) {
      return raw ? response : { ok: true, data: {} };
    }
    let envelope;
    if (raw && response.ok && !response.headers.get("Content-Type")?.includes("json")) return response;
    try { envelope = await (raw ? response.clone() : response).json(); }
    catch {
      throw new ApiError(response.ok ? "INVALID_RESPONSE" : `HTTP_${response.status}`, "The server returned an unexpected response.", {}, response.status);
    }
    if (envelope?.ok === false) {
      const error = envelope.error || {};
      throw new ApiError(error.code || `HTTP_${response.status}`, error.message || "The request could not be completed.", error.details || {}, response.status);
    }
    if (!response.ok) throw new ApiError(`HTTP_${response.status}`, "The request could not be completed.", {}, response.status);
    if (raw) return response;
    if (envelope?.ok !== true || !Object.prototype.hasOwnProperty.call(envelope, "data")) {
      throw new ApiError("INVALID_RESPONSE", "The server returned an invalid API envelope.", {}, response.status);
    }
    return envelope;
  }

  /** Add dismissal, a pausable timeout, and a short fade to one notification. */
  function prepareToast(node, ms, previousFocus) {
    let remaining = Number.isFinite(ms) && ms > 0 ? ms : 0;
    let timer = null;
    let started = 0;
    let leaving = false;
    let hovered = false;
    const pause = () => {
      if (timer !== null) { clearTimeout(timer); remaining = Math.max(0, remaining - (Date.now() - started)); timer = null; }
    };
    const dismiss = () => {
      if (leaving) return;
      leaving = true;
      pause();
      const hadFocus = node.contains(document.activeElement);
      node.classList.add("is-leaving");
      const remove = () => {
        node.remove();
        if (hadFocus && previousFocus?.isConnected) previousFocus.focus();
      };
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) remove();
      else setTimeout(remove, 150);
    };
    const resume = () => {
      if (remaining > 0 && !hovered && !node.contains(document.activeElement) && !leaving && timer === null) {
        started = Date.now();
        timer = setTimeout(dismiss, remaining);
      }
    };
    node.append(el("button", { type: "button", class: "toast-close", "aria-label": "Dismiss notification", on: { click: dismiss } }, "Dismiss"));
    node.addEventListener("mouseenter", () => { hovered = true; pause(); });
    node.addEventListener("mouseleave", () => { hovered = false; resume(); });
    node.addEventListener("focusin", pause);
    node.addEventListener("focusout", () => setTimeout(resume, 0));
    resume();
    return node;
  }

  /** Stack an accessible notification; ms=0 keeps it until explicitly dismissed. */
  function toast(message, type = "info", ms = 4000) {
    const kind = kinds.has(type) ? type : "info";
    let container = document.getElementById("toast-container");
    if (!container) {
      container = el("div", { id: "toast-container", class: "toast-container", "aria-live": "polite", "aria-atomic": "false" });
      document.body.append(container);
    }
    const node = el("div", { class: `toast toast-${kind}`, role: "status" }, el("span", {}, message));
    container.append(node);
    return prepareToast(node, ms, document.activeElement);
  }

  /** Open an existing native dialog without registering duplicate handlers. */
  function openDialog(id) {
    const dialog = document.getElementById(id);
    if (!(dialog instanceof HTMLDialogElement)) throw new TypeError("A dialog element with this ID is required.");
    if (!dialog.open) { dialog.returnValue = ""; dialog.showModal(); }
    return dialog;
  }

  /** Close an existing native dialog, safely doing nothing if already closed. */
  function closeDialog(id) {
    const dialog = document.getElementById(id);
    if (!(dialog instanceof HTMLDialogElement)) throw new TypeError("A dialog element with this ID is required.");
    if (dialog.open) dialog.close("cancel");
  }

  /** Confirm with one lazy native dialog; overlapping requests resolve false. */
  function confirmDialog({ title = "Confirm action", message = "", confirmText = "Confirm", danger = false } = {}) {
    if (settleConfirmation) return Promise.resolve(false);
    if (!confirmation) {
      confirmation = el("dialog", { id: "nexus-confirm-dialog", "aria-labelledby": "nexus-confirm-title", "aria-describedby": "nexus-confirm-message" },
        el("h2", { id: "nexus-confirm-title" }), el("p", { id: "nexus-confirm-message" }),
        el("form", { method: "dialog", class: "actions" },
          el("button", { type: "submit", class: "btn btn-secondary", value: "cancel", autofocus: true }, "Cancel"),
          el("button", { type: "submit", class: "btn", value: "confirm", id: "nexus-confirm-submit" }, "Confirm")));
      document.body.append(confirmation);
      confirmation.addEventListener("close", () => {
        const settle = settleConfirmation;
        settleConfirmation = null;
        if (settle) settle(confirmation.returnValue === "confirm");
      });
    }
    document.getElementById("nexus-confirm-title").textContent = title;
    document.getElementById("nexus-confirm-message").textContent = message;
    const submit = document.getElementById("nexus-confirm-submit");
    submit.textContent = confirmText;
    submit.className = danger ? "btn btn-danger" : "btn";
    return new Promise((resolve, reject) => {
      settleConfirmation = resolve;
      try { openDialog(confirmation.id); }
      catch (error) { settleConfirmation = null; reject(error); }
    });
  }

  /** Make a text-only badge; unknown visual kinds use muted. */
  function badge(text, kind = "muted") {
    return el("span", { class: `badge badge-${kinds.has(kind) ? kind : "muted"}` }, text ?? missing);
  }

  /** Map contract statuses/actions to text badges. Unknown enums remain neutral. */
  function statusBadge(value) {
    const status = String(value ?? "").toUpperCase();
    const mapping = {
      LOW: "ok", MEDIUM: "warn", HIGH: "danger", CRITICAL: "danger",
      VERIFIED: "ok", VALID: "ok", INVALID: "danger", REVOKED: "danger", EXPIRED: "warn", ISSUED: "ok",
      ACTIVE: "ok", WITHDRAWN: "muted", GRADUATED: "ok", PENDING: "info", FLAGGED: "danger",
      PREVIEWED: "info", COMMITTED: "ok", PARTIAL: "warn", REJECTED: "danger", ACCEPTED: "ok",
      OPEN: "info", UNDER_REVIEW: "warn", CLARIFICATION_REQUESTED: "warn", DATA_ERROR_CONFIRMED: "danger",
      FALSE_POSITIVE: "ok", RESOLVED: "ok", REQUEST_CLARIFICATION: "warn", CONFIRM_DATA_ERROR: "danger",
      DISMISS_FALSE_POSITIVE: "ok", RESOLVE: "ok"
    };
    return badge(value == null || value === "" ? missing : String(value).replace(/_/g, " "), mapping[status] || "muted");
  }

  /** Render safe cells; custom render(value, row) may return a DOM node or plain text. */
  function renderTable(container, { columns, rows = [], emptyText = "No records found." }) {
    if (!Array.isArray(columns) || !columns.length || !Array.isArray(rows)) throw new TypeError("Table columns and rows must be arrays; at least one column is required.");
    const table = el("table", { class: "table" });
    const header = el("tr");
    const body = el("tbody");
    columns.forEach(column => header.append(el("th", { scope: "col" }, column.label)));
    table.append(el("thead", {}, header), body);
    for (const row of rows) {
      const tr = el("tr");
      for (const column of columns) {
        const value = row[column.key];
        const content = column.render ? column.render(value, row) : value;
        tr.append(el("td", { class: ["left", "center", "right"].includes(column.align) ? `align-${column.align}` : "" }, content ?? missing));
      }
      body.append(tr);
    }
    if (!rows.length) body.append(el("tr", {}, el("td", { colspan: columns.length, class: "empty-state" }, emptyText)));
    containerNode(container).replaceChildren(el("div", { class: "table-wrapper", role: "region", "aria-label": "Scrollable data table", tabindex: "0" }, table));
    return table;
  }

  /** Prevent repeat submits; restore the disabled/aria-busy state even after failure. */
  async function withButtonLock(button, asyncFn) {
    if (!(button instanceof HTMLButtonElement)) throw new TypeError("A button element is required.");
    if (lockedButtons.has(button) || button.disabled) return undefined;
    const busy = button.getAttribute("aria-busy");
    lockedButtons.add(button);
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try { return await asyncFn(); }
    finally {
      lockedButtons.delete(button);
      button.disabled = false;
      if (busy === null) button.removeAttribute("aria-busy");
      else button.setAttribute("aria-busy", busy);
    }
  }

  /** Render previous/next controls, clamping empty or stale page metadata. */
  function paginate(container, meta, onPage) {
    const integer = (value, fallback) => Number.isSafeInteger(Number(value)) ? Number(value) : fallback;
    const total = Math.max(0, integer(meta?.total, 0));
    const perPage = Math.max(1, integer(meta?.per_page, 25));
    const pages = Math.max(1, Math.ceil(total / perPage));
    const page = Math.min(pages, Math.max(1, integer(meta?.page, 1)));
    const button = (label, target, disabled) => {
      const node = el("button", { type: "button", class: "btn btn-secondary", disabled }, label);
      node.addEventListener("click", () => { void withButtonLock(node, () => onPage(target)).catch(error => toast(error.message || "Unable to change page.", "danger")); });
      return node;
    };
    const nav = el("nav", { class: "pagination", "aria-label": "Table pages" },
      el("span", { "aria-live": "polite" }, `Page ${page} of ${pages} (${total} records)`),
      el("div", { class: "actions" }, button("Previous", page - 1, page <= 1), button("Next", page + 1, page >= pages)));
    containerNode(container).replaceChildren(nav);
    return nav;
  }

  /** Build labelled filters; setValues is silent, text input is debounced 250ms. */
  function filterBar(container, fields, onChange = () => {}) {
    const host = containerNode(container);
    filterCleanups.get(host)?.();
    let timer = null;
    let disposed = false;
    const controls = new Map();
    const form = el("form", { class: "filter-bar" });
    const prefix = `nexus-filter-${++filterSequence}`;
    const getValues = () => Object.fromEntries(Array.from(controls, ([key, control]) => [key, control.value]));
    const notify = () => {
      clearTimeout(timer);
      timer = null;
      Promise.resolve().then(() => { if (!disposed) return onChange(getValues()); }).catch(error => toast(error.message || "Unable to update filters.", "danger"));
    };
    for (const [index, field] of fields.entries()) {
      if (!field.name || controls.has(field.name)) throw new TypeError("Filter names must be unique and non-empty.");
      const type = field.type || "text";
      if (!["select", "text", "date"].includes(type)) throw new TypeError("Use select, text or date filters.");
      const id = `${prefix}-${index}`;
      const control = type === "select" ? el("select", { id, name: field.name, class: "select" }) : el("input", { id, name: field.name, type, class: "input", placeholder: field.placeholder || "" });
      if (type === "select") {
        const options = field.options || [];
        const pairs = options.map(option => typeof option === "object" ? option : { value: option, label: option });
        if (!pairs.some(option => String(option.value) === "")) control.append(el("option", { value: "" }, field.placeholder || "All"));
        pairs.forEach(option => control.append(el("option", { value: option.value }, option.label)));
      }
      controls.set(field.name, control);
      control.addEventListener(type === "text" ? "input" : "change", () => {
        clearTimeout(timer);
        if (type === "text") timer = setTimeout(notify, 250);
        else notify();
      });
      form.append(el("div", { class: "form-row" }, el("label", { for: id }, field.label), control));
    }
    const setValues = (values = {}) => {
      clearTimeout(timer);
      timer = null;
      controls.forEach((control, key) => { control.value = values[key] == null ? "" : String(values[key]); });
    };
    form.append(el("button", { type: "submit", class: "btn" }, "Apply filters"), el("button", { type: "reset", class: "btn btn-secondary" }, "Clear"));
    form.addEventListener("submit", event => { event.preventDefault(); notify(); });
    form.addEventListener("reset", event => { event.preventDefault(); setValues(); notify(); });
    host.replaceChildren(form);
    const destroy = () => {
      disposed = true;
      clearTimeout(timer);
      form.remove();
      if (filterCleanups.get(host) === destroy) filterCleanups.delete(host);
    };
    filterCleanups.set(host, destroy);
    return { getValues, setValues, destroy };
  }

  /** Parse a date without shifting a date-only value into a different timezone. */
  function dateValue(value) {
    if (value === null || value === undefined || value === "") return null;
    const text = String(value);
    if (/^\d{4}-\d{2}-\d{2}$/.test(text)) {
      const [year, month, day] = text.split("-").map(Number);
      const date = new Date(year, month - 1, day);
      return date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day ? date : null;
    }
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  /** Format a valid ISO date in the browser's timezone, using en-IN. */
  function fmtDate(iso) {
    const date = dateValue(iso);
    return date ? new Intl.DateTimeFormat("en-IN", { dateStyle: "medium" }).format(date) : missing;
  }

  /** Format a valid ISO timestamp in the browser's timezone, using en-IN. */
  function fmtDateTime(iso) {
    const date = dateValue(iso);
    return date ? new Intl.DateTimeFormat("en-IN", { dateStyle: "medium", timeStyle: "short" }).format(date) : missing;
  }

  const numeric = value => (typeof value === "number" || (typeof value === "string" && value.trim() !== "")) && Number.isFinite(Number(value)) ? Number(value) : null;

  /** Format finite counts with Indian digit grouping; missing/invalid values use an em dash. */
  function fmtNumber(value) {
    const number = numeric(value);
    return number === null ? missing : new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 }).format(number);
  }

  /** Format a percentage on a 0–100 scale: 80 means 80%, not 8,000%. */
  function fmtPercent(value) {
    const number = numeric(value);
    return number === null ? missing : new Intl.NumberFormat("en-IN", { style: "percent", maximumFractionDigits: 1 }).format(number / 100);
  }

  /** Check the server-published route inventory before requesting optional modules. */
  function routeAvailable(path) {
    try { return JSON.parse(document.querySelector('meta[name="nexus-api-routes"]')?.content || "[]").includes(path); }
    catch { return false; }
  }

  /** Load authorized options from an available module; missing/failed modules return []. */
  async function loadOptions(path, params, area) {
    if (!routeAvailable(path)) { toast(`${area} filters are not available yet.`, "warn"); return []; }
    try {
      const { data } = await api(path, { params });
      const rows = Array.isArray(data) ? data : data?.[`${area.toLowerCase()}s`] || data?.items || data?.rows || data?.summary;
      if (!Array.isArray(rows)) throw new ApiError("INVALID_RESPONSE", `The server did not return ${area.toLowerCase()} filter options.`);
      const options = new Map();
      for (const row of rows) {
        const programme = area === "Programme";
        const value = programme ? row.programme_id ?? row.programme?.id ?? row.id : row.id;
        const label = programme ? row.programme_name ?? row.programme?.name ?? row.name : row.name;
        if (value != null && label != null) options.set(String(value), { value: String(value), label: String(label) });
      }
      if (rows.length && !options.size) throw new ApiError("INVALID_RESPONSE", `The server did not return usable ${area.toLowerCase()} filter options.`);
      return Array.from(options.values());
    } catch (error) {
      if (error.status !== 401) toast(error.message || `Unable to load ${area.toLowerCase()} filters.`, "warn");
      return [];
    }
  }

  /** Optional, scoped option loaders; params can include page and institution_id. */
  const loadFilters = Object.freeze({
    institutions: (params = {}) => loadOptions("/api/institutions", { per_page: 100, ...params }, "Institution"),
    programmes: (params = {}) => loadOptions("/api/seats/summary", params, "Programme")
  });

  window.Nexus = Object.freeze({ ApiError, api, toast, confirmDialog, openDialog, closeDialog,
    renderTable, paginate, filterBar, badge, statusBadge, fmtDate, fmtDateTime,
    fmtNumber, fmtPercent, escapeHTML, el, withButtonLock, loadFilters });

  const enhanceShell = () => {
    document.querySelectorAll("#toast-container .toast").forEach(node => {
      if (!node.querySelector(".toast-close")) prepareToast(node, 4000, document.activeElement);
    });
    const toggle = document.querySelector(".sidebar-menu-toggle");
    const menu = document.getElementById("sidebar-menu");
    const syncMenu = () => {
      if (!toggle || !menu) return;
      if (window.matchMedia("(max-width: 899px)").matches) {
        menu.hidden = toggle.getAttribute("aria-expanded") !== "true";
      } else {
        menu.hidden = false;
      }
    };
    syncMenu();
    window.addEventListener("resize", syncMenu);
    toggle?.addEventListener("click", () => {
      const expanded = toggle.getAttribute("aria-expanded") === "true";
      toggle.setAttribute("aria-expanded", String(!expanded));
      menu.hidden = expanded;
    });
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", enhanceShell, { once: true });
  else enhanceShell();
})();
