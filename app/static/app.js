// Medvora dashboard. Plain DOM and fetch, no dependencies, no build step.
// Every value from the API is inserted as text (textContent), never as HTML.
"use strict";

const KEY_STORAGE = "medvora.apiKey";
const state = { key: null, me: null, sources: [], jobs: [] };

const FIELD_LABELS = {
  identifier: "Record id", subject: "Patient id", name: "Name", "name.family": "Family name",
  "name.given": "Given name", birthDate: "Birth date", gender: "Sex", phone: "Phone",
  email: "Email", postalCode: "Postal code", nationalId: "National id",
  insuranceMemberId: "Insurance member no.", code: "Test", value: "Value", unit: "Unit",
  effectiveDateTime: "Taken at", valueQuantity: "Measurement",
};
const FACT_LABELS = {
  family_name: "Family name", given_name: "Given name", middle_initial: "Middle initial",
  birth_date: "Birth date", gender: "Sex", postal_code: "Postal code", phone: "Phone",
  email: "Email", national_id: "National id", insurance_member_id: "Insurance member no.",
};
const OUTCOMES = {
  agree: ["Same", "is-ok"], disagree: ["Different", "is-bad"], missing: ["Not on both", "is-dup"],
  swapped: ["Day and month swapped", "is-hold"], nickname: ["Nickname", "is-ok"],
  close: ["One letter off", "is-hold"],
};
const FINDINGS = {
  CONFLICT_BIRTHDATE: "Birth dates disagree", CONFLICT_GENDER: "Sex disagrees",
  CONFLICT_NAME_FAMILY: "Family names disagree", POSSIBLE_NAMESAKE: "Possible namesake",
  DUPLICATE_RECORD: "Same measurement from two sources", NAME_SPLIT_UNCERTAIN: "Name kept as written",
  FIELD_IGNORED: "Columns ignored", IDENTIFIER_WAS_NUMERIC: "Id sent as a number",
  PHONE_INVALID: "Phone number dropped", EMAIL_INVALID: "Email dropped",
  IDENTIFIER_INVALID: "Id dropped",
};
const JOB_STATUS = {
  pending: ["Waiting for the worker", "is-dup"], running: ["Processing", "is-review"],
  completed: ["Done", "is-ok"], completed_with_errors: ["Done, some rows quarantined", "is-hold"],
  failed: ["Failed", "is-bad"],
};
const LINK_STATUS = {
  pending_verification: ["Waiting for the verifier", "is-dup"], review: ["Needs a person", "is-review"],
  match: ["Same person", "is-ok"], different: ["Different people", "is-bad"],
};
const EXAMPLES = {
  "source-a/Patient": { mrn: "0012345", patient_name: "ROBERT MILLER", dob: "03/07/1984", sex: "M",
    ssn: "912-34-5678", phone: "(555) 201-3344", email: "robert.miller@example.org", zip: "10001" },
  "source-a/Observation": { obs_id: "A-9001-WT", mrn: "0012345", test: "WT", result: "154",
    units: "lbs", taken_at: "10/01/2026 10:30" },
  "source-b/Patient": { patient_id: "B-12345", first_name: "Bob", last_name: "MILLER",
    date_of_birth: "07-03-1984", gender: "Male", member_id: "BR55501234",
    mobile: "+1 555-201-3344", pincode: "10001" },
  "source-b/Observation": { result_id: "B-9001-2", patient_id: "B-12345", test_code: "FBS",
    value: "5.50", uom: "mmol/L", collected_at: "2026-10-01T09:30:00" },
  "source-c/Patient": { hospital_no: "C012345", full_name: "MILLER, Robert", birth_date: "03/04/1984",
    sex_code: "1", ssn: "912-34-5678", tel: "555 201 3344", postcode: "10001" },
  "source-c/Observation": { accession: "C-9001-H", hospital_no: "C012345", loinc: "8302-2",
    value: "180.3", unit: "cm", observed_at: "2026-10-01 07:30" },
};

// ---------- DOM and text helpers ----------

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") node.className = value;
    else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else if (["value", "checked", "disabled", "selected", "hidden"].includes(name)) node[name] = value;
    else node.setAttribute(name, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

const $ = (selector) => document.querySelector(selector);

class Exact {
  constructor(text) { this.text = text; }
  toString() { return this.text; }
  toJSON() { return this.text; }
}

// Keep decimals exactly as the server wrote them (70.10 stays 70.10) where the browser allows.
function parseExact(text) {
  return JSON.parse(text, (key, value, context) =>
    typeof value === "number" && context && context.source && context.source !== String(value)
      ? new Exact(context.source)
      : value);
}

// Indented JSON that writes exact decimals as numbers again (70.10, not "70.10" or 70.1).
function jsonText(value, depth = 0) {
  const pad = "  ".repeat(depth + 1);
  const close = "  ".repeat(depth);
  if (value instanceof Exact) return value.text;
  if (Array.isArray(value)) {
    if (value.length === 0) return "[]";
    const items = value.map((v) => pad + jsonText(v, depth + 1));
    return ["[", items.join(",\n"), `${close}]`].join("\n");
  }
  if (value && typeof value === "object") {
    const entries = Object.entries(value);
    if (entries.length === 0) return "{}";
    const items = entries.map(([k, v]) => `${pad}${JSON.stringify(k)}: ${jsonText(v, depth + 1)}`);
    return ["{", items.join(",\n"), `${close}}`].join("\n");
  }
  return JSON.stringify(value);
}

function brief(value) {
  if (value === null || value === undefined || value === "") return "(empty)";
  if (value instanceof Exact) return value.text;
  if (Array.isArray(value)) return value.map(brief).join(", ");
  if (typeof value === "object") {
    return Object.entries(value).filter(([, v]) => v !== null && v !== undefined)
      .map(([k, v]) => `${k}: ${brief(v)}`).join("; ");
  }
  return String(value);
}

function when(iso) {
  if (!iso) return "";
  const date = new Date(String(iso));
  return Number.isNaN(date.getTime())
    ? String(iso)
    : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function mark(text, tone) { return el("span", { class: `mark ${tone}` }, text); }

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 3500);
}

