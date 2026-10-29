"use strict";

const TOKEN_KEY = "outreach_admin_token";

const SEND_LABELS = {
  draft: "Черновик",
  approved: "Одобрено",
  rejected: "Отклонено",
  sent: "Отправлено",
  skipped_400: "Отклонено Lichess",
  skipped_429: "Лимит Lichess",
  failed: "Ошибка",
  rate_limited: "Лимит Lichess",
  error: "Ошибка",
};

const CANDIDATE_LABELS = {
  new: "Новые",
  selected: "Отобраны",
  messaged: "Написано",
  joined: "Вступили",
  skipped: "Пропущены",
};

const REACTION_LABELS = {
  none: "Нет ответа",
  replied: "Ответил",
  declined: "Отказался",
  spam_report: "Жалоба на спам",
};

let toastTimer = null;

function $(id) {
  return document.getElementById(id);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function button(label, className) {
  const node = el("button", className, label);
  node.type = "button";
  return node;
}

function addOption(select, value, label) {
  const option = document.createElement("option");
  option.value = value;
  option.textContent = label;
  select.append(option);
}

function getToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

function numberOrNull(value) {
  const trimmed = String(value).trim();
  return trimmed === "" ? null : Number(trimmed);
}

function formatValue(value) {
  return value === null || value === undefined ? "—" : String(value);
}

async function errorDetail(response) {
  try {
    const data = await response.json();
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail)) {
      return data.detail.map((item) => item.msg || JSON.stringify(item)).join("; ");
    }
    return JSON.stringify(data);
  } catch (error) {
    return `Ошибка ${response.status}`;
  }
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = getToken();
  if (token) headers.set("X-Admin-Token", token);
  if (options.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  let response;
  try {
    response = await fetch(`/api${path}`, { ...options, headers });
  } catch (error) {
    throw new Error("Нет связи с сервером");
  }

  if (response.status === 401) {
    openGate();
    throw new Error("Неверный или отсутствующий токен");
  }
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  if (response.status === 204) return null;
  return response.json();
}

function showToast(message, isError = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.toggle("is-error", Boolean(isError));
  toast.classList.remove("is-hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.add("is-hidden"), isError ? 6500 : 3500);
}

function reportError(error) {
  showToast(error && error.message ? error.message : String(error), true);
}

async function runAction(trigger, action) {
  trigger.disabled = true;
  try {
    await action();
  } catch (error) {
    reportError(error);
  } finally {
    trigger.disabled = false;
  }
}

function badgeFor(status) {
  const badge = el("span", "badge", SEND_LABELS[status] || status);
  if (status === "sent") badge.classList.add("is-sent");
  if (status === "failed" || status === "skipped_400" || status === "rejected") {
    badge.classList.add("is-error");
  }
  return badge;
}

/* ---------- Parse ---------- */

function bindParseForm() {
  const form = $("parse-form");
  const status = $("parse-status");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    status.textContent = "Парсим клуб…";

    const payload = {
      club_id: $("parse-club").value.trim(),
      min_rapid: numberOrNull($("parse-min-rapid").value),
      max_rapid: numberOrNull($("parse-max-rapid").value),
      min_games: Number($("parse-min-games").value || 0),
      limit: numberOrNull($("parse-limit").value),
      exclude_own: $("parse-exclude-own").checked,
    };

    try {
      const result = await api("/parse", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      status.textContent = `Готово: обработано ${result.parsed} кандидатов.`;
      showToast(`Распарсено: ${result.parsed}`);
    } catch (error) {
      status.textContent = error.message || String(error);
      reportError(error);
    } finally {
      submit.disabled = false;
    }
  });
}

/* ---------- Review ---------- */

