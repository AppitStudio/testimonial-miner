const state = {
  items: [],
  filtered: [],
  selectedId: null,
  status: "all",
  app: "all",
  review: "all",
  problemFree: false,
  search: "",
  sort: "quality",
  visible: 80,
};

const $ = (selector) => document.querySelector(selector);
const listEl = $("#result-list");
const detailEl = $("#detail-pane");
const resultCountEl = $("#result-count");
const showMoreEl = $("#show-more");
const toastEl = $("#toast");
let toastTimer;

const escapeHtml = (value = "") => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

const clamp = (value, min = 0, max = 1) => Math.min(max, Math.max(min, Number(value) || 0));
const percent = (value) => `${Math.round(clamp(value) * 100)}%`;
const score = (value, digits = 2) => Number(value || 0).toFixed(digits);

function displayDate(value, withTime = false) {
  if (!value) return "Unknown date";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(0, 10);
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric", month: "short", day: "numeric",
    ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  }).format(date);
}

function showToast(message) {
  toastEl.textContent = message;
  toastEl.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove("visible"), 1800);
}

function populateSelect(element, values, firstLabel) {
  const options = [`<option value="all">${escapeHtml(firstLabel)}</option>`];
  for (const value of values) {
    options.push(`<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`);
  }
  element.innerHTML = options.join("");
}

function renderMetrics(items) {
  const candidates = items.filter((item) => item.status === "candidate").length;
  const borderline = items.filter((item) => item.status === "borderline").length;
  const strong = items.filter((item) => Number(item.praise_quality) >= 2).length;
  const clean = items.filter((item) => Number(item.mentions_problem) < .5).length;
  const average = items.length
    ? items.reduce((sum, item) => sum + Number(item.praise_quality || 0), 0) / items.length
    : 0;

  $("#metric-candidates").textContent = candidates.toLocaleString();
  $("#metric-borderline").textContent = borderline.toLocaleString();
  $("#metric-strong").textContent = strong.toLocaleString();
  $("#metric-clean").textContent = clean.toLocaleString();
  $("#quality-average").textContent = `${average.toFixed(2)} avg`;

  const bins = Array.from({ length: 12 }, () => 0);
  for (const item of items) {
    const index = Math.min(11, Math.max(0, Math.floor((Number(item.praise_quality || 1) - 1) / 2 * 12)));
    bins[index] += 1;
  }
  const maximum = Math.max(...bins, 1);
  $("#quality-bars").innerHTML = bins.map((count) =>
    `<i class="quality-bar" style="height:${Math.max(8, count / maximum * 100)}%" title="${count} testimonials"></i>`
  ).join("");
}

function searchableText(item) {
  return [item.quote, item.body, item.from_name, item.from_email, item.subject, item.app, item.kind]
    .filter(Boolean).join(" ").toLocaleLowerCase();
}

function applyFilters({ resetVisible = true } = {}) {
  const query = state.search.trim().toLocaleLowerCase();
  let result = state.items.filter((item) => {
    if (state.status !== "all" && item.status !== state.status) return false;
    if (state.app !== "all" && item.app !== state.app) return false;
    if (state.review !== "all" && item.review !== state.review) return false;
    if (state.problemFree && Number(item.mentions_problem) >= .5) return false;
    if (query && !searchableText(item).includes(query)) return false;
    return true;
  });

  const sorters = {
    quality: (a, b) => Number(b.praise_quality || 0) - Number(a.praise_quality || 0)
      || Number(b.has_praise || 0) - Number(a.has_praise || 0),
    praise: (a, b) => Number(b.has_praise || 0) - Number(a.has_praise || 0)
      || Number(b.praise_quality || 0) - Number(a.praise_quality || 0),
    newest: (a, b) => String(b.date || "").localeCompare(String(a.date || "")),
    app: (a, b) => String(a.app || "").localeCompare(String(b.app || ""))
      || Number(b.praise_quality || 0) - Number(a.praise_quality || 0),
  };
  result.sort(sorters[state.sort]);
  state.filtered = result;
  if (resetVisible) state.visible = 80;
  if (!result.some((item) => item.id === state.selectedId)) {
    state.selectedId = result[0]?.id ?? null;
  }
  renderResults();
  renderDetail();
}