function recordLink(id, label) {
  return el("a", { href: `#/lookup/${id}` }, label || id);
}

// ---------- API ----------

class ApiError extends Error {
  constructor(status, problem) {
    super((problem && (problem.detail || problem.title)) || `Request failed (${status})`);
    this.status = status;
    this.problem = problem || {};
  }
}

async function api(path, { method = "GET", json, body, params } = {}) {
  const url = new URL(path, location.origin);
  for (const [name, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(name, value);
  }
  const headers = { "X-API-Key": state.key };
  let payload = body;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = typeof json === "string" ? json : JSON.stringify(json);
  }
  const response = await fetch(url, { method, headers, body: payload });
  const text = await response.text();
  let data = null;
  try { data = text ? parseExact(text) : null; } catch { data = { detail: text }; }
  if (!response.ok) throw new ApiError(response.status, data);
  return { status: response.status, data, text };
}

function problemBox(error) {
  if (!(error instanceof ApiError)) {
    return el("div", { class: "problem", role: "alert" }, el("p", {}, el("strong", {}, "Could not reach the server.")),
      el("p", {}, "Check that the API is running, then try again."));
  }
  const problem = error.problem;
  const roleHint = error.status === 403 && state.me
    ? `Your ${state.me.role} key cannot do this.` : null;
  const retry = error.status === 429 ? "Too many requests for this key. Wait a minute and try again." : null;
  return el("div", { class: "problem", role: "alert" },
    el("p", {}, el("strong", {}, `${error.status} ${problem.title || "Request failed"}`)),
    roleHint && el("p", {}, roleHint),
    retry && el("p", {}, retry),
    problem.detail && el("p", {}, problem.detail),
    (problem.errors || []).length > 0 && el("ul", {}, problem.errors.map((e) =>
      el("li", {}, `${FIELD_LABELS[e.field] || e.field}: ${e.code}${e.message ? ` (${e.message})` : ""}`))),
    problem.request_id && el("p", { class: "note" }, "Request id ", el("span", { class: "mono" }, problem.request_id)));
}

const can = {
  ingest: () => state.me && ["ingest", "admin"].includes(state.me.role),
  read: () => state.me && ["reader", "admin"].includes(state.me.role),
  admin: () => state.me && state.me.role === "admin",
};

// ---------- Slips: raw value beside the value it became ----------

function slip(field, sourceField, original, result, rule) {
  return el("div", { class: "slip" },
    el("div", { class: "slip-field" }, FIELD_LABELS[field] || field, sourceField && el("small", {}, sourceField)),
    el("div", { class: "slip-raw", title: "As received" }, brief(original)),
    el("div", { class: "slip-arrow", "aria-hidden": "true" }, "→"),
    el("div", { class: "slip-out", title: "Canonical" }, brief(result)),
    el("div", { class: "slip-rule" }, rule || ""));
}

function slipsFor(transformations) {
  return el("div", { class: "slips" }, transformations.map((t) =>
    slip(t.field, t.source_field, t.original, t.result, `${t.rule} v${t.rule_version}`)));
}

// ---------- Connect ----------

function renderConnect(message) {
  $("#tabs").hidden = true;
  $("#who").hidden = true;
  $("#disconnect").hidden = true;
  const input = el("input", {
    type: "password", id: "key", class: "key-input", autocomplete: "off", spellcheck: "false",
    placeholder: "mvk_…", required: true,
  });
  const form = el("form", {
    onsubmit: async (event) => {
      event.preventDefault();
      await connect(input.value.trim(), form);
    },
  },
  el("div", { class: "field" }, el("label", { for: "key" }, "API key"), input),
  el("button", { class: "button", type: "submit" }, "Connect"));

  const main = $("#main");
  main.replaceChildren(el("section", { class: "hero" },
    el("h1", {}, "See how every hospital record was cleaned up"),
    el("p", { class: "lede" },
      "Upload a patient or lab file, watch each row become one standard format, and check what was held back and why. Paste an API key to open your organisation's records."),
    message && el("div", { class: "problem", role: "alert" }, el("p", {}, message)),
    form,
    el("p", { class: "note" }, "The key stays in this browser tab and is sent only to this server. No key yet? Run ",
      el("code", {}, "docker compose run --rm tools python -m scripts.seed"), "."),
    el("div", { class: "slips", "aria-label": "Examples of what Medvora does" },
      slip("birthDate", "dob, month first", "03/07/1984", "1984-03-07", "date.parse v1"),
      slip("valueQuantity", "result, units", "154 lbs", "69.85 kg", "unit.lb_av_to_kg v1"),
      slip("name", "full_name, family first", "MILLER, Robert", "Robert Miller", "name.parse v1"),
      slip("effectiveDateTime", "taken_at, New York", "10/01/2026 10:30", "2026-10-01T14:30:00Z", "timestamp.to_utc v1"))));
  input.focus();
}