function reviewCard(row) {
  const draft = row.latest_outreach;
  const state = { outreachId: draft ? draft.id : null };

  const card = el("article", "card");
  const head = el("div", "card-head");
  head.append(el("h3", "", row.nick));
  if (draft) head.append(badgeFor(draft.send_status));
  card.append(head);

  card.append(
    el(
      "p",
      "ratings",
      `Rapid: ${formatValue(row.rapid)} · Blitz: ${formatValue(row.blitz)} · ` +
        `Bullet: ${formatValue(row.bullet)} · Партий: ${formatValue(row.games_total)}`,
    ),
  );

  const textarea = el("textarea");
  textarea.value = draft && draft.message_text ? draft.message_text : "";
  textarea.placeholder = "Черновик ещё не сгенерирован";
  card.append(textarea);

  const ratingLabel = el("label", "", "Оценка");
  const rating = document.createElement("select");
  addOption(rating, "", "—");
  for (let value = 1; value <= 5; value += 1) {
    addOption(rating, String(value), String(value));
  }
  rating.value = draft && draft.reviewer_rating ? String(draft.reviewer_rating) : "";
  ratingLabel.append(rating);

  const notesLabel = el("label", "", "Заметки");
  const notes = document.createElement("input");
  notes.type = "text";
  notes.placeholder = "Короткая заметка";
  notes.value = draft && draft.reviewer_notes ? draft.reviewer_notes : "";
  notesLabel.append(notes);

  const fieldRow = el("div", "field-row");
  fieldRow.append(ratingLabel, notesLabel);
  card.append(fieldRow);

  function payload() {
    return {
      message_text: textarea.value,
      reviewer_rating: rating.value ? Number(rating.value) : null,
      reviewer_notes: notes.value.trim() ? notes.value.trim() : null,
    };
  }

  async function ensureOutreach() {
    if (state.outreachId !== null) return state.outreachId;
    const created = await api("/outreach/generate", {
      method: "POST",
      body: JSON.stringify({ candidate_id: row.id }),
    });
    state.outreachId = created.id;
    textarea.value = created.message_text || "";
    return state.outreachId;
  }

  const generateBtn = button("Сгенерировать", "ghost-button");
  const saveBtn = button("Сохранить оценку", "ghost-button");
  const approveBtn = button("Одобрить и отправить", "primary-button wide");
  const rejectBtn = button("Отклонить", "danger-button");
  const copyBtn = button("Скопировать текст", "ghost-button");

  generateBtn.addEventListener("click", () =>
    runAction(generateBtn, async () => {
      const created = await api("/outreach/generate", {
        method: "POST",
        body: JSON.stringify({ candidate_id: row.id }),
      });
      state.outreachId = created.id;
      showToast("Черновик готов");
      await loadReview();
    }),
  );

  saveBtn.addEventListener("click", () =>
    runAction(saveBtn, async () => {
      if (state.outreachId === null) {
        showToast("Сначала сгенерируйте черновик", true);
        return;
      }
      await api(`/outreach/${state.outreachId}`, {
        method: "PATCH",
        body: JSON.stringify(payload()),
      });
      showToast("Оценка сохранена");
    }),
  );

  approveBtn.addEventListener("click", () =>
    runAction(approveBtn, async () => {
      const outreachId = await ensureOutreach();
      await api(`/outreach/${outreachId}`, {
        method: "PATCH",
        body: JSON.stringify(payload()),
      });
      await api(`/outreach/${outreachId}/approve`, { method: "POST" });
      const result = await api(`/outreach/${outreachId}/send`, { method: "POST" });
      if (result.status === "sent") {
        showToast(`Отправлено: ${row.nick}`);
      } else {
        const reason = SEND_LABELS[result.status] || result.status;
        showToast(`Не отправлено (${reason}): ${result.error || "без деталей"}`, true);
      }
      await loadReview();
    }),
  );

  rejectBtn.addEventListener("click", () =>
    runAction(rejectBtn, async () => {
      if (state.outreachId === null) {
        showToast("Сначала сгенерируйте черновик", true);
        return;
      }
      await api(`/outreach/${state.outreachId}/reject`, { method: "POST" });
      showToast("Отклонено");
      await loadReview();
    }),
  );

  copyBtn.addEventListener("click", () =>
    runAction(copyBtn, async () => {
      const text = textarea.value.trim();
      if (!text) {
        showToast("Текст пуст", true);
        return;
      }
      try {
        await navigator.clipboard.writeText(text);
      } catch (error) {
        textarea.select();
        document.execCommand("copy");
      }
      showToast("Скопировано");
    }),
  );

  const actions = el("div", "actions");
  actions.append(generateBtn, saveBtn, approveBtn, rejectBtn, copyBtn);
  card.append(actions);
  return card;
}

async function loadReview() {
  const list = $("review-list");
  list.replaceChildren(el("p", "empty", "Загрузка…"));
  try {
    const rows = await api("/candidates");
    const visible = rows.filter((row) => {
      const draft = row.latest_outreach;
      return !draft || draft.send_status !== "sent";
    });

    if (visible.length === 0) {
      list.replaceChildren(el("p", "empty", "Нет кандидатов для ревью. Сначала запустите парсинг."));
      return;
    }

    list.replaceChildren();
    visible.forEach((row) => list.append(reviewCard(row)));
  } catch (error) {
    list.replaceChildren(el("p", "empty", error.message || String(error)));
    reportError(error);
  }
}

/* ---------- Sent ---------- */

function sentCard(row) {
  const card = el("article", "card");

  const head = el("div", "card-head");
  head.append(el("h3", "", row.nick));
  head.append(badgeFor(row.send_status));
  card.append(head);

  card.append(
    el(
      "p",
      "ratings",
      `Rapid: ${formatValue(row.rapid)} · Blitz: ${formatValue(row.blitz)} · ` +
        `Bullet: ${formatValue(row.bullet)}`,
    ),
  );

  if (row.message_text) {
    const textarea = el("textarea");
    textarea.value = row.message_text;
    textarea.readOnly = true;
    card.append(textarea);
  }

  if (row.send_error) {
    card.append(el("p", "error-text", row.send_error));
  }

  const reactionLabel = el("label", "", "Реакция");
  const reaction = document.createElement("select");
  Object.entries(REACTION_LABELS).forEach(([value, label]) => addOption(reaction, value, label));
  reaction.value = row.reaction || "none";
  reactionLabel.append(reaction);

  const saveBtn = button("Сохранить", "primary-button");
  saveBtn.addEventListener("click", () =>
    runAction(saveBtn, async () => {
      await api(`/outreach/${row.id}`, {
        method: "PATCH",
        body: JSON.stringify({ reaction: reaction.value }),
      });
      showToast("Реакция сохранена");
    }),
  );

  const fieldRow = el("div", "field-row");
  fieldRow.append(reactionLabel, saveBtn);
  card.append(fieldRow);
  return card;
}

