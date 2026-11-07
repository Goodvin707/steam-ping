const state = {
  status: null,
  friends: [],
  events: [],
  selectedId: null,
  signature: "",
};

const $ = (selector) => document.querySelector(selector);

function formatTime(unix) {
  if (!unix) return "ещё не было";
  const label = new Intl.DateTimeFormat("ru-RU", {
    timeZone: "Europe/Moscow",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(unix * 1000));
  return `${label} МСК`;
}

function formatInterval(seconds) {
  if (seconds % 3600 === 0) return `каждые ${seconds / 3600} ч`;
  if (seconds % 60 === 0) return `каждые ${seconds / 60} мин`;
  return `каждые ${seconds} с`;
}

function toast(text) {
  const node = $("#toast");
  node.textContent = text;
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    node.hidden = true;
  }, 4200);
}

async function readError(response) {
  try {
    const data = await response.json();
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail)) {
      return data.detail.map((item) => item.msg || "проверь поле").join(", ");
    }
  } catch (_error) {
    /* тело не JSON */
  }
  return "Запрос не прошёл.";
}

function chip(text, kind) {
  const node = document.createElement("span");
  node.className = `chip${kind ? ` ${kind}` : ""}`;
  node.textContent = text;
  return node;
}

function hideBrokenImage(image) {
  image.addEventListener("error", () => image.remove());
}

function renderChips(status) {
  const root = $("#chips");
  root.replaceChildren();
  root.append(
    chip(
      status.steam_configured ? "Steam: ключ есть" : "Steam: без ключа",
      status.steam_configured ? "ok" : "warn",
    ),
  );
  if (status.discord_connected) {
    root.append(chip(`Discord: ${status.bot_user}`, "ok"));
  } else if (status.discord_error) {
    root.append(chip("Discord: токен не подошёл", "bad"));
  } else if (status.discord_configured) {
    root.append(chip("Discord: подключаюсь", "warn"));
  } else if (status.webhook_configured) {
    root.append(chip("Discord: вебхук", "ok"));
  } else {
    root.append(chip("Discord: без канала", "warn"));
  }
  const delivery = {
    discord: "Автоотправка в канал",
    webhook: "Автоотправка вебхуком",
    panel: "Автоотправка выключена",
  }[status.delivery];
  root.append(chip(delivery, status.delivery === "panel" ? "" : "ok"));
  root.append(chip(formatInterval(status.poll_interval_seconds)));
  if (status.last_poll_at) {
    root.append(chip(`сверка ${formatTime(status.last_poll_at)}`));
  }
  const guilds = status.guilds || [];
  if (guilds.length > 1) {
    root.append(chip(`Серверов: ${guilds.length}`));
  }
}

function renderGuilds(status) {
  const root = $("#guilds");
  if (!root) return;
  root.replaceChildren();
  const extra = (status.guilds || []).filter((guild) => !guild.shared_with_panel);
  if (!extra.length) return;
  const note = document.createElement("p");
  note.className = "form-note";
  note.textContent = "У других серверов свои списки и свои снимки. Здесь их не видно и сюда их покупки не приходят.";
  root.append(note);
  for (const guild of extra) {
    const row = document.createElement("p");
    row.className = "guild-row";
    const channel = guild.notify_channel_id ? "канал выбран" : "канал не выбран";
    row.textContent = `${guild.name}: профилей ${guild.live_friends}, ${channel}`;
    root.append(row);
  }
}

function renderBanners(status) {
  const root = $("#banners");
  root.replaceChildren();
  const messages = [status.discord_error, status.last_poll_error].filter(Boolean);
  for (const message of messages) {
    const banner = document.createElement("div");
    banner.className = "banner";
    banner.textContent = message;
    root.append(banner);
  }
}