function itemCard(item) {
  const selected = item.id === state.selectedId;
  const sender = item.from_name || item.from_email || "Unknown sender";
  const quote = item.quote || "No quote sentence was selected.";
  const issue = Number(item.mentions_problem) >= .5;
  return `
    <button class="result-card${selected ? " selected" : ""}" type="button"
      data-id="${escapeHtml(item.id)}" role="option" aria-selected="${selected}">
      <span class="card-top">
        <span class="chip chip-${escapeHtml(item.status)}">${escapeHtml(item.status)}</span>
        <span class="chip">${escapeHtml(item.app || "unclear")}</span>
        ${issue ? '<span class="problem-mark">Issue mentioned</span>' : ""}
      </span>
      <span class="result-quote">“${escapeHtml(quote)}”</span>
      <span class="card-meta">
        <span class="sender">${escapeHtml(sender)}</span>
        <span class="meta-dot"></span>
        <span>${escapeHtml(displayDate(item.date))}</span>
        <span class="quality-score">Q ${score(item.praise_quality, 1)}</span>
      </span>
    </button>`;
}

function renderResults() {
  resultCountEl.textContent = state.filtered.length.toLocaleString();
  const shown = state.filtered.slice(0, state.visible);
  listEl.innerHTML = shown.length
    ? shown.map(itemCard).join("")
    : '<div class="empty-results"><div><strong>No testimonials match</strong><br>Try clearing a filter or using a broader search.</div></div>';
  showMoreEl.hidden = state.visible >= state.filtered.length;
  if (!showMoreEl.hidden) {
    showMoreEl.textContent = `Show ${Math.min(80, state.filtered.length - state.visible)} more`;
  }
}

function meterCard(label, value, valueLabel, problem = false) {
  return `<div class="score-card">
    <span>${escapeHtml(label)}</span>
    <strong>${escapeHtml(valueLabel)}</strong>
    <div class="meter${problem ? " problem" : ""}"><i style="width:${percent(value)}"></i></div>
  </div>`;
}

function renderDetail() {
  const item = state.items.find((entry) => entry.id === state.selectedId);
  if (!item) {
    detailEl.innerHTML = `<div class="empty-detail"><div class="empty-mark" aria-hidden="true">“</div>
      <h2>No testimonial selected</h2><p>Adjust the filters to find something to inspect.</p></div>`;
    return;
  }
  const sender = item.from_name || "Unknown sender";
  const email = item.from_email || "No email";
  const reasons = (item.reasons || []).length
    ? `<div class="section-heading"><h2>Decision notes</h2></div><div class="reason-list">${item.reasons.map((reason) => `<span class="reason">${escapeHtml(reason)}</span>`).join("")}</div>`
    : "";
  detailEl.innerHTML = `<div class="detail-content">
    <div class="detail-header">
      <div class="detail-chips">
        <span class="chip chip-${escapeHtml(item.status)}">${escapeHtml(item.status)}</span>
        <span class="chip">${escapeHtml(item.app || "unclear")}</span>
        <span class="chip">${escapeHtml(item.review || "pending")}</span>
        <span class="chip">${escapeHtml((item.kind || "other").replaceAll("_", " "))}</span>
      </div>
      <button type="button" class="copy-button" id="copy-quote">
        <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M15 9V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h3"/></svg>
        <span>Copy quote</span>
      </button>
    </div>
    <blockquote class="hero-quote">“${escapeHtml(item.quote || "No quote sentence was selected.")}”</blockquote>
    <div class="attribution">
      <strong>${escapeHtml(sender)}</strong><span>·</span><span>${escapeHtml(email)}</span><span>·</span><span>${escapeHtml(displayDate(item.date))}</span>
    </div>
    <p class="detail-subject">Subject: ${escapeHtml(item.subject || "No subject")}</p>

    <div class="score-grid">
      ${meterCard("Quality", Number(item.praise_quality) / 3, `${score(item.praise_quality, 2)} / 3`)}
      ${meterCard("Praise", item.has_praise, percent(item.has_praise))}
      ${meterCard("Sender is user", item.sender_is_user, percent(item.sender_is_user))}
      ${meterCard("Problem signal", item.mentions_problem, percent(item.mentions_problem), true)}
    </div>

    ${reasons}
    <div class="section-heading"><h2>Original cleaned email</h2><span>${item.body_truncated ? "Trimmed to model limit" : "Complete cleaned body"}</span></div>
    <pre class="email-body">${escapeHtml(item.body || "No cleaned body was stored.")}</pre>
  </div>`;

  $("#copy-quote").addEventListener("click", () => copyQuote(item));
}

