/*
 * KOL Mail Discord bridge, page script (runs inside Discord's own page: "world": "MAIN" in the manifest).
 *
 * The content script only sees what is drawn on the screen, so a DM had to be opened before KOL Mail knew about it.
 * Discord's page, however, already receives every DM's new messages, whichever one is on screen. This script listens
 * to that data inside the page (the stores and the dispatcher Discord's own code uses) and hands it to the content
 * script with window.postMessage. It reads; it never writes a message, never presses a key and never touches the
 * account's login.
 *
 * One thing it can ask Discord's page to do, and only when the content script says so (the owner switched that on
 * in KOL Mail): load a DM's recent messages with the page's own loader, the very request a click on that DM makes.
 *
 * Nothing here is an official interface. When Discord changes its page and the stores are not found, this script
 * does nothing, and the content script goes on reading the open DM off the screen as before.
 *
 * Pure helpers are exported for node (test/page.test.js).
 */
(function () {
  "use strict";

  const TAG = "kolmail-dc";
  const DM = 1;                          // channel type of a one-to-one DM
  const TEXT_TYPES = new Set([0, 19]);   // a message, a reply; calls, pins and the like are not conversation
  const ID = /^\d{5,25}$/;
  const TEXT_MAX = 4000;
  const BATCH_MAX = 100;                 // messages of one DM handed over at a time (the newest are kept)
  const CHANNELS_MAX = 60;               // DMs named in a snapshot, most recent first

  // =================================================================================== pure helpers

  function userName(u) {
    return u && typeof u === "object" ? String(u.global_name || u.globalName || u.username || "") : "";
  }

  /** A message time as ISO text: Discord's servers send text, its stores keep Date (or moment) objects. */
  function isoTime(t) {
    if (!t) return null;
    if (typeof t === "string") return isNaN(Date.parse(t)) ? null : t;
    try {
      if (typeof t.toISOString === "function") return t.toISOString();
      if (typeof t.toDate === "function") return t.toDate().toISOString();
    } catch (e) { /* an invalid date */ }
    return null;
  }

  /** Message text as a person reads it: <@id> becomes @name, a custom emoji its :name:. Markdown stays. */
  function plainText(m, lookup) {
    let s = String((m && m.content) || "");
    const names = new Map();
    for (const u of Array.isArray(m && m.mentions) ? m.mentions : []) {
      if (u && typeof u === "object" && u.id) names.set(String(u.id), userName(u));
    }
    s = s.replace(/<@!?(\d{5,25})>/g, (all, id) => {
      const n = names.get(id) || (lookup ? lookup(id) : "");
      return n ? "@" + n : all;
    });
    s = s.replace(/<a?:(\w{1,40}):\d{5,25}>/g, ":$1:");
    if (!s.trim()) {
      const stickers = Array.isArray(m && (m.sticker_items || m.stickerItems)) ? m.sticker_items || m.stickerItems : [];
      const named = stickers.map((x) => x && x.name).filter(Boolean);
      if (named.length) s = named.map((n) => `[贴纸 ${n}]`).join(" ");
    }
    return s.slice(0, TEXT_MAX);
  }

  /**
   * One message as the sync contract has it, from either shape Discord uses (the server's snake_case JSON, or the
   * store's records). Null for anything that is not a sent chat message: one still being sent, a call, a pin.
   * opts: { meId, lookup(userId) -> name, quoted(messageId) -> text }.
   */
  function normMessage(m, opts) {
    if (!m || typeof m !== "object") return null;
    const id = String(m.id || "");
    if (!ID.test(id)) return null;
    if (m.state && m.state !== "SENT") return null;
    if (!TEXT_TYPES.has(Number(m.type || 0))) return null;
    const a = m.author;
    if (!a || typeof a !== "object" || !ID.test(String(a.id || ""))) return null;
    const at = isoTime(m.timestamp);
    if (!at) return null;
    const o = opts || {};
    const attachments = (Array.isArray(m.attachments) ? m.attachments : []).slice(0, 10)
      .filter((x) => x && typeof x === "object")
      .map((x) => ({ name: String(x.filename || x.name || "file").slice(0, 120), url: String(x.url || "").slice(0, 500) }));
    let reply = null;
    const quotedMsg = m.referenced_message || m.referencedMessage;
    const ref = m.message_reference || m.messageReference;
    if (quotedMsg && typeof quotedMsg === "object" && quotedMsg.content) reply = String(quotedMsg.content);
    else if (ref && ref.message_id && o.quoted) reply = o.quoted(String(ref.message_id)) || null;
    return {
      id,
      at,
      author_id: String(a.id),
      author: userName(a),
      out: !!o.meId && String(a.id) === String(o.meId),
      text: plainText(m, o.lookup),
      attachments,
      reply_to: reply ? reply.slice(0, 300) : null,
    };
  }

  /** Newest BATCH_MAX of a list of normalized messages, one per id (the later one wins: an edit). */
  function newestBatch(list) {
    const byId = new Map();
    for (const m of list) if (m) byId.set(m.id, m);
    const all = Array.from(byId.values()).sort((x, y) =>
      x.id.length !== y.id.length ? x.id.length - y.id.length : x.id < y.id ? -1 : x.id > y.id ? 1 : 0);
    return all.slice(-BATCH_MAX);
  }

  /** A store: something with a name, registered with a dispatcher that can be subscribed to. Discord also exports
   *  proxies that answer every property with a function: those have no real dispatcher. */
  function storeName(v) {
    try {
      if (!v || (typeof v !== "object" && typeof v !== "function")) return null;
      if (typeof v.getName !== "function" || !v._dispatcher || typeof v._dispatcher.subscribe !== "function") return null;
      const n = v.getName();
      return typeof n === "string" ? n : null;
    } catch (e) {
      return null;
    }
  }

  /** The module that loads messages: told apart from look-alikes by the other things it does. */
  function isLoader(v) {
    try {
      if (!v || typeof v !== "object") return false;
      const keys = Object.keys(v);
      return keys.includes("fetchMessages") && keys.includes("jumpToMessage") && keys.includes("receiveMessage") &&
        typeof v.fetchMessages === "function";
    } catch (e) {
      return false;
    }
  }

  /** Stores and the loader out of webpack's module caches (plain objects: { id: { exports } }). */
  function scanModules(caches) {
    const stores = {};
    let loader = null;
    const seen = new Set();
    const look = (v) => {
      if (!v || (typeof v !== "object" && typeof v !== "function") || seen.has(v)) return;
      seen.add(v);
      const n = storeName(v);
      if (n && !stores[n]) stores[n] = v;
      if (!loader && isLoader(v)) loader = v;
    };
    for (const cache of caches) {
      for (const id in cache) {
        let ex;
        try { ex = cache[id] && cache[id].exports; } catch (e) { continue; }
        if (!ex || (typeof ex !== "object" && typeof ex !== "function")) continue;
        if (typeof window !== "undefined" && ex === window) continue;
        look(ex);
        if (typeof ex !== "object") continue;
        let keys = [];
        try { keys = Object.getOwnPropertyNames(ex); } catch (e) { continue; }
        if (keys.length > 100) continue;  // a table of strings or icons, not a module's handful of exports
        for (const k of keys) {
          try { look(ex[k]); } catch (e) { /* a binding not set yet */ }
        }
      }
    }
    return { stores, loader };
  }

  const Pure = { userName, isoTime, plainText, normMessage, newestBatch, storeName, isLoader, scanModules };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { Pure };
    return;
  }
  if (typeof window === "undefined" || window.__kolmailDcPage) return;
  window.__kolmailDcPage = true;

  // =================================================================================== in Discord's page

  const FIND_EVERY_MS = 3000;
  const FIND_SLOW_MS = 30000;   // after two minutes of not finding the stores: Discord changed, look now and then
  const FLUSH_MS = 800;
  const SNAPSHOT_MS = 60000;

  let S = null;            // { ChannelStore, UserStore, MessageStore, dispatcher, loader }
  let tries = 0;
  const pending = new Map();  // channel id -> { messages: normalized ones waiting to go, loaded: a whole load among them }
  let flushTimer = 0;
  let lastSnapshot = "";

  function say(kind, data) {
    try { window.postMessage({ source: TAG, from: "page", kind, data }, location.origin); } catch (e) { /* page going away */ }
  }

  function find() {
    const chunk = window.webpackChunkdiscord_app;
    if (!chunk || typeof chunk.push !== "function") return null;
    const reqs = [];
    const before = chunk.length;
    try {
      // The usual way to ask webpack for its require function. Several runtimes may answer: all are looked through.
      chunk.push([[Symbol("kolmail")], {}, (r) => { reqs.push(r); }]);
      if (chunk.length > before) chunk.pop();
    } catch (e) {
      return null;
    }
    const { stores, loader } = scanModules(reqs.map((r) => (r && r.c) || {}));
    const { ChannelStore, UserStore, MessageStore } = stores;
    if (!ChannelStore || !UserStore || !MessageStore) return null;
    if (typeof ChannelStore.getChannel !== "function" || typeof UserStore.getUser !== "function" ||
        typeof UserStore.getCurrentUser !== "function" || typeof MessageStore.getMessages !== "function") return null;
    if (!UserStore.getCurrentUser()) return null;  // not signed in yet
    return { ChannelStore, UserStore, MessageStore, dispatcher: ChannelStore._dispatcher, loader };
  }

  function me() {
    const u = S.UserStore.getCurrentUser();
    return u ? { id: String(u.id), name: userName(u) } : null;
  }

  /** { channel_id, peer, last } of a one-to-one DM with a person, else null (group DMs, servers, bots). */
  function dmOf(channelId) {
    const c = channelId && S.ChannelStore.getChannel(String(channelId));
    if (!c || c.type !== DM) return null;
    const uid = Array.isArray(c.recipients) && c.recipients.length === 1 ? c.recipients[0] : null;
    const u = uid && S.UserStore.getUser(typeof uid === "object" ? uid.id : uid);
    if (!u || u.bot || u.system) return null;
    return {
      channel_id: String(c.id),
      peer: { id: String(u.id), name: userName(u), username: String(u.username || "") },
      last: c.lastMessageId ? String(c.lastMessageId) : null,
    };
  }

  /** The messages Discord's page holds of a DM, and whether it has loaded that DM (ready) or only holds what
   *  arrived while it was open elsewhere. */
  function held(channelId) {
    try {
      const coll = S.MessageStore.getMessages(String(channelId));
      const arr = coll && (typeof coll.toArray === "function" ? coll.toArray() : coll._array);
      return { messages: Array.isArray(arr) ? arr : [], ready: !!(coll && coll.ready) };
    } catch (e) {
      return { messages: [], ready: false };
    }
  }

  /**
   * loaded: these are a DM's recent messages as Discord loaded them, not single ones that just arrived. KOL Mail
   * remembers that, with the id of the DM's newest message: a DM whose newest message is none it keeps (a call, a
   * pin) would otherwise look forever as if its history were still missing, and be asked for on every page load.
   */
  function take(channelId, raws, wasLoaded) {
    const conv = dmOf(channelId);
    const who = me();
    if (!conv || !who) return;
    const opts = {
      meId: who.id,
      lookup: (id) => userName(S.UserStore.getUser(id)),
      quoted: (id) => {
        try { const q = S.MessageStore.getMessage(conv.channel_id, id); return q && q.content ? String(q.content) : null; }
        catch (e) { return null; }
      },
    };
    const msgs = raws.map((m) => normMessage(m, opts)).filter(Boolean);
    if (!msgs.length && !wasLoaded) return;
    const waiting = pending.get(conv.channel_id) || { messages: [], loaded: false };
    pending.set(conv.channel_id, { messages: newestBatch([...waiting.messages, ...msgs]), loaded: waiting.loaded || !!wasLoaded });
    if (!flushTimer) flushTimer = setTimeout(flush, FLUSH_MS);
  }

  function flush() {
    flushTimer = 0;
    const who = S && me();
    if (!who) { pending.clear(); return; }
    const convs = [];
    for (const [channelId, waiting] of pending) {
      const conv = dmOf(channelId);
      if (conv) convs.push(Object.assign(conv, { messages: waiting.messages, loaded: waiting.loaded }));
    }
    pending.clear();
    if (convs.length) say("feed", { me: who, convs });
  }

  /** Which DMs there are and the id of each one's newest message: KOL Mail compares it with what it holds. */
  function snapshot(force) {
    const who = S && me();
    if (!who) return;
    let list = [];
    try { list = S.ChannelStore.getSortedPrivateChannels() || []; } catch (e) { return; }
    const channels = [];
    for (const c of list) {
      const conv = c && c.type === DM ? dmOf(c.id) : null;
      if (conv && conv.last) channels.push(conv);
      if (channels.length >= CHANNELS_MAX) break;
    }
    const key = JSON.stringify(channels.map((c) => [c.channel_id, c.last]));
    if (!force && key === lastSnapshot) return;
    lastSnapshot = key;
    say("channels", { me: who, channels });
  }

  // Whatever Discord's own code does next must never meet an error of ours.
  const guarded = (fn) => (a) => { try { fn(a); } catch (e) { /* a shape we do not know: skipped */ } };

  function start() {
    const d = S.dispatcher;
    d.subscribe("MESSAGE_CREATE", guarded((a) => {
      if (!a || a.optimistic || !a.message) return;  // optimistic: shown before the server took it
      take(a.channelId || a.message.channel_id, [a.message]);
    }));
    d.subscribe("MESSAGE_UPDATE", guarded((a) => {
      const m = a && a.message;
      if (!m || !m.author || m.content == null || !m.timestamp) return;  // a link preview arriving, not an edit
      take(m.channel_id, [m]);
    }));
    d.subscribe("LOAD_MESSAGES_SUCCESS", guarded((a) => {
      // the newest stretch of a DM, not an older page scrolled up to: that one says nothing about its newest message
      if (a && Array.isArray(a.messages)) take(a.channelId, a.messages, !a.isBefore && !a.isAfter);
    }));
    say("ready", { ok: true, can_load: !!S.loader });
    // DMs the page already holds (the one it was opened on loaded before this script found the stores).
    guarded(() => {
      for (const c of S.ChannelStore.getSortedPrivateChannels() || []) {
        if (c && c.type === DM) { const h = held(c.id); if (h.messages.length) take(c.id, h.messages, h.ready); }
      }
    })();
    snapshot(true);
    setInterval(guarded(() => snapshot(false)), SNAPSHOT_MS);
  }

  function attempt() {
    tries++;
    let found = null;
    try { found = find(); } catch (e) { found = null; }
    if (found) { S = found; start(); return; }
    if (tries === 40) say("ready", { ok: false });
    setTimeout(attempt, tries < 40 ? FIND_EVERY_MS : FIND_SLOW_MS);
  }

  window.addEventListener("message", (e) => {
    if (e.source !== window || e.origin !== location.origin) return;
    const d = e.data;
    if (!d || d.source !== TAG || d.from !== "ext") return;
    if (d.kind === "hello") {
      if (S) { say("ready", { ok: true, can_load: !!S.loader }); snapshot(true); }
      return;
    }
    if (d.kind === "load" && S) {
      guarded(() => {
        const conv = dmOf(d.channel_id);
        if (!conv) return;
        const h = held(conv.channel_id);
        // the page loaded this DM already: nothing to ask for. A few messages that arrived while it was never
        // opened are not its history.
        if (h.ready && h.messages.length) { take(conv.channel_id, h.messages, true); return; }
        if (!S.loader) return;
        // Discord's own loader, called the way opening the DM calls it. Its answer arrives as LOAD_MESSAGES_SUCCESS.
        const p = S.loader.fetchMessages({ channelId: conv.channel_id, limit: 50 });
        if (p && typeof p.catch === "function") p.catch(() => {});
      })();
    }
  });

  setTimeout(attempt, 1500);
})();