async function connect(key, form) {
  state.key = key;
  const button = form && form.querySelector("button");
  if (button) button.disabled = true;
  try {
    const [me, sources] = await Promise.all([api("/v1/me"), api("/v1/sources")]);
    state.me = me.data;
    state.sources = sources.data.items;
    try { sessionStorage.setItem(KEY_STORAGE, key); } catch { /* private mode: key lives in memory only */ }
    $("#who").replaceChildren(el("strong", {}, state.me.org_name), `, ${state.me.role} key`);
    $("#who").hidden = false;
    $("#disconnect").hidden = false;
    $("#tabs").hidden = false;
    route();
  } catch (error) {
    state.key = null;
    try { sessionStorage.removeItem(KEY_STORAGE); } catch { /* nothing stored */ }
    renderConnect(error instanceof ApiError && error.status === 401
      ? "That key was not accepted. Check it was copied in full."
      : "Could not reach the server. Check that the API is running.");
  } finally {
    if (button) button.disabled = false;
  }
}

function disconnect() {
  state.key = null;
  state.me = null;
  state.jobs = [];
  try { sessionStorage.removeItem(KEY_STORAGE); } catch { /* nothing stored */ }
  location.hash = "";
  renderConnect();
}

// ---------- Overview ----------

async function renderOverview(main) {
  main.append(el("h1", {}, "Overview"));
  if (!can.read()) {
    main.append(el("p", { class: "lede" },
      "This ingest key can send records but not read them back. Use the upload and try-a-record sections, or connect with a reader or admin key to see results."));
    return;
  }
  if (can.admin()) {
    try {
      const { data } = await api("/metrics");
      main.append(ledger(data));
    } catch (error) { main.append(problemBox(error)); }
  } else {
    main.append(el("p", { class: "note" }, "Counts by source need an admin key."));
  }
  main.append(el("h2", {}, "Findings"));
  try {
    const { data } = await api("/v1/findings", { params: { limit: 200 } });
    if (data.items.length === 0) {
      main.append(el("p", { class: "empty" }, "No conflicts or warnings yet. Upload the sample files to see some."));
      return;
    }
    const groups = new Map();
    for (const finding of data.items) {
      if (!groups.has(finding.code)) groups.set(finding.code, []);
      groups.get(finding.code).push(finding);
    }
    main.append(el("p", { class: "note" }, data.next_cursor
      ? "Grouped from the 200 most recent findings."
      : "Grouped by kind, newest first."));
    for (const [code, items] of groups) {
      main.append(el("h3", {}, `${FINDINGS[code] || code} (${items.length})`),
        el("ul", { class: "findings" }, items.slice(0, 3).map((f) => findingItem(f, { showKind: false }))));
    }
  } catch (error) { main.append(problemBox(error)); }
}

function ledger(metrics) {
  const sources = Object.entries(metrics.by_source);
  const totals = { ingested: 0, normalized: 0, duplicate_records: 0, open: 0, superseded: 0, resolved: 0 };
  const rows = sources.map(([source, c]) => {
    totals.ingested += c.ingested; totals.normalized += c.normalized;
    totals.duplicate_records += c.duplicate_records; totals.open += c.quarantine.open;
    totals.superseded += c.quarantine.superseded; totals.resolved += c.quarantine.resolved;
    return el("tr", {}, el("td", {}, source), el("td", { class: "num" }, c.ingested),
      el("td", { class: "num" }, c.normalized), el("td", { class: "num" }, c.duplicate_records),
      el("td", { class: "num" }, c.quarantine.open), el("td", { class: "num" }, c.quarantine.superseded));
  });
  const links = metrics.match_links;
  return el("div", {},
    el("p", { class: "lede" }, "Every received record ends up normalized, recognised as a duplicate measurement, held in quarantine, or replaced by a newer version."),
    sources.length === 0
      ? el("p", { class: "empty" }, "Nothing received yet. Start with a sample file in Upload a file.")
      : el("div", { class: "table-wrap" }, el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, "Source"), el("th", { class: "num" }, "Received"),
          el("th", { class: "num" }, "Normalized"), el("th", { class: "num" }, "Duplicate measurements"),
          el("th", { class: "num" }, "In quarantine"), el("th", { class: "num" }, "Replaced by newer version"))),
        el("tbody", {}, rows),
        el("tfoot", {}, el("tr", {}, el("td", {}, "All sources"), el("td", { class: "num" }, totals.ingested),
          el("td", { class: "num" }, totals.normalized), el("td", { class: "num" }, totals.duplicate_records),
          el("td", { class: "num" }, totals.open), el("td", { class: "num" }, totals.superseded))))),
    totals.resolved > 0 && el("p", { class: "note" }, `${totals.resolved} quarantined record(s) were fixed by reprocessing and are counted as normalized.`),
    el("h2", {}, "Patient matching"),
    el("div", { class: "stat-line" },
      el("span", {}, el("strong", {}, links.match), "linked as the same person"),
      el("span", {}, el("strong", {}, links.review), el("a", { href: "#/review" }, "waiting for a person")),
      el("span", {}, el("strong", {}, links.pending_verification), "waiting for the verifier"),
      el("span", {}, el("strong", {}, links.different), "marked as different people")));
}