function renderFriends(friends, status) {
  const root = $("#friends");
  root.replaceChildren();
  if (!friends.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = status.demo_mode
      ? "Пока никого. Нажми «Показать пример» — появится Алекс и карточка, как её увидит канал."
      : "Пока никого. Вставь ссылку на профиль или добавь WATCH_STEAM_IDS в .env и перезапусти процесс.";
    root.append(empty);
    return;
  }
  for (const friend of friends) {
    const row = document.createElement("article");
    row.className = "friend";

    const avatar = document.createElement("img");
    avatar.alt = "";
    avatar.src = friend.avatar_url || "/static/demo-avatar.svg";
    avatar.addEventListener("error", () => {
      if (avatar.dataset.fallback === "1") return;
      avatar.dataset.fallback = "1";
      avatar.src = "/static/demo-avatar.svg";
    });

    const text = document.createElement("div");
    const title = document.createElement("h3");
    title.textContent = friend.persona_name;
    if (friend.is_demo) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "пример";
      title.append(badge);
    }
    const meta = document.createElement("p");
    if (friend.is_demo) {
      meta.textContent = "В Steam не ходит, нужен только для образца карточки.";
    } else if (friend.baseline_done) {
      meta.textContent = `${friend.game_count} игр в снимке · ${formatTime(friend.last_checked_at)}`;
    } else {
      meta.textContent = friend.last_checked_at
        ? `Снимок ещё не готов · ${formatTime(friend.last_checked_at)}`
        : "Снимок ещё не сделан.";
    }
    text.append(title, meta);
    if (friend.last_error) {
      const error = document.createElement("p");
      error.className = "error";
      error.textContent = friend.last_error;
      text.append(error);
    }
    if (friend.profile_url) {
      const link = document.createElement("a");
      link.href = friend.profile_url;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = "Профиль";
      text.append(link);
    }

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "linkish";
    remove.textContent = "Убрать";
    remove.addEventListener("click", () => removeFriend(friend));

    row.append(avatar, text, remove);
    root.append(row);
  }
}

function deliveryCaption(event, status) {
  if (event.kind === "note") return "Заметка только на панели, в канал не уходит.";
  if (event.is_demo) return "Пример. В Discord это сообщение не отправляю.";
  if (event.discord_sent === 1) return "Ушло в Discord.";
  if (status.delivery === "panel") return "Канал не подключён, событие дождётся доставки.";
  return "Ждёт отправки в Discord.";
}

function renderPreview(event, status) {
  const root = $("#preview");
  root.replaceChildren();
  if (!event) {
    const box = document.createElement("div");
    box.className = "placeholder";
    box.textContent = "Здесь появится карточка в том виде, в каком её увидит Discord.";
    root.append(box);
    return;
  }

  const frame = document.createElement("article");
  frame.className = "discord";
  const channel = document.createElement("div");
  channel.className = "discord-channel";
  const hash = document.createElement("span");
  hash.textContent = "#";
  channel.append(hash, document.createTextNode("библиотека"));

  const msg = document.createElement("div");
  msg.className = "msg";
  const avatar = document.createElement("img");
  avatar.className = "avatar";
  avatar.alt = "";
  avatar.src = "/static/bot.svg";
  const body = document.createElement("div");
  const meta = document.createElement("div");
  meta.className = "meta";
  const name = document.createElement("b");
  name.textContent = "Steam Ping";
  const tag = document.createElement("span");
  tag.className = "bot-tag";
  tag.textContent = "БОТ";
  const time = document.createElement("time");
  time.textContent = formatTime(event.detected_at);
  meta.append(name, tag, time);

  const content = document.createElement("p");
  content.className = "content";
  content.textContent = event.kind === "note"
    ? `${event.persona_name}: ${event.name}.`
    : `${event.persona_name} — в библиотеке Steam новая игра ${event.name}.`;

  body.append(meta, content);

  if (event.kind === "game") {
    const embed = document.createElement("div");
    embed.className = "embed";
    const bar = document.createElement("div");
    bar.className = "embed-bar";
    const inner = document.createElement("div");
    inner.className = "embed-body";

    const kicker = document.createElement("div");
    kicker.className = "embed-kicker";
    if (event.avatar_url) {
      const face = document.createElement("img");
      face.alt = "";
      face.src = event.avatar_url;
      hideBrokenImage(face);
      kicker.append(face);
    }
    kicker.append(document.createTextNode(`${event.persona_name} · библиотека Steam`));

    const title = document.createElement("a");
    title.className = "embed-title";
    title.href = event.store_url || "#";
    title.target = "_blank";
    title.rel = "noopener";
    title.textContent = event.name;

    const desc = document.createElement("p");
    desc.className = "embed-desc";
    desc.textContent = event.short_description || "Страница магазина открывается по заголовку.";

    const fields = document.createElement("dl");
    fields.className = "embed-fields";
    for (const [label, value] of [
      ["Что произошло", "Не было в прошлом снимке"],
      ["Цена на витрине", event.price_text || "нет на витрине"],
    ]) {
      const wrap = document.createElement("div");
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value;
      wrap.append(dt, dd);
      fields.append(wrap);
    }

    const footer = document.createElement("div");
    footer.className = "embed-footer";
    footer.textContent = "Steam Ping · игра появилась в библиотеке";
    inner.append(kicker, title, desc, fields);
    if (event.header_image) {
      const art = document.createElement("img");
      art.className = "art";
      art.alt = `Обложка ${event.name}`;
      art.src = event.header_image;
      hideBrokenImage(art);
      inner.append(art);
    }
    inner.append(footer);
    embed.append(bar, inner);
    body.append(embed);
  } else {
    const note = document.createElement("p");
    note.className = "embed-desc";
    note.textContent = event.short_description || "";
    body.append(note);
  }

  const caption = document.createElement("p");
  caption.className = "delivery";
  caption.textContent = deliveryCaption(event, status);
  body.append(caption);
  msg.append(avatar, body);
  frame.append(channel, msg);
  root.append(frame);
}

