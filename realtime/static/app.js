"use strict";

const $ = (id) => document.getElementById(id);
const labels = { owner: "あなた", muse: "Muse", dots: "dots" };
const initials = { owner: "あ", muse: "M", dots: "d" };
const state = {
  me: null,
  events: null,
  messages: new Map(),
  files: [],
  clientId: null,
  busy: false,
  generation: 0,
  followLatest: true,
};
const bytes = (n) =>
  n < 1024 * 1024
    ? `${Math.max(1, Math.ceil(n / 1024))} KB`
    : `${(n / 1024 / 1024).toFixed(1)} MB`;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "X-Chat-Request": "1", ...options.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const error = new Error(
      typeof body.detail === "string"
        ? body.detail
        : `処理できませんでした (${response.status})`,
    );
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function connection(text, online = false) {
  $("connection").textContent = text;
  $("connection").classList.toggle("online", online);
}

function showLogin() {
  state.generation++;
  state.events?.close();
  state.events = null;
  state.me = null;
  state.messages.clear();
  state.files = [];
  state.clientId = null;
  $("messages").replaceChildren();
  $("pending-files").replaceChildren();
  $("body").value = "";
  $("files").value = "";
  $("send-error").textContent = "";
  $("jump-latest").hidden = true;
  $("room").hidden = true;
  $("login-panel").hidden = false;
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function renderMessage(message) {
  const article = node(
    "article",
    `message${message.author === state.me.author ? " mine" : ""}`,
  );
  article.dataset.id = message.id;
  article.append(
    node("div", `avatar ${message.author}`, initials[message.author]),
  );
  const main = node("div", "message-main");
  const meta = node("div", "message-meta");
  meta.append(node("span", "message-name", labels[message.author]));
  const date = new Date(message.created_at);
  const time = node(
    "time",
    "message-time",
    new Intl.DateTimeFormat("ja-JP", {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date),
  );
  time.dateTime = message.created_at;
  time.title = date.toLocaleString("ja-JP");
  meta.append(time);
  main.append(meta);
  if (message.body) main.append(node("p", "message-body", message.body));
  if (message.attachments.length) {
    const files = node("div", "attachment-list");
    for (const file of message.attachments) {
      const card = node("div", "file-card");
      const url = `/api/attachments/${encodeURIComponent(file.id)}`;
      if (file.media_type.startsWith("image/")) {
        const img = node("img");
        img.src = url;
        img.alt = file.name;
        img.loading = "lazy";
        img.addEventListener("load", () => {
          if (state.followLatest) scrollLatest();
        });
        card.append(img);
      }
      const link = node("a", "", `↓ ${file.name}`);
      link.href = `${url}?download=true`;
      link.download = file.name;
      card.append(link, node("small", "", bytes(file.size)));
      files.append(card);
    }
    main.append(files);
  }
  article.append(main);
  return article;
}

function atBottom() {
  const el = $("timeline");
  return el.scrollHeight - el.scrollTop - el.clientHeight < 80;
}

function scrollLatest() {
  state.followLatest = true;
  $("timeline").scrollTop = $("timeline").scrollHeight;
  $("jump-latest").hidden = true;
}

function addMessages(messages, older = false) {
  const stick = atBottom();
  const previousHeight = $("timeline").scrollHeight;
  for (const message of messages) {
    if (state.messages.has(message.id)) continue;
    state.messages.set(message.id, message);
    const element = renderMessage(message);
    const following = [...$("messages").children].find(
      (child) => Number(child.dataset.id) > message.id,
    );
    $("messages").insertBefore(element, following || null);
  }
  $("empty").hidden = state.messages.size > 0;
  if (older) {
    state.followLatest = false;
    $("timeline").scrollTop += $("timeline").scrollHeight - previousHeight;
  } else if (stick) scrollLatest();
  else if (messages.length) $("jump-latest").hidden = false;
}

async function enterRoom(me) {
  state.me = me;
  const generation = ++state.generation;
  const history = await api("/api/messages");
  if (state.generation !== generation) return;
  $("login-panel").hidden = true;
  $("room").hidden = false;
  $("identity").textContent = `${me.label} として参加中`;
  $("messages").replaceChildren();
  state.messages.clear();
  $("load-more").hidden = !history.has_more;
  addMessages(history.messages);
  scrollLatest();
  connection("接続中…");
  state.events?.close();
  const last = history.messages.at(-1)?.id || 0;
  const events = new EventSource(`/api/events?after=${last}`);
  state.events = events;
  events.onopen = () => {
    if (state.events === events) connection("リアルタイム接続", true);
  };
  events.addEventListener("message", (event) => {
    if (state.events === events) addMessages([JSON.parse(event.data)]);
  });
  events.addEventListener("session-expired", () => {
    if (state.events !== events) return;
    showLogin();
    $("login-error").textContent =
      "接続の有効期限が切れました。再度入室してください。";
  });
  events.onerror = async () => {
    if (state.events !== events) return;
    connection("再接続中…");
    // Native EventSource retries with Last-Event-ID, recovering missed messages.
    try {
      await api("/api/me");
    } catch (error) {
      if (error.status === 401 && state.events === events) showLogin();
    }
  };
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("login-error").textContent = "";
  $("login-button").disabled = true;
  try {
    const me = await api("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: $("token").value.trim() }),
    });
    $("token").value = "";
    await enterRoom(me);
  } catch (error) {
    $("login-error").textContent = error.message;
  } finally {
    $("login-button").disabled = false;
  }
});