function findingItem(finding, { showKind = true } = {}) {
  const details = finding.details || {};
  let text = "";
  if (finding.code.startsWith("CONFLICT_")) {
    text = `${brief(details.value)} in ${details.source}, ${brief(details.other_value)} in ${details.other_source}`;
  } else if (finding.code === "DUPLICATE_RECORD") {
    text = `${details.source} sent ${brief(details.value)} ${details.unit || ""}, already stored from ${details.existing_source}`;
  } else {
    text = details.reason || details.message || "";
  }
  return el("li", {},
    showKind && [mark(FINDINGS[finding.code] || finding.code, finding.kind === "conflict" ? "is-hold" : "is-dup"), " "],
    text, " ", recordLink(finding.record_id, "open record"),
    finding.other_record_id ? [" and ", recordLink(finding.other_record_id, "the other")] : null);
}

// ---------- Upload ----------

function sourceSelect(id, onchange, includeAll) {
  return el("select", { id, onchange },
    includeAll && el("option", { value: "" }, "All sources"),
    state.sources.map((s) => el("option", { value: s.source }, s.source)));
}

function sampleName(source, type) {
  return `${source}-${type === "Patient" ? "patients" : "observations"}.csv`;
}

function renderUpload(main) {
  main.append(el("h1", {}, "Upload a file"),
    el("p", { class: "lede" }, "Send a CSV the way a hospital system would. It uploads as soon as you choose it; rows are processed in the background and the progress shows below."));
  if (!can.ingest()) {
    main.append(el("p", { class: "note" }, "Uploading needs an ingest or admin key."));
  }
  const source = sourceSelect("upload-source", () => updateColumns());
  const type = el("select", { id: "upload-type", onchange: () => updateColumns() },
    el("option", { value: "Patient" }, "Patients"), el("option", { value: "Observation" }, "Lab results"));
  const file = el("input", { type: "file", id: "upload-file", accept: ".csv,text/csv", disabled: !can.ingest() });
  const drop = el("label", { class: "drop", for: "upload-file" },
    file, el("strong", {}, "Choose a CSV file"), " or drop it here", el("br"),
    el("span", { class: "note" }, "It uploads straight away, as the source and record type chosen above."));
  const feedback = el("div", { "aria-live": "polite" });
  const jobs = el("div", { id: "jobs" }, state.jobs.length === 0
    ? el("p", { class: "empty" }, "Nothing uploaded in this session yet.")
    : state.jobs.map((j) => j.node));

  async function send(blob, filename, sourceName, recordType) {
    if (!can.ingest()) return;
    feedback.replaceChildren(el("p", { class: "note" }, `Uploading ${filename} as ${sourceName} ${recordType === "Patient" ? "patients" : "lab results"}…`));
    const data = new FormData();
    data.append("file", blob, filename);
    data.append("source", sourceName);
    data.append("record_type", recordType);
    try {
      const { data: job } = await api("/v1/uploads", { method: "POST", body: data });
      feedback.replaceChildren();
      const tracker = addJob(job);
      tracker.scrollIntoView({ block: "nearest" });
      toast(`Uploaded ${job.filename}: ${job.total_rows} rows queued`);
    } catch (error) { feedback.replaceChildren(problemBox(error)); }
  }

  file.addEventListener("change", () => {
    const chosen = file.files[0];
    // Clear the input, so choosing the same file again uploads it again.
    file.value = "";
    if (chosen) send(chosen, chosen.name, source.value, type.value);
  });
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("is-over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("is-over"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("is-over");
    const dropped = e.dataTransfer.files[0];
    if (dropped) send(dropped, dropped.name, source.value, type.value);
  });

  const form = el("div", {},
    el("div", { class: "row" },
      el("div", { class: "field" }, el("label", { for: "upload-source" }, "Source system"), source),
      el("div", { class: "field" }, el("label", { for: "upload-type" }, "Records in the file"), type)),
    el("div", { class: "field" }, drop), feedback,
    el("h2", {}, "Jobs in this session"), jobs);

  const columns = el("div");
  function updateColumns() {
    const info = state.sources.find((s) => s.source === source.value);
    const spec = info && info.record_types[type.value];
    columns.replaceChildren(
      el("h3", {}, `Columns ${source.value} sends`),
      spec ? el("ul", { class: "columns-list" }, spec.columns.map((c) =>
        el("li", { class: spec.id_columns.includes(c) ? "is-id" : null }, c)))
        : el("p", { class: "note" }, "This source sends no records of this kind."),
      spec && el("p", { class: "note" }, `Bold columns identify the record. Times are read in ${info.timezone}.`),
      info && el("p", { class: "note" }, info.description));
  }
  updateColumns();

  const samples = el("ul", { class: "samples" }, state.sources.flatMap((s) =>
    ["Patient", "Observation"].map((t) => {
      const name = sampleName(s.source, t);
      const url = `/ui/samples/${name}`;
      return el("li", {},
        el("a", { href: url, download: "" }, name),
        can.ingest() && el("button", {
          class: "button button-quiet button-small", type: "button",
          onclick: async () => {
            const response = await fetch(url);
            send(await response.blob(), name, s.source, t);
          },
        }, "Upload"));
    })));

  main.append(el("div", { class: "split" }, form, el("div", { class: "aside" }, columns,
    el("h3", {}, "Sample files"),
    el("p", { class: "note" }, "Synthetic data with a few broken rows; upload one with a click. Upload patients before lab results: the same glucose test from source A and B is recognised as one measurement once the patients are linked. Upload a file twice to see that nothing is stored twice."),
    samples)));
}