async function loadSent() {
  const list = $("sent-list");
  list.replaceChildren(el("p", "empty", "Загрузка…"));
  try {
    const rows = await api("/outreach");
    // Newest first is more useful on a phone.
    rows.reverse();
    if (rows.length === 0) {
      list.replaceChildren(el("p", "empty", "Пока нет сгенерированных приглашений."));
      return;
    }
    list.replaceChildren();
    rows.forEach((row) => list.append(sentCard(row)));
  } catch (error) {
    list.replaceChildren(el("p", "empty", error.message || String(error)));
    reportError(error);
  }
}

/* ---------- Stats ---------- */

function statCard(label, value) {
  const card = el("div", "stat");
  card.append(el("span", "stat-value", value));
  card.append(el("span", "stat-label", label));
  return card;
}

function statList(title, counts, labels) {
  const card = el("div", "stat stat-wide");
  card.append(el("span", "stat-label", title));
  const list = el("ul", "stat-list");
  Object.entries(counts).forEach(([key, value]) => {
    const item = document.createElement("li");
    item.append(el("span", "", labels[key] || key));
    item.append(el("span", "", String(value)));
    list.append(item);
  });
  card.append(list);
  return card;
}

async function loadStats() {
  const panel = $("stats-panel");
  panel.replaceChildren(el("p", "empty", "Загрузка…"));
  try {
    const data = await api("/stats");
    panel.replaceChildren(
      statCard("Кандидаты", String(data.candidates_total)),
      statCard("Приглашения", String(data.outreach_total)),
      statCard("Отправлено", String(data.sent_total)),
      statCard("Вступили", String(data.joined_total)),
      statCard("Конверсия", `${(data.conversion_rate * 100).toFixed(1)}%`),
      statList("Кандидаты по статусам", data.candidates_by_status, CANDIDATE_LABELS),
      statList("Приглашения по статусам", data.outreach_by_send_status, SEND_LABELS),
    );
  } catch (error) {
    panel.replaceChildren(el("p", "empty", error.message || String(error)));
    reportError(error);
  }
}

/* ---------- Analytics ---------- */

async function downloadDataset() {
  // A plain <a href> cannot send X-Admin-Token, so fetch the CSV as a Blob.
  const headers = new Headers();
  const token = getToken();
  if (token) headers.set("X-Admin-Token", token);

  let response;
  try {
    response = await fetch("/api/analytics/export.csv", { headers });
  } catch (error) {
    throw new Error("Нет связи с сервером");
  }

  if (response.status === 401) {
    openGate();
    throw new Error("Неверный или отсутствующий токен");
  }
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }

  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "dataset.csv";
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function bindAnalyticsActions() {
  const refreshBtn = $("refresh-joined");
  const downloadBtn = $("download-csv");
  const status = $("analytics-status");

  refreshBtn.addEventListener("click", () =>
    runAction(refreshBtn, async () => {
      status.textContent = "Проверяем вступивших…";
      const data = await api("/analytics/refresh", { method: "POST" });
      status.textContent =
        `Проверено: ${data.checked} · Новых: ${data.newly_joined} · ` +
        `Всего вступили: ${data.total_joined}`;
      showToast(`Новых вступивших: ${data.newly_joined}`);
      await loadStats();
    }),
  );

  downloadBtn.addEventListener("click", () =>
    runAction(downloadBtn, async () => {
      await downloadDataset();
      status.textContent = "CSV скачан.";
      showToast("CSV скачан");
    }),
  );
}

/* ---------- Tabs and token gate ---------- */

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("is-active", tab.dataset.tab === name);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("is-hidden", panel.id !== `tab-${name}`);
  });
  if (name === "review") loadReview();
  if (name === "sent") loadSent();
  if (name === "stats") loadStats();
}

function openGate() {
  $("token-input").value = getToken();
  $("token-gate").classList.remove("is-hidden");
}

function closeGate() {
  $("token-gate").classList.add("is-hidden");
}

function init() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  });

  $("token-open").addEventListener("click", openGate);
  $("token-save").addEventListener("click", () => {
    localStorage.setItem(TOKEN_KEY, $("token-input").value.trim());
    closeGate();
    switchTab(document.querySelector(".tab.is-active").dataset.tab);
  });
  $("token-skip").addEventListener("click", closeGate);

  bindParseForm();
  bindAnalyticsActions();

  if (getToken()) {
    closeGate();
  } else {
    openGate();
  }
}

init();