async function copyQuote(item) {
  const sender = item.from_name || item.from_email || "Customer";
  const text = `“${item.quote || ""}” — ${sender}`;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  showToast("Quote copied");
}

function clearFilters() {
  state.status = "all";
  state.app = "all";
  state.review = "all";
  state.problemFree = false;
  state.search = "";
  state.sort = "quality";
  $("#search").value = "";
  $("#app-filter").value = "all";
  $("#review-filter").value = "all";
  $("#problem-filter").checked = false;
  $("#sort-filter").value = "quality";
  document.querySelectorAll("[data-status]").forEach((button) => {
    const active = button.dataset.status === "all";
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  applyFilters();
}

function bindControls() {
  $("#search").addEventListener("input", (event) => {
    state.search = event.target.value;
    applyFilters();
  });
  $("#app-filter").addEventListener("change", (event) => {
    state.app = event.target.value;
    applyFilters();
  });
  $("#review-filter").addEventListener("change", (event) => {
    state.review = event.target.value;
    applyFilters();
  });
  $("#problem-filter").addEventListener("change", (event) => {
    state.problemFree = event.target.checked;
    applyFilters();
  });
  $("#sort-filter").addEventListener("change", (event) => {
    state.sort = event.target.value;
    applyFilters();
  });
  $("#status-filter").addEventListener("click", (event) => {
    const button = event.target.closest("[data-status]");
    if (!button) return;
    state.status = button.dataset.status;
    document.querySelectorAll("[data-status]").forEach((candidate) => {
      const active = candidate === button;
      candidate.classList.toggle("active", active);
      candidate.setAttribute("aria-pressed", String(active));
    });
    applyFilters();
  });
  listEl.addEventListener("click", (event) => {
    const card = event.target.closest("[data-id]");
    if (!card) return;
    state.selectedId = card.dataset.id;
    renderResults();
    renderDetail();
    if (window.innerWidth < 900) detailEl.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  listEl.addEventListener("keydown", (event) => {
    if (!["ArrowDown", "ArrowUp"].includes(event.key) || !state.filtered.length) return;
    event.preventDefault();
    const current = Math.max(0, state.filtered.findIndex((item) => item.id === state.selectedId));
    const next = event.key === "ArrowDown"
      ? Math.min(state.filtered.length - 1, current + 1)
      : Math.max(0, current - 1);
    state.selectedId = state.filtered[next].id;
    if (next >= state.visible) state.visible += 80;
    renderResults();
    renderDetail();
    listEl.querySelector(`[data-id="${CSS.escape(state.selectedId)}"]`)?.scrollIntoView({ block: "nearest" });
  });
  showMoreEl.addEventListener("click", () => {
    state.visible += 80;
    renderResults();
  });
  $("#clear-filters").addEventListener("click", clearFilters);
  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) {
      event.preventDefault();
      $("#search").focus();
    }
  });
}

async function init() {
  bindControls();
  try {
    const response = await fetch("/api/testimonials", { cache: "no-store" });
    if (!response.ok) throw new Error(`Server returned ${response.status}`);
    const payload = await response.json();
    if (payload.error) throw new Error(payload.error);
    state.items = Array.isArray(payload.items) ? payload.items : [];
    $("#updated-at").textContent = payload.updated_at
      ? `Updated ${displayDate(payload.updated_at, true)}`
      : "No scan data yet";

    const apps = [...new Set(state.items.map((item) => item.app || "unclear"))]
      .sort((a, b) => a.localeCompare(b));
    const reviews = [...new Set(state.items.map((item) => item.review || "pending"))]
      .sort((a, b) => a.localeCompare(b));
    populateSelect($("#app-filter"), apps, "All apps");
    populateSelect($("#review-filter"), reviews, "Any review");
    renderMetrics(state.items);
    applyFilters();
  } catch (error) {
    listEl.innerHTML = `<div class="empty-results"><div><strong>Could not load the database</strong><br>${escapeHtml(error.message)}</div></div>`;
    detailEl.innerHTML = `<div class="empty-detail"><div class="empty-mark">!</div><h2>Dashboard unavailable</h2><p>Check that testimonials.json exists and reload this page.</p></div>`;
    $("#updated-at").textContent = "Load failed";
  }
}

init();