function addJob(job, options = {}) {
  const tracker = jobTracker(job, options);
  state.jobs.unshift({ id: job.id, node: tracker });
  const list = $("#jobs");
  if (list) {
    list.querySelector(".empty")?.remove();
    list.prepend(tracker);
  }
  return tracker;
}

function jobTracker(job, { title } = {}) {
  const fill = el("div", { class: "bar-fill" });
  const status = el("span");
  const counts = el("div", { class: "counts" });
  const after = el("div");
  const node = el("section", { class: "job" },
    el("div", { class: "job-head" },
      el("span", { class: "job-name" }, title || job.filename || "Reprocess"),
      el("span", { class: "note" }, [job.source, job.record_type].filter(Boolean).join(", ")), status),
    el("div", { class: "bar", role: "progressbar", "aria-label": "Progress" }, fill), counts, after);

  function update(current) {
    const [text, tone] = JOB_STATUS[current.status] || [current.status, "is-dup"];
    status.replaceChildren(mark(text, tone));
    const total = current.total_rows || 0;
    const percent = total ? Math.round((current.processed_rows / total) * 100) : (current.status === "pending" ? 0 : 100);
    fill.style.width = `${percent}%`;
    node.classList.toggle("is-done", current.status.startsWith("completed"));
    node.classList.toggle("is-failed", current.status === "failed");
    counts.replaceChildren(
      el("span", {}, `${current.processed_rows} of ${total} rows`),
      el("span", { class: "is-ok" }, `${current.normalized_rows} normalized`),
      el("span", { class: "is-hold" }, `${current.failed_rows} quarantined`),
      el("span", { class: "is-dup" }, `${current.duplicate_rows} already received`));
    if (current.status === "completed_with_errors" && current.kind === "upload") {
      after.replaceChildren(el("p", { class: "note" },
        el("a", { href: `#/quarantine?source=${encodeURIComponent(current.source)}` }, "See the quarantined rows")));
    }
    if (current.status === "failed" && current.last_error) {
      after.replaceChildren(el("p", { class: "problem" }, current.last_error));
    }
  }

  async function poll() {
    try {
      const { data } = await api(`/v1/jobs/${job.id}`);
      update(data);
      if (data.status === "pending" || data.status === "running") setTimeout(poll, 900);
    } catch (error) {
      after.replaceChildren(problemBox(error));
    }
  }
  update(job);
  poll();
  return node;
}

// ---------- Try one record ----------

function renderTry(main) {
  main.append(el("h1", {}, "Try one record"),
    el("p", { class: "lede" }, "Edit a record as a source system would send it. Check it to see each value's conversion without saving anything, or save it."));
  const source = sourceSelect("try-source", () => fill());
  const type = el("select", { id: "try-type", onchange: () => fill() },
    el("option", { value: "Patient" }, "Patient"), el("option", { value: "Observation" }, "Lab result"));
  const text = el("textarea", { id: "try-payload", spellcheck: "false", "aria-describedby": "try-help" });
  const result = el("div", { "aria-live": "polite" });
  const help = el("p", { class: "note", id: "try-help" });
  function fill() {
    const example = EXAMPLES[`${source.value}/${type.value}`];
    text.value = example ? JSON.stringify(example, null, 2) : "{}";
    help.textContent = source.value === "source-c" && type.value === "Patient"
      ? "This birth date is ambiguous: source C accepts both day-first and month-first dates, and 03/04 fits both. Check it to see why it would be quarantined."
      : "Change a value, for example the date or the unit, and check again to see how problems are reported.";
    result.replaceChildren();
  }

  async function send(dryRun) {
    result.replaceChildren();
    try { JSON.parse(text.value); } catch {
      result.append(el("div", { class: "problem" }, el("p", {}, "The record is not valid JSON. Check quotes and commas.")));
      return;
    }
    // Rebuild the body as text so numbers keep the precision typed in the editor.
    const body = `{"source":${JSON.stringify(source.value)},"record_type":${JSON.stringify(type.value)},"payload":${text.value}}`;
    try {
      const response = await api(dryRun ? "/v1/validate" : "/v1/records", { method: "POST", json: body });
      result.append(dryRun ? validation(response.data) : saved(response));
    } catch (error) {
      result.append(problemBox(error));
      if (error.status === 422 && error.problem.quarantine_id) {
        result.append(el("p", {}, "The record was kept in ",
          el("a", { href: "#/quarantine" }, "quarantine"), " with these reasons. Fix it and save again: the corrected version settles the quarantined one."));
      }
    }
  }

  main.append(el("div", { class: "row" },
    el("div", { class: "field" }, el("label", { for: "try-source" }, "Source system"), source),
    el("div", { class: "field" }, el("label", { for: "try-type" }, "Record type"), type)),
  el("div", { class: "field" }, el("label", { for: "try-payload" }, "Record as sent"), text, help),
  el("div", { class: "actions" },
    el("button", { class: "button button-quiet", type: "button", onclick: () => send(true) }, "Check without saving"),
    el("button", { class: "button", type: "button", disabled: !can.ingest(), onclick: () => send(false) }, "Save record")),
  result);
  fill();
}