function renderFeed(events, status) {
  const root = $("#feed");
  root.replaceChildren();
  if (!events.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = status.demo_mode
      ? "Лента пустая. «Показать пример» собирает карточку без ключей Steam и Discord."
      : "Лента пустая. После первой сверки здесь будет заметка, что список игр запомнен. Дальше — только новые игры.";
    root.append(empty);
    return;
  }
  for (const event of events) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `event${event.id === state.selectedId ? " is-selected" : ""}`;
    if (event.header_image && event.kind === "game") {
      const thumb = document.createElement("img");
      thumb.className = "event-thumb";
      thumb.alt = "";
      thumb.src = event.header_image;
      hideBrokenImage(thumb);
      button.append(thumb);
    } else {
      const spacer = document.createElement("div");
      spacer.className = "event-thumb";
      button.append(spacer);
    }
    const text = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = event.name;
    const meta = document.createElement("small");
    const kind = event.kind === "note" ? "заметка" : event.persona_name;
    meta.textContent = `${kind} · ${formatTime(event.detected_at)}`;
    text.append(title, meta);
    button.append(text);
    button.addEventListener("click", () => {
      state.selectedId = event.id;
      state.signature = "";
      render();
    });
    root.append(button);
  }
}

function renderAuto(status) {
  const stateNode = $("#auto-state");
  if (stateNode) stateNode.textContent = status.auto_label || "";
  const clear = $("#webhook-clear");
  if (clear) clear.hidden = !status.webhook_configured;
  const input = $("#webhook-url");
  if (!input) return;
  input.placeholder = status.webhook_configured
    ? "Вебхук сохранён. Вставь новый адрес, чтобы заменить"
    : "https://discord.com/api/webhooks/…";
}

function renderInvite(status) {
  const slot = $("#invite");
  slot.replaceChildren();
  if (!status.invite_url) {
    slot.textContent = "Ссылка на приглашение появится, когда в .env будет DISCORD_TOKEN.";
    return;
  }
  const link = document.createElement("a");
  link.href = status.invite_url;
  link.target = "_blank";
  link.rel = "noopener";
  link.textContent = "Пригласить бота на сервер";
  slot.append(link);
}

function render() {
  const { status, friends, events } = state;
  if (!status) return;
  if (!events.some((event) => event.id === state.selectedId)) {
    const preferred = events.find((event) => event.kind === "game") || events[0];
    state.selectedId = preferred ? preferred.id : null;
  }
  const signature = JSON.stringify({
    status,
    friends,
    events,
    selectedId: state.selectedId,
  });
  if (signature === state.signature) return;
  state.signature = signature;

  renderChips(status);
  renderBanners(status);
  renderAuto(status);
  renderFriends(friends, status);
  renderGuilds(status);
  renderFeed(events, status);
  renderPreview(events.find((event) => event.id === state.selectedId) || null, status);
  renderInvite(status);

  $("#simulate").hidden = !status.demo_mode;
  $("#test-delivery").hidden = !(status.discord_configured || status.webhook_configured);
  const locked = !status.steam_configured;
  $("#steam-query").disabled = locked;
  $("#watch-submit").disabled = locked;
  $("#form-note").textContent = locked
    ? "Поле включится после STEAM_API_KEY и перезапуска. Пример выше работает и без ключа."
    : "Первая сверка запоминает текущие игры и молчит. Сообщение уйдёт на следующую новую игру.";
}