$("logout").addEventListener("click", async () => {
  try {
    await api("/api/logout", { method: "POST" });
    showLogin();
  } catch (error) {
    $("send-error").textContent = error.message;
  }
});

$("load-more").addEventListener("click", async () => {
  const button = $("load-more");
  const generation = state.generation;
  button.disabled = true;
  try {
    const before = Math.min(...state.messages.keys());
    const result = await api(`/api/messages?before=${before}`);
    if (generation !== state.generation) return;
    addMessages(result.messages, true);
    button.hidden = !result.has_more;
  } catch (error) {
    $("send-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

function renderFiles() {
  $("pending-files").replaceChildren();
  state.files.forEach((file, index) => {
    const item = node("li");
    item.append(node("span", "", `${file.name} · ${bytes(file.size)}`));
    const remove = node("button", "", "×");
    remove.type = "button";
    remove.disabled = state.busy;
    remove.setAttribute("aria-label", `${file.name} を外す`);
    remove.onclick = () => {
      state.files.splice(index, 1);
      state.clientId = null;
      renderFiles();
    };
    item.append(remove);
    $("pending-files").append(item);
  });
}

function pickFiles(files) {
  if (state.busy) return;
  const next = [...state.files, ...files];
  if (
    next.length > 5 ||
    next.some((f) => f.size > 10 * 1024 * 1024) ||
    next.reduce((sum, f) => sum + f.size, 0) > 20 * 1024 * 1024
  ) {
    $("send-error").textContent =
      "1ファイル10 MB、合計20 MB、最大5件までです。";
    return;
  }
  state.files = next;
  state.clientId = null;
  $("send-error").textContent = "";
  renderFiles();
}

$("attach").onclick = () => $("files").click();
$("files").onchange = () => {
  pickFiles([...$("files").files]);
  $("files").value = "";
};
$("body").oninput = () => {
  state.clientId = null;
};
$("body").addEventListener("keydown", (event) => {
  if (
    event.key === "Enter" &&
    !event.shiftKey &&
    !event.isComposing &&
    event.keyCode !== 229
  ) {
    event.preventDefault();
    if (!state.busy) $("compose").requestSubmit();
  }
});
$("body").addEventListener("paste", (event) => {
  if (event.clipboardData.files.length) {
    event.preventDefault();
    pickFiles([...event.clipboardData.files]);
  }
});
for (const name of ["dragenter", "dragover"])
  $("compose").addEventListener(name, (event) => {
    event.preventDefault();
    $("compose").classList.add("dragging");
  });
$("compose").addEventListener("dragleave", (event) => {
  if (!$("compose").contains(event.relatedTarget))
    $("compose").classList.remove("dragging");
});
$("compose").addEventListener("drop", (event) => {
  event.preventDefault();
  $("compose").classList.remove("dragging");
  pickFiles([...event.dataTransfer.files]);
});
// Keep drops outside the composer from navigating away and losing a draft.
window.addEventListener("dragover", (event) => event.preventDefault());
window.addEventListener("drop", (event) => event.preventDefault());
$("jump-latest").onclick = scrollLatest;
$("timeline").addEventListener("scroll", () => {
  state.followLatest = atBottom();
  if (atBottom()) $("jump-latest").hidden = true;
});

$("compose").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy) return;
  $("send-error").textContent = "";
  const body = $("body").value.trim();
  if (!body && !state.files.length) {
    $("send-error").textContent = "本文またはファイルを追加してください。";
    return;
  }
  state.clientId ||= crypto.randomUUID();
  const form = new FormData();
  form.set("body", body);
  form.set("client_id", state.clientId);
  state.files.forEach((file) => form.append("files", file));
  state.busy = true;
  const generation = state.generation;
  $("send").textContent = "送信中…";
  for (const id of ["send", "attach", "body", "logout"]) $(id).disabled = true;
  renderFiles();
  try {
    const message = await api("/api/messages", { method: "POST", body: form });
    if (generation !== state.generation) return;
    addMessages([message]);
    $("body").value = "";
    state.files = [];
    state.clientId = null;
    scrollLatest();
  } catch (error) {
    if (generation === state.generation) {
      $("send-error").textContent = `${error.message} 入力内容は残っています。`;
      if (error.status === 401) connection("再入室が必要です");
    }
  } finally {
    state.busy = false;
    $("send").textContent = "送信 ↑";
    for (const id of ["send", "attach", "body", "logout"])
      $(id).disabled = false;
    renderFiles();
  }
});

(async () => {
  const generation = state.generation;
  try {
    const me = await api("/api/me");
    if (generation === state.generation) await enterRoom(me);
  } catch (error) {
    if (generation !== state.generation) return;
    showLogin();
    if (error.status !== 401)
      $("login-error").textContent =
        "サーバーに接続できません。時間をおいて再度お試しください。";
  }
})();