function validation(data) {
  if (!data.valid) {
    return el("div", {}, el("div", { class: "problem" },
      el("p", {}, el("strong", {}, "This record would be quarantined.")),
      el("ul", {}, data.errors.map((e) => el("li", {},
        `${FIELD_LABELS[e.field] || e.field}${e.source_field ? ` (column ${e.source_field})` : ""}: `,
        el("span", { class: "mono" }, e.code), ` ${e.message}`)))),
    warnings(data.warnings));
  }
  return el("div", {},
    el("div", { class: "success" }, el("p", {}, el("strong", {}, "This record is valid."),
      ` Mapping version ${data.mapping_version}. Nothing was saved.`)),
    warnings(data.warnings),
    el("h2", {}, "How each value was converted"),
    slipsFor(data.transformations),
    el("details", {}, el("summary", {}, "Canonical record"),
      el("pre", { class: "json" }, jsonText(data.resource))));
}

function warnings(list) {
  if (!list || list.length === 0) return null;
  return el("ul", { class: "findings" }, list.map((w) => el("li", {},
    mark(FINDINGS[w.code] || w.code, "is-dup"), " ", w.message)));
}

function saved(response) {
  const data = response.data;
  let headline;
  if (data.status === "duplicate_record") headline = "Already stored from another source. Kept as a finding, not as new data.";
  else if (data.duplicate) headline = "Already received. Nothing new was stored.";
  else headline = "Saved and normalized.";
  const id = data.record ? data.record.id : data.duplicate_of;
  return el("div", {},
    el("div", { class: "success" }, el("p", {}, el("strong", {}, `${response.status} `), headline),
      id && el("p", {}, recordLink(id, "Open the record and its lineage"))),
    warnings(data.warnings),
    el("pre", { class: "json" }, jsonText(data)));
}

// ---------- Quarantine ----------

async function renderQuarantine(main, query) {
  main.append(el("h1", {}, "Quarantine"),
    el("p", { class: "lede" }, "Records that could not be trusted, with every reason. Nothing here is lost: a corrected resend or a mapping fix followed by reprocessing settles them."));
  if (!can.read()) { main.append(el("p", { class: "note" }, "Reading the quarantine needs a reader or admin key.")); return; }
  const status = el("select", { id: "q-status" },
    el("option", { value: "open" }, "Open"), el("option", { value: "resolved" }, "Fixed by reprocessing"),
    el("option", { value: "superseded" }, "Replaced by newer version"), el("option", { value: "" }, "Any status"));
  const source = sourceSelect("q-source", null, true);
  source.value = query.get("source") || "";
  const reason = el("input", { type: "text", id: "q-reason", list: "reason-codes", placeholder: "Any reason" });
  const codes = el("datalist", { id: "reason-codes" }, ["DATE_AMBIGUOUS", "DATE_UNPARSEABLE", "DATE_OUT_OF_RANGE",
    "FIELD_MISSING", "CODE_UNMAPPED", "UNIT_UNKNOWN", "UNIT_INCOMPATIBLE", "VALUE_NOT_NUMERIC",
    "TIMESTAMP_UNPARSEABLE", "TIMESTAMP_NONEXISTENT", "TIMESTAMP_AMBIGUOUS", "IDENTIFIER_MISSING",
    "INTERNAL_ERROR"].map((c) => el("option", { value: c })));
  const results = el("div");
  const reprocessArea = el("div");
  let cursor = null;
  let body = null;

  async function load(reset) {
    if (reset) { cursor = null; results.replaceChildren(); body = null; }
    try {
      const { data } = await api("/v1/quarantine", { params: {
        status: status.value, source: source.value, reason_code: reason.value.trim().toUpperCase(), cursor, limit: 25 } });
      if (!body) {
        if (data.items.length === 0) { results.append(el("p", { class: "empty" }, "Nothing matches these filters.")); return; }
        body = el("tbody");
        results.append(el("div", { class: "table-wrap" }, el("table", { class: "stack" },
          el("thead", {}, el("tr", {}, el("th", {}, "First seen"), el("th", {}, "Source"),
            el("th", {}, "Record id in source"), el("th", {}, "Reasons"), el("th", {}, "Status"), el("th", { class: "num" }, "Attempts"))),
          body)));
      }
      data.items.forEach((item) => body.append(...quarantineRows(item)));
      cursor = data.next_cursor;
      results.querySelector(".more")?.remove();
      if (cursor) results.append(el("div", { class: "actions more" },
        el("button", { class: "button button-quiet", type: "button", onclick: () => load(false) }, "Show more")));
    } catch (error) { results.append(problemBox(error)); }
  }

  const reprocess = el("button", {
    class: "button button-quiet", type: "button", hidden: !can.admin(),
    onclick: async () => {
      reprocessArea.replaceChildren();
      const filter = { source: source.value || undefined, reason_code: reason.value.trim().toUpperCase() || undefined };
      if (!filter.source && !filter.reason_code) {
        reprocessArea.append(el("p", { class: "problem" }, "Choose a source or a reason first, so only those records are re-run."));
        return;
      }
      try {
        const { data } = await api("/v1/quarantine/reprocess", { method: "POST", json: filter });
        reprocessArea.append(jobTracker(data, { title: `Reprocessing ${data.total_rows} open record(s) with the current mapping` }));
      } catch (error) { reprocessArea.append(problemBox(error)); }
    },
  }, "Reprocess matching open records");

  main.append(codes, el("form", { onsubmit: (e) => { e.preventDefault(); load(true); } },
    el("div", { class: "row" },
      el("div", { class: "field" }, el("label", { for: "q-status" }, "Status"), status),
      el("div", { class: "field" }, el("label", { for: "q-source" }, "Source"), source),
      el("div", { class: "field" }, el("label", { for: "q-reason" }, "Reason"), reason),
      el("button", { class: "button", type: "submit" }, "Show records"), reprocess)),
  reprocessArea, el("div", { class: "note" }, "Select a row to see each problem."), results);
  load(true);
}