async function refresh() {
  const responses = await Promise.all([
    fetch("/api/status"),
    fetch("/api/friends"),
    fetch("/api/events"),
  ]);
  if (responses.some((response) => !response.ok)) {
    throw new Error("panel");
  }
  const [status, friends, events] = await Promise.all(responses.map((response) => response.json()));
  state.status = status;
  state.friends = friends;
  state.events = events;
  render();
}

async function removeFriend(friend) {
  const response = await fetch("/api/friends/remove", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query: friend.steam_id }),
  });
  if (!response.ok) {
    toast(await readError(response));
    return;
  }
  toast(`${friend.persona_name} больше не в списке.`);
  state.signature = "";
  await refresh();
}

async function withBusy(button, action) {
  button.disabled = true;
  try {
    await action();
  } finally {
    if (button.id === "watch-submit") {
      button.disabled = !state.status?.steam_configured;
    } else {
      button.disabled = false;
    }
  }
}

$("#simulate").addEventListener("click", () => withBusy($("#simulate"), async () => {
  const response = await fetch("/api/demo/purchase", { method: "POST" });
  if (!response.ok) {
    toast(await readError(response));
    return;
  }
  const event = await response.json();
  state.selectedId = event.id;
  state.signature = "";
  await refresh();
  toast(`В примере появилась ${event.name}.`);
  $("#preview").scrollIntoView({ block: "nearest" });
}));

$("#check").addEventListener("click", () => withBusy($("#check"), async () => {
  const response = await fetch("/api/check", { method: "POST" });
  if (!response.ok) {
    toast(await readError(response));
    return;
  }
  const data = await response.json();
  state.signature = "";
  await refresh();
  toast(data.message);
}));

$("#test-delivery").addEventListener("click", () => withBusy($("#test-delivery"), async () => {
  const response = await fetch("/api/test-delivery", { method: "POST" });
  const data = await response.json().catch(() => ({}));
  toast(data.message || "Не удалось отправить проверку.");
}));

$("#webhook-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const button = $("#webhook-save");
  withBusy(button, async () => {
    $("#webhook-error").textContent = "";
    const url = $("#webhook-url").value.trim();
    const response = await fetch("/api/webhook", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    if (!response.ok) {
      $("#webhook-error").textContent = await readError(response);
      return;
    }
    const data = await response.json();
    $("#webhook-url").value = "";
    state.signature = "";
    await refresh();
    toast(data.message);
  });
});

$("#webhook-clear").addEventListener("click", () => withBusy($("#webhook-clear"), async () => {
  $("#webhook-error").textContent = "";
  const response = await fetch("/api/webhook/clear", { method: "POST" });
  if (!response.ok) {
    $("#webhook-error").textContent = await readError(response);
    return;
  }
  const data = await response.json();
  state.signature = "";
  await refresh();
  toast(data.message);
}));

$("#watch-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const button = $("#watch-submit");
  withBusy(button, async () => {
    $("#form-error").textContent = "";
    const query = $("#steam-query").value.trim();
    const response = await fetch("/api/watch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    if (!response.ok) {
      $("#form-error").textContent = await readError(response);
      return;
    }
    const friend = await response.json();
    $("#steam-query").value = "";
    state.signature = "";
    await refresh();
    toast(friend.poll_message || `Слежу за ${friend.persona_name}. Дальше сверка идёт сама.`);
  });
});

refresh().catch(() => {
  const root = $("#friends");
  root.replaceChildren();
  const empty = document.createElement("p");
  empty.className = "form-error";
  empty.textContent = "Панель не ответила. Обнови страницу через пару секунд.";
  root.append(empty);
});

setInterval(() => {
  refresh().catch(() => {});
}, 5000);
