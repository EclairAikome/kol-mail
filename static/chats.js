// 私信: the Telegram and Discord private chats (app.py /api/chat/*), drafted like mail replies.
// Everything a KOL wrote goes into the page as textContent, never as HTML.
(() => {
  "use strict";
  const $c = (sel, root = document) => root.querySelector(sel);
  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false)
      n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    return n;
  };
  async function call(path, body) {
    const init = { headers: { "X-KOL-Mail": "1" } };
    if (body !== undefined) {
      init.method = "POST"; init.body = JSON.stringify(body); init.headers["Content-Type"] = "application/json";
    }
    const r = await fetch(path, init);
    const data = await r.json().catch(() => ({ error: "响应无法解析" }));
    if (!r.ok) { const e = new Error(data.error || r.statusText); e.status = r.status; e.data = data; throw e; }
    return data;
  }
  const when = s => {
    const d = new Date(s); if (isNaN(d)) return s || "";
    const t = d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    return d.toDateString() === new Date().toDateString() ? t : `${d.getMonth() + 1}-${d.getDate()} ${t}`;
  };
  const norm = s => String(s || "").replace(/\r\n/g, "\n").trim();
  // replaceChildren would print a null or false as text: the conditional parts drop out here
  const kids = (...a) => a.flat().filter(k => k !== null && k !== undefined && k !== false);

  document.head.append(el("style", { text: `
    #chatBtn .n { margin-left: 4px; font-size: 11px; font-weight: 600; color: var(--on-accent); background: var(--accent);
      border-radius: 9px; padding: 0 6px; }
    #chatView { position: fixed; top: 44px; left: 0; right: 0; bottom: 0; z-index: 5; display: grid;
      grid-template-columns: 330px 1fr; background: var(--bg); }
    #chatView[hidden] { display: none; }
    #chatView .side { border-right: 1px solid var(--line); overflow: auto; background: var(--surface); }
    #chatView .side .listbar { flex-wrap: wrap; }
    #chatView .filters button { border: 0; background: none; color: var(--muted); cursor: pointer; font: inherit; padding: 2px 6px; }
    #chatView .filters button.on { color: var(--accent); font-weight: 600; }
    #chatView .conv { overflow: auto; padding: 14px 20px 40px; }
    .chat-ch { font-size: 11px; border-radius: 8px; padding: 0 6px; border: 1px solid var(--line); color: var(--muted); }
    .chat-ch.telegram { color: #2a9fd6; border-color: #2a9fd6; } .chat-ch.discord { color: #5865f2; border-color: #5865f2; }
    .chat-link { font-size: 12px; color: var(--muted); }
    .chat-link.ok { color: var(--ok); }
    .chat-head { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }
    .chat-head h2 { margin: 0; }
    .chat-linkbar { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; margin: 4px 0 12px; font-size: 13px; }
    .chat-linkbar input { font: inherit; padding: 3px 8px; border: 1px solid var(--line); border-radius: 6px;
      background: var(--bg); color: var(--text); width: 220px; }
    .chat-hits { display: flex; flex-direction: column; gap: 4px; margin: 4px 0 10px; max-height: 220px; overflow: auto; }
    .chat-hits button { text-align: left; }
    .chat-msgs { display: flex; flex-direction: column; gap: 8px; margin: 10px 0 16px; }
    .bubble { max-width: 78%; border: 1px solid var(--line); border-radius: 10px; padding: 7px 10px; background: var(--card); }
    .bubble.out { align-self: flex-end; background: var(--accent-soft); }
    .bubble .t { font-size: 11px; color: var(--muted); margin-bottom: 2px; }
    .bubble .o { white-space: pre-wrap; word-break: break-word; }
    .bubble .z { white-space: pre-wrap; word-break: break-word; color: var(--muted); font-size: 13px; margin-top: 4px;
      border-top: 1px dashed var(--line); padding-top: 4px; }
    .bubble .x { font-size: 12px; color: var(--warn); margin-top: 2px; }
    .chat-reply textarea.full { min-height: 110px; }
    .chat-reply .row2 { display: flex; gap: 8px; align-items: center; margin: 8px 0; flex-wrap: wrap; }
    .chat-queue { border: 1px solid var(--warn); color: var(--warn); border-radius: 8px; padding: 8px 10px; margin: 8px 0; font-size: 13px; }
    .chat-setup section { margin-bottom: 16px; }
    .chat-setup .code { user-select: all; font-family: Consolas, monospace; background: var(--panel); border: 1px solid var(--line);
      border-radius: 4px; padding: 1px 6px; }
  ` }));

  // ---------------------------------------------------------------- the view

  const st = { convs: [], filter: "all", key: null, conv: null, msgs: [], queue: [], ctx: "", newestIn: "",
               draft: null, pairs: null, language: "", timer: null, listTimer: null, busy: false, rowsHit: [] };
  const btn = el("button", { class: "act", id: "chatBtn", type: "button", title: "Telegram 和 Discord 私信：按手册起草，写进 KOL 总表" },
    "私信", el("span", { class: "n", hidden: true }));
  const compose = $c("#composeBtn");
  if (compose) compose.after(btn); else ($c("header") || document.body).append(btn);
  const view = el("div", { id: "chatView", hidden: true });
  const side = el("div", { class: "side" });
  const pane = el("div", { class: "conv" }, el("div", { class: "empty", text: "选一个会话" }));
  view.append(side, pane);
  document.body.append(view);

  function show(on) {
    view.hidden = !on;
    btn.classList.toggle("primary", on);
    clearInterval(st.timer); st.timer = null;
    if (on) { loadList(); st.timer = setInterval(tick, 10000); }
    else if (location.hash.startsWith("#chat")) history.replaceState(null, "", location.pathname + location.search);
  }
  btn.addEventListener("click", () => show(view.hidden));
  window.addEventListener("hashchange", fromHash);
  // The Discord extension re-opens this tab on the same #chat=… it already shows: no hashchange then, but the tab
  // comes to the front.
  document.addEventListener("visibilitychange", () => { if (!document.hidden) fromHash(); });
  function fromHash() {
    const m = /^#chat(?:=((?:tg|dc):-?\d+))?$/.exec(location.hash);
    if (!m) return;
    if (!view.hidden && (!m[1] || m[1] === st.key)) return;  // already there: keep a draft in progress
    show(true);
    if (m[1]) openConv(m[1]);
  }

  async function tick() {
    if (view.hidden || document.hidden) return;
    await loadList().catch(() => {});
    if (st.key && !st.busy) await refreshConv().catch(() => {});
  }

  async function badge() {
    try {
      const d = await call("/api/chat/list");
      const n = d.convs.filter(c => c.unread).length;
      const b = $c(".n", btn); b.hidden = !n; b.textContent = n;
      return d;
    } catch { return null; }
  }
  setInterval(() => { if (view.hidden && !document.hidden) badge(); }, 30000);
  badge();

  async function loadList() {
    const d = await call("/api/chat/list" + (st.filter === "ignored" ? "?ignored=1" : ""));
    st.convs = d.convs;
    const unread = d.convs.filter(c => c.unread).length;
    const b = $c(".n", btn); b.hidden = !unread || st.filter === "ignored"; b.textContent = unread;
    const tgNote = !d.telegram.configured ? "Telegram 还没登录（设置 → 私信渠道）"
      : d.telegram.error ? "Telegram：" + d.telegram.error : !d.telegram.ready ? "Telegram 连接中" : "";
    const dcNote = d.discord.seen ? "" : "Discord 扩展还没连上（设置 → 私信渠道）";
    side.replaceChildren(...kids(
      el("div", { class: "listbar" },
        el("button", { class: "act", type: "button", onclick: () => show(false) }, "← 邮件"),
        el("span", { class: "filters" }, ...[["all", "全部"], ["unlinked", "未关联"], ["ignored", "已忽略"]].map(([k, t]) =>
          el("button", { type: "button", class: st.filter === k ? "on" : "", onclick: () => { st.filter = k; loadList(); } }, t))),
        el("button", { class: "act", type: "button", onclick: () => loadList() }, "刷新")),
      ...[tgNote, dcNote, d.sheet ? "" : "没接 KOL 总表：私信只在这里看，不记表", d.popo_error || ""].filter(Boolean)
        .map(t => el("div", { class: "note", style: "padding:6px 12px" }, t)),
      ...(st.filter === "unlinked" ? d.convs.filter(c => !c.linked) : d.convs).map(row),
      d.convs.length ? null : el("div", { class: "empty", text: "还没有会话" })));
  }

  function row(c) {
    return el("div", { class: "row" + (c.key === st.key ? " on" : "") + (c.unread ? " unread" : ""), onclick: () => openConv(c.key) },
      el("div", { class: "top" },
        el("span", { class: "from", text: c.name }),
        el("span", {}, el("span", { class: "chat-ch " + c.channel, text: c.channel === "telegram" ? "TG" : "DC" }), " ", when(c.last_at))),
      el("div", { class: "chat-link" + (c.linked ? " ok" : ""), text: c.linked ? `${c.linked.id || c.linked.email} ${c.linked.kol}` : "未关联总表" }),
      el("div", { class: "zh", text: (c.preview_out ? "我：" : "") + (c.preview || "") }));
  }

  // ---------------------------------------------------------------- one conversation

  async function openConv(key) {
    if (st.key !== key) Object.assign(st, { key, ctx: "", draft: null, pairs: null, rowsHit: [], feedbackLog: [],
                                            sendLocked: false });
    if (location.hash !== "#chat=" + key) history.replaceState(null, "", "#chat=" + key);
    await refreshConv(true);
    for (const r of side.querySelectorAll(".row")) r.classList.remove("on");
    loadList().catch(() => {});
  }

  async function refreshConv(full = false) {
    const key = st.key;
    const d = await call("/api/chat/conv?key=" + encodeURIComponent(key));
    if (key !== st.key) return;
    const changed = full || d.messages.length !== st.msgs.length ||
      (d.messages.at(-1) || {}).id !== (st.msgs.at(-1) || {}).id || d.messages.some((m, i) => (st.msgs[i] || {}).zh !== m.zh);
    Object.assign(st, { conv: d.conv, msgs: d.messages, queue: d.queue });
    if (full || !$c(".chat-msgs", pane)) render();
    else if (changed) { renderMsgs(); renderQueue(); }
    else renderQueue();
    if (full && d.messages.some(m => !m.zh && m.text)) translate();
  }

  async function translate() {
    const key = st.key;
    try {
      const d = await call("/api/chat/translate", { key });
      if (key === st.key) { st.msgs = d.messages; renderMsgs(); }
    } catch (e) { note("翻译失败：" + e.message, true); }
  }

  function render() {
    const c = st.conv;
    pane.replaceChildren(...kids(
      el("div", { class: "chat-head" },
        el("h2", { text: c.name }), c.username ? el("span", { class: "note", text: "@" + c.username }) : null,
        el("span", { class: "chat-ch " + c.channel, text: c.label }),
        c.channel === "telegram" ? el("button", { class: "act", type: "button", onclick: () => syncNow() }, "刷新") : null),
      el("div", { class: "chat-linkbar", id: "chatLink" }),
      el("div", { class: "chat-hits", id: "chatHits" }),
      el("div", { class: "chat-msgs", id: "chatMsgs" }),
      el("div", { id: "chatQueue" }),
      replyBox()));
    renderLink(); renderMsgs(); renderQueue();
  }

  async function syncNow() {
    try { const d = await call("/api/chat/sync", { key: st.key }); if (d.added) await refreshConv(); }
    catch (e) { note(e.message, true); }
  }

  function renderLink() {
    const c = st.conv, box = $c("#chatLink", pane), hits = $c("#chatHits", pane);
    hits.replaceChildren(...kids());
    if (c.linked) {
      box.replaceChildren(...kids(el("span", { class: "chat-link ok", text: `已关联总表 ${c.linked.id || ""} ${c.linked.kol}` }),
        el("button", { class: "act", type: "button", onclick: () => pick() }, "改"),
        el("button", { class: "act", type: "button", onclick: () => unlink() }, "取消关联"),
        el("span", { class: "note", text: "这个会话的每条消息都会记进总表沟通记录" })));
      return;
    }
    box.replaceChildren(...kids(el("span", { class: "chat-link", text: c.ignored ? "已忽略（不是 KOL）" : "还没关联总表，私信不会记表" }),
      el("button", { class: "act primary", type: "button", onclick: () => pick() }, "关联总表"),
      c.ignored ? el("button", { class: "act", type: "button", onclick: () => ignore(false) }, "取消忽略")
        : el("button", { class: "act", type: "button", onclick: () => ignore(true) }, "不是 KOL，忽略")));
    suggestions();
  }

  async function suggestions() {
    const key = st.key;
    try {
      const d = await call("/api/chat/rows?key=" + encodeURIComponent(key) + "&q=__none__");
      if (key !== st.key || !d.suggest.length || st.conv.linked) return;  // switched chats meanwhile
      $c("#chatHits", pane).replaceChildren(...kids(el("span", { class: "note", text: "看着像总表里的：" }),
        ...d.suggest.map(r => el("button", { class: "act", type: "button", onclick: () => link(r) },
          `关联 ${r.id || r.email} ${r.kol}${r.project ? " · " + r.project : ""}${r.stage ? " · " + r.stage : ""}`))));
    } catch { /* the sheet is busy: the search box still works */ }
  }

  function pick() {
    const hits = $c("#chatHits", pane);
    const input = el("input", { type: "search", placeholder: "搜 KOL 名、ID 或邮箱", maxlength: "80" });
    const list = el("div", { class: "chat-hits" });
    let t = null;
    input.addEventListener("input", () => { clearTimeout(t); t = setTimeout(search, 250); });
    const key = st.key;
    async function search() {
      const q = input.value.trim();
      if (!q) { list.replaceChildren(...kids()); return; }
      try {
        const d = await call("/api/chat/rows?key=" + encodeURIComponent(key) + "&q=" + encodeURIComponent(q));
        if (key !== st.key) return;
        list.replaceChildren(...kids(...d.rows.map(r => el("button", { class: "act", type: "button", onclick: () => link(r) },
          `${r.id || r.email} ${r.kol}${r.project ? " · " + r.project : ""}${r.platform ? " · " + r.platform : ""}${r.stage ? " · " + r.stage : ""}`)),
          d.rows.length ? null : el("span", { class: "note", text: "总表里没找到。新 KOL 先在总表里建好那一行（会话里说一声就行），再回来关联" })));
      } catch (e) { list.replaceChildren(...kids(el("span", { class: "note err", text: e.message }))); }
    }
    hits.replaceChildren(...kids(input, list));
    input.focus();
  }

  // Every answer below lands only on the chat it was asked for: switching chats meanwhile drops it.
  async function link(r) {
    const key = st.key;
    try {
      const d = await call("/api/chat/link", { key, id: r.id || "", email: r.email || "" });
      loadList();
      if (key !== st.key) return;
      st.conv = d.conv; renderLink();
      note("已关联，最近几天的消息会记进总表沟通记录");
    } catch (e) { if (key === st.key) note(e.message, true); }
  }
  async function unlink() {
    const key = st.key;
    try {
      const d = await call("/api/chat/unlink", { key });
      loadList();
      if (key === st.key) { st.conv = d.conv; renderLink(); note("已取消关联，以后不会再自动关联这个会话"); }
    } catch (e) { if (key === st.key) note(e.message, true); }
  }
  async function ignore(on) {
    const key = st.key;
    try {
      await call("/api/chat/ignore", { key, ignored: on });
      loadList();
      if (key === st.key) { st.conv.ignored = on; renderLink(); }
    } catch (e) { if (key === st.key) note(e.message, true); }
  }

  function renderMsgs() {
    const box = $c("#chatMsgs", pane);
    if (!box) return;
    const atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 80;
    box.replaceChildren(...kids(...st.msgs.map(m => el("div", { class: "bubble" + (m.out ? " out" : "") },
      el("div", { class: "t", text: `${m.out ? "我" : (m.author || st.conv.name)} · ${when(m.at)}` }),
      m.reply_to ? el("div", { class: "x", text: "回复：" + m.reply_to }) : null,
      el("div", { class: "o", text: m.text || "" }),
      m.media ? el("div", { class: "x", text: "附件：" + m.media }) : null,
      m.attachments && m.attachments.length ? el("div", { class: "x", text: "附件：" + m.attachments.join("、") }) : null,
      m.link ? el("div", { class: "x", text: "链接：" + m.link }) : null,
      m.zh && m.zh !== m.text ? el("div", { class: "z", text: m.zh }) : null)),
      st.msgs.length ? null : el("div", { class: "empty", text: "还没有消息" })));
    if (atBottom || !box.dataset.seen) { pane.scrollTop = pane.scrollHeight; box.dataset.seen = "1"; }
  }

  function renderQueue() {
    const box = $c("#chatQueue", pane);
    if (!box) return;
    const q = st.queue || [];
    box.replaceChildren(...kids(q.length ? el("div", { class: "chat-queue" },
      `还有 ${q.length} 条等你在 Discord 里发：打开和对方的私信，扩展会把第 ${q[0].n} 条填进输入框，你检查后按回车，发完自动填下一条。`,
      " ", el("button", { class: "act", type: "button", onclick: () => window.open("https://discord.com/channels/@me/" + st.key.slice(3), "_blank", "noopener") }, "去 Discord")) : null));
  }

  // ---------------------------------------------------------------- drafting

  function replyBox() {
    const box = el("div", { class: "reply chat-reply", id: "chatReply" },
      el("textarea", { id: "chatNotes", placeholder: "可以先写几句要点（比如“报 2 条 80 美元，先问受众国家”），也可以空着直接起草；写好了整段中文回复就点“翻译我写的中文”", maxlength: "4000" }),
      el("div", { class: "row2" },
        el("button", { class: "act primary", type: "button", onclick: () => strategies() }, "按手册起草"),
        el("button", { class: "act", type: "button", onclick: () => draft({ mode: "translate" }) }, "翻译我写的中文"),
        el("span", { class: "note", id: "chatNote" })),
      el("div", { id: "chatOpts" }),
      el("div", { id: "chatDraft" }));
    return box;
  }
  function note(text, bad = false) {
    const n = $c("#chatNote", pane); if (!n) return;
    n.textContent = text || ""; n.className = "note" + (bad ? " err" : "");
  }
  // One request at a time per chat. fn gets the chat's key and must drop its result when st.key moved on.
  async function busy(label, fn) {
    const key = st.key;
    if (st.busy === key) return;
    st.busy = key; note(label);
    try { await fn(key); } catch (e) {
      if (key !== st.key) return;
      if (e.data && e.data.changed) { note(e.message, true); await refreshConv(); }
      else note(e.message, true);
      if (e.data && (e.data.uncertain || e.data.sent > 0)) { st.sendLocked = true; renderPairs(); }
    } finally { if (st.busy === key) st.busy = null; }
  }

  function strategies() {
    return busy("AI 在读对话、想回法…", async key => {
      const d = await call("/api/chat/strategies", { key, ctx: st.ctx, notes: $c("#chatNotes", pane).value });
      if (key !== st.key) return;
      st.ctx = d.ctx;
      const box = $c("#chatOpts", pane);
      box.replaceChildren(...kids(el("p", { class: "body", text: d.situation_zh }),
        el("div", { class: "opts" }, ...d.options.map((o, i) => el("div", { class: "opt" + (i === d.recommended ? " rec" : "") },
          el("h4", {}, o.title_zh, i === d.recommended ? el("span", { class: "badge", text: "推荐" }) : null),
          el("div", { class: "lbl", text: "怎么回" }), el("div", { class: "txt", text: o.approach_zh }),
          el("div", { class: "lbl", text: "为什么" }), el("div", { class: "txt", text: o.why_zh }),
          el("div", { class: "lbl", text: "风险" }), el("div", { class: "txt risk", text: o.risk_zh }),
          el("button", { class: "act primary", type: "button", onclick: () => draft({ strategy: o }) }, "用这个方案起草"))))));
      note("");
    });
  }

  function draft(extra) {
    return busy("AI 在起草…", async key => {
      // the owner's earlier change requests keep holding in later rounds, as in a mail reply
      const body = { key, ctx: st.ctx, notes: $c("#chatNotes", pane).value, ...extra,
                     feedback_history: extra.feedback ? st.feedbackLog || [] : [] };
      const d = await call("/api/chat/draft", body);
      if (key !== st.key) return;
      st.feedbackLog = extra.feedback ? [...(st.feedbackLog || []), extra.feedback].slice(-20) : [];
      // d.newest belongs to this draft: what the conversation looked like when it was written (sendIt sends it)
      st.ctx = d.ctx; st.language = d.reply_language || "";
      st.draft = d; st.pairs = d.pairs; st.sendLocked = false;
      renderDraft(d.reply_body);
      note("");
    });
  }

  function renderDraft(text) {
    const d = st.draft, box = $c("#chatDraft", pane);
    const body = el("textarea", { class: "full", id: "chatBody" });
    body.value = text;
    body.addEventListener("input", () => { st.pairs = null; renderPairs(); });
    const fb = el("input", { type: "text", id: "chatFb", placeholder: "修改意见，比如“语气热情一点”“价格改成两条 150 美元”", maxlength: "1000",
      style: "flex:1;font:inherit;padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--text)" });
    const warn = [...(d.warnings || [])];
    box.replaceChildren(...kids(
      el("h3", { class: "note", text: `草稿（${d.reply_language || ""}，每段发成一条消息）` }),
      d.notes_zh ? el("p", { class: "note", text: d.notes_zh }) : null,
      ...warn.map(w => el("p", { class: "note err", text: w })),
      d.needs_approval_zh && d.needs_approval_zh !== "无" ? el("p", { class: "note err", text: "发之前要你确认：" + d.needs_approval_zh }) : null,
      body,
      el("div", { class: "row2" }, fb,
        el("button", { class: "act", type: "button", onclick: () => revise() }, "按意见修改")),
      el("div", { id: "chatPairs" })));
    renderPairs();
  }

  function renderPairs() {
    const box = $c("#chatPairs", pane); if (!box) return;
    const text = norm($c("#chatBody", pane).value);
    const p = st.pairs;
    const fresh = p && p.ok;
    const parts = text ? text.split(/\n\s*\n/).map(s => s.trim()).filter(Boolean).length : 0;
    const send = el("button", { class: "act primary", type: "button", disabled: !fresh || !parts || st.sendLocked,
      onclick: () => sendIt() },
      st.conv.channel === "telegram" ? `发到 Telegram（${parts} 条）` : `交给 Discord 发送（${parts} 条）`);
    const table = p ? el("table", { class: "pairs" }, el("tbody", {}, ...p.rows.map((r, i) => {
      const zh = el("textarea", { class: "zhedit", "data-i": i, rows: "1" });
      zh.value = r.failed ? "" : r.zh;
      zh.readOnly = !!r.same;
      zh.addEventListener("input", () => { zh.style.height = "auto"; zh.style.height = zh.scrollHeight + "px"; tr.classList.toggle("changed", zh.value !== (r.zh || "")); });
      const tr = el("tr", { class: r.same ? "same" : "" }, el("td", { text: r.orig }), el("td", { class: "zh" + (r.failed ? " failed" : "") }, zh));
      requestAnimationFrame(() => { zh.style.height = zh.scrollHeight + "px"; });
      return tr;
    }))) : null;
    box.replaceChildren(...kids(
      el("div", { class: "pairsbar" },
        el("b", { class: "note", text: "中文对照（改中文那一栏可以让 AI 只改那几段原文）" }),
        el("button", { class: "act", type: "button", onclick: () => repair() }, fresh ? "重新对照" : "更新对照"),
        el("button", { class: "act", type: "button", onclick: () => zhEdit() }, "按改过的中文改原文"),
        send),
      p && p.error ? el("p", { class: "note err", text: p.error }) : null,
      p ? null : el("p", { class: "note err", text: "正文改过了，点“更新对照”核对后才能发" }),
      st.sendLocked ? el("p", { class: "note err", text: "这版可能已经发出了一部分，先看上面的聊天记录；还要补发就重新起草" }) : null,
      table));
  }

  function revise() {
    const fb = $c("#chatFb", pane).value.trim();
    if (!fb) { note("先写修改意见", true); return; }
    return draft({ previous_body: $c("#chatBody", pane).value, feedback: fb });
  }
  function repair() {
    return busy("在核对中文…", async key => {
      const pairs = await call("/api/reply/pairs", { text: $c("#chatBody", pane).value });
      if (key !== st.key) return;
      st.pairs = pairs; renderPairs(); note("");
    });
  }
  function zhEdit() {
    const edits = [...pane.querySelectorAll("#chatPairs textarea.zhedit")].map(t => {
      const r = st.pairs.rows[+t.dataset.i];
      return t.value !== (r.zh || "") ? { index: +t.dataset.i, orig: r.orig, zh_before: r.zh || "", zh_after: t.value } : null;
    }).filter(Boolean);
    if (!edits.length) { note("没有改过的中文", true); return; }
    return busy("AI 在按你的中文改原文…", async key => {
      const d = await call("/api/reply/zhedit", { text: $c("#chatBody", pane).value, edits, language: st.language });
      if (key !== st.key) return;
      $c("#chatBody", pane).value = d.body; st.pairs = d.pairs; renderPairs();
      note((d.warnings || []).join("；"), !!(d.warnings || []).length);
    });
  }
  function sendIt() {
    const text = $c("#chatBody", pane).value;
    const zh = (st.pairs && st.pairs.rows || []).map(r => r.zh || "");
    const tg = st.conv.channel === "telegram";
    const newest = st.draft && st.draft.newest;
    return busy(tg ? "在发送…" : "在交给 Discord…", async key => {
      const d = await call("/api/chat/send", { key, text, zh, newest });
      if (key !== st.key) { loadList(); return; }
      st.draft = null; st.pairs = null;
      $c("#chatDraft", pane).replaceChildren(...kids()); $c("#chatOpts", pane).replaceChildren(...kids()); $c("#chatNotes", pane).value = "";
      st.ctx = "";
      note(tg ? `已发出 ${d.sent} 条` : `已交给 Discord ${d.queued} 条：去和对方的私信页，检查输入框里的内容后按回车`);
      await refreshConv();
    });
  }

  // ---------------------------------------------------------------- 设置 → 私信渠道

  const tab = $c("#tabChat");
  if (tab) tab.onclick = () => { if (typeof showSetupTab === "function") showSetupTab("setupChat"); renderSetup(); };  // eslint-disable-line no-undef
  async function renderSetup() {
    if (window.tgSetup && typeof window.tgSetup.render === "function") window.tgSetup.render();
    const box = $c("#dcSetup"); if (!box) return;
    let d;
    try { d = await call("/api/chat/ext"); } catch (e) { box.replaceChildren(...kids(el("p", { class: "note err", text: e.message }))); return; }
    const token = el("span", { class: "code", text: d.token });
    box.replaceChildren(...kids(
      el("h4", { text: "Discord 浏览器扩展" }),
      el("p", { class: "note", text: "Discord 不让程序替你收发私信，所以用一个只在 Discord 网页版里工作的小扩展：它把你打开的私信交给 KOL Mail 记录和起草，你确认的回复由它填进输入框，回车还是你自己按。" }),
      el("ol", { class: "note" },
        el("li", {}, "Chrome 打开 chrome://extensions ，右上角打开“开发者模式”"),
        el("li", {}, "点“加载已解压的扩展程序”，选这个文件夹：", el("span", { class: "code", text: d.folder })),
        el("li", {}, "点浏览器右上角的扩展图标，把下面的令牌粘贴进去" + (d.port !== 8765 ? `，端口填 ${d.port}` : "") + "，点“测试连接”"),
        el("li", {}, "用 Discord 网页版（discord.com）和 KOL 私聊，桌面版 Discord 里扩展不起作用")),
      el("div", { class: "row2" }, el("b", { text: "令牌" }), token,
        el("button", { class: "act", type: "button", onclick: () => navigator.clipboard.writeText(d.token).then(() => note2("已复制"), () => note2("复制失败，手动选中复制")) }, "复制"),
        el("button", { class: "act", type: "button", onclick: async () => { await call("/api/chat/ext", {}); renderSetup(); } }, "重新生成")),
      el("p", { class: "note", id: "dcNote", text: d.seen ? "扩展上次连上：" + when(d.seen) : "扩展还没连上过" })));
    function note2(t) { const n = $c("#dcNote"); if (n) n.textContent = t; }
  }

  if (location.hash.startsWith("#chat")) setTimeout(fromHash, 300);
})();