function quarantineRows(item) {
  const toneFor = { open: "is-hold", resolved: "is-ok", superseded: "is-dup" };
  const labelFor = { open: "Open", resolved: "Fixed", superseded: "Replaced" };
  const detail = el("tr", { class: "detail", hidden: true }, el("td", { colspan: 6 },
    el("ul", {}, item.errors.map((e) => el("li", {},
      `${FIELD_LABELS[e.field] || e.field}${e.source_field ? ` (column ${e.source_field})` : ""}: `,
      el("span", { class: "mono" }, e.code), ` ${e.message}`))),
    el("p", { class: "note" }, `Mapping version ${item.mapping_version}. Error class: ${item.error_class}.`),
    item.resolved_record_id && el("p", {}, recordLink(item.resolved_record_id, "Open the record that settled it"))));
  const row = el("tr", {
    class: "is-clickable", tabindex: 0, "aria-expanded": "false",
    onclick: () => toggle(), onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } },
  },
  el("td", { "data-label": "First seen" }, when(item.first_seen_at)),
  el("td", { "data-label": "Source" }, item.source),
  el("td", { class: "mono", "data-label": "Record id in source" }, item.source_record_id || "(none)"),
  el("td", { "data-label": "Reasons" }, el("span", { class: "codes" }, item.reason_codes.map((c) => mark(c, "is-hold")))),
  el("td", { "data-label": "Status" }, mark(labelFor[item.status] || item.status, toneFor[item.status] || "is-dup")),
  el("td", { class: "num", "data-label": "Attempts" }, item.attempts));
  function toggle() {
    detail.hidden = !detail.hidden;
    row.setAttribute("aria-expanded", String(!detail.hidden));
  }
  return [row, detail];
}

// ---------- Match review ----------

async function renderReview(main, query) {
  main.append(el("h1", {}, "Match review"),
    el("p", { class: "lede" }, "Pairs of records that may be the same patient but were not proven: they share no national id, insurance number, phone or email, or the verifier found a contradiction. A person decides; the system never merges on a guess."));
  if (!can.read()) { main.append(el("p", { class: "note" }, "Reviewing matches needs a reader or admin key.")); return; }
  const status = el("select", { id: "r-status", onchange: () => { location.hash = `#/review?status=${status.value}`; } },
    Object.entries(LINK_STATUS).map(([value, [label]]) => el("option", { value }, label)));
  status.value = query.get("status") || "review";
  const list = el("div");
  main.append(el("div", { class: "row" }, el("div", { class: "field" }, el("label", { for: "r-status" }, "Show pairs"), status)), list);
  if (!can.admin()) list.before(el("p", { class: "note" }, "Recording a decision needs an admin key."));
  try {
    const { data } = await api("/v1/matches", { params: { status: status.value, limit: 50 } });
    list.replaceChildren(data.items.length === 0
      ? el("p", { class: "empty" }, status.value === "review" ? "Nothing waiting for a person." : "No pairs with this status.")
      : el("div", {}, data.items.map(pairCard)));
  } catch (error) { list.replaceChildren(problemBox(error)); }
}

function pairCard(link) {
  const [label, tone] = LINK_STATUS[link.status] || [link.status, "is-dup"];
  const facts = link.facts || {};
  const scored = link.breakdown || {};
  const breakdown = Object.keys(FACT_LABELS).filter((field) => field in scored)
    .map((field) => `${scored[field] > 0 ? "+" : ""}${scored[field]} ${FACT_LABELS[field]}`).join(", ");
  const outcome = el("div", { "aria-live": "polite" });
  const card = el("article", { class: "pair" },
    el("div", { class: "pair-head" }, el("span", { class: "points" }, `${link.points} points`), mark(label, tone),
      el("span", { class: "note" }, breakdown)),
    el("div", { class: "pair-records" },
      recordSide(link.record, link.record_id), recordSide(link.candidate, link.candidate_id)),
    el("dl", { class: "facts" }, Object.keys(FACT_LABELS).map((field) => {
      const [text, factTone] = OUTCOMES[facts[field]] || [facts[field] || "", "is-dup"];
      return el("div", {}, el("dt", {}, FACT_LABELS[field]), el("dd", { class: factTone }, text));
    })),
    link.verdict && el("p", { class: "note" }, `Verifier ${link.verdict.verifier}: ${link.verdict.verdict}`,
      link.verdict.contradictions.length ? `, contradicted by ${link.verdict.contradictions.map((f) => FACT_LABELS[f] || f).join(", ")}` : "",
      link.verdict.failure ? ` (${link.verdict.failure})` : ""),
    can.admin() && el("div", { class: "actions" },
      el("button", { class: "button", type: "button", onclick: () => decide("same") }, "Same person"),
      el("button", { class: "button button-quiet", type: "button", onclick: () => decide("different") }, "Different people")),
    outcome);

  async function decide(decision) {
    try {
      const { data } = await api(`/v1/matches/${link.id}/decision`, { method: "POST", json: { decision } });
      const [newLabel, newTone] = LINK_STATUS[data.status];
      card.querySelector(".pair-head .mark").replaceWith(mark(newLabel, newTone));
      outcome.replaceChildren(el("p", { class: "success" },
        decision === "same" ? "Linked as the same person. Conflicting fields are listed as findings."
          : "Kept apart. If they share a name and birth date, a possible-namesake finding was added."));
    } catch (error) { outcome.replaceChildren(problemBox(error)); }
  }
  return card;
}

function recordSide(summary, id) {
  return el("div", {},
    el("div", { class: "note" }, summary ? summary.source : ""),
    el("div", { class: "mono" }, summary ? summary.source_record_id : ""),
    recordLink(id, "Open record"));
}

// ---------- Look up a record ----------

async function renderLookup(main, query, id) {
  main.append(el("h1", {}, "Look up a record"),
    el("p", { class: "lede" }, "Every value of a stored record, beside exactly what the source sent and the rule that changed it."));
  if (!can.read()) { main.append(el("p", { class: "note" }, "Reading records needs a reader or admin key.")); return; }
  const input = el("input", { type: "text", id: "lookup-id", class: "key-input", value: id || "", placeholder: "Record id", spellcheck: "false" });
  main.append(el("form", { onsubmit: (e) => { e.preventDefault(); if (input.value.trim()) location.hash = `#/lookup/${input.value.trim()}`; } },
    el("div", { class: "row" }, el("div", { class: "field" }, el("label", { for: "lookup-id" }, "Record id"), input),
      el("button", { class: "button", type: "submit" }, "Look up"))));
  if (!id) {
    main.append(el("p", { class: "empty" }, "Record ids appear after saving a record, in findings and in match review."));
    return;
  }
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) {
    main.append(el("p", { class: "problem" },
      "That is not a record id. Ids look like 3f2b9c1e-8a4d-4c6b-9f0e-2d7a1b5c8e90; check it was copied in full."));
    return;
  }
  const area = el("div");
  main.append(area);
  try {
    const { data } = await api(`/v1/records/${encodeURIComponent(id)}/lineage`);
    const record = data.record;
    // el() skips null and false children; the DOM's own append would print them as text.
    area.append(el("div", {},
      el("h2", {}, `${record.record_type} from ${record.source}`),
      el("p", {}, el("span", { class: "mono" }, record.source_record_id), "  ",
        mark(record.is_current ? "Current version" : "Older version", record.is_current ? "is-ok" : "is-dup"),
        " ", el("span", { class: "note" }, `Mapping ${data.mapping.source} v${data.mapping.version}, received ${when(record.received_at)}`)),
      warnings(data.warnings.map((f) => ({ code: f.code, message: f.details.message || "" }))),
      el("h3", {}, "How each value was converted"),
      slipsFor(data.transformations),
      el("details", {}, el("summary", {}, "Exactly what the source sent"), el("pre", { class: "json" }, jsonText(data.raw.payload))),
      el("details", {}, el("summary", {}, "Canonical record"), el("pre", { class: "json" }, jsonText(record.resource)))));
    if (record.record_type === "Patient") {
      const { data: group } = await api(`/v1/patients/${encodeURIComponent(id)}/matches`);
      area.append(el("div", {}, el("h3", {}, "Same patient in other records"),
        group.members.length <= 1
          ? el("p", { class: "empty" }, "Not linked to any other record yet.")
          : el("ul", {}, group.members.filter((m) => m.id !== record.id).map((m) =>
            el("li", {}, `${m.source} `, el("span", { class: "mono" }, m.source_record_id), " ", recordLink(m.id, "open")))),
        group.conflicts.length > 0 && el("ul", { class: "findings" }, group.conflicts.map((f) => findingItem(f)))));
    }
  } catch (error) {
    area.append(error instanceof ApiError && error.status === 404
      ? el("p", { class: "problem" }, "No record with this id in your organisation.")
      : problemBox(error));
  }
}

// ---------- Routing ----------

const PAGES = { overview: renderOverview, upload: renderUpload, try: renderTry,
  quarantine: renderQuarantine, review: renderReview, lookup: renderLookup };

function route() {
  if (!state.key) { renderConnect(); return; }
  const [path, search] = (location.hash.replace(/^#\/?/, "") || "overview").split("?");
  const [page, param] = path.split("/");
  const render = PAGES[page] || renderOverview;
  const name = PAGES[page] ? page : "overview";
  document.querySelectorAll("#tabs .tab").forEach((tab) => {
    if (tab.dataset.tab === name) {
      tab.setAttribute("aria-current", "page");
      tab.scrollIntoView({ block: "nearest", inline: "nearest" });
    } else {
      tab.removeAttribute("aria-current");
    }
  });
  const main = $("#main");
  // Each visit renders into its own container. A page still loading when the user moves
  // on keeps writing into its detached container, never into the page now on screen.
  const view = el("div");
  main.replaceChildren(view);
  render(view, new URLSearchParams(search || ""), param ? decodeURIComponent(param) : undefined);
  main.focus({ preventScroll: true });
}

window.addEventListener("hashchange", route);
document.addEventListener("DOMContentLoaded", () => {
  $("#disconnect").addEventListener("click", disconnect);
  let saved = null;
  try { saved = sessionStorage.getItem(KEY_STORAGE); } catch { /* storage unavailable */ }
  if (saved) connect(saved);
  else renderConnect();
});
