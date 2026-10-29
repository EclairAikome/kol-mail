/*
 * KOL Mail Discord bridge, background service worker.
 *
 * The only part of the extension that talks to KOL Mail, and the only one that sees the token.
 * It fetches nothing but http://127.0.0.1:<port>/ext/discord/<op> and never follows a redirect.
 * Discord is never contacted from here: the content script reads the page, nothing else.
 */
"use strict";

const DEFAULT_PORT = 8765;
const TIMEOUT_MS = 15000;
const OPS = new Set(["hello", "sync", "queue", "queue/update"]);
const STATUSES = new Set(["filled", "sent", "dropped"]);
const DISCORD_ORIGINS = new Set(["https://discord.com", "https://ptb.discord.com", "https://canary.discord.com"]);
const CHANNEL_ID = /^\d{5,25}$/;
const OUTBOX_KEY = "outbox";
const OUTBOX_MAX = 200;
const OUTBOX_MAX_AGE_MS = 10 * 60 * 1000; // a status older than this is no use to KOL Mail any more
const OUTBOX_MAX_TRIES = 30;

const ERR = {
  auth: "令牌不对，到 KOL Mail 设置 → 私信渠道 复制令牌",
  noToken: "还没填令牌，点浏览器右上角的扩展图标，粘贴 KOL Mail 的令牌",
  offline: "KOL Mail 没在运行",
  timeout: "KOL Mail 没有响应，稍后再试",
  badReply: "KOL Mail 返回的内容看不懂，可能需要更新 KOL Mail",
  internal: "扩展内部出错，刷新页面再试",
};

// Content scripts must not read the token: only this worker and the popup may use storage.local.
try {
  const p = chrome.storage.local.setAccessLevel && chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
  if (p && typeof p.catch === "function") p.catch(() => {});
} catch (e) { /* older Chrome: content.js does not read storage.local anyway */ }

function validPort(v) {
  const n = Number(v);
  return Number.isInteger(n) && n >= 1 && n <= 65535 ? n : DEFAULT_PORT;
}

async function getSettings() {
  const s = await chrome.storage.local.get(["token", "port"]);
  return {
    token: typeof s.token === "string" ? s.token.trim() : "",
    port: s.port == null || s.port === "" ? DEFAULT_PORT : validPort(s.port),
  };
}

/** One POST to KOL Mail: { net: true, error } when it could not be reached, else { status, body }. */
async function rawCall(op, payload) {
  const { token, port } = await getSettings();
  if (!token) return { noToken: true };
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  let res;
  try {
    res = await fetch(`http://127.0.0.1:${port}/ext/discord/${op}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-KOL-Ext": token },
      body: JSON.stringify(payload || {}),
      signal: ctrl.signal,
      credentials: "omit",
      cache: "no-store",
      redirect: "error", // the token goes to the configured local port and nowhere else
    });
  } catch (e) {
    return { net: true, error: e && e.name === "AbortError" ? ERR.timeout : ERR.offline };
  } finally {
    clearTimeout(timer);
  }
  let body = null;
  try { body = await res.json(); } catch (e) { body = null; }
  return { status: res.status, body };
}

/**
 * hello / sync / queue. Resolves { ok: true, data } or { ok: false, code, error, status? }:
 * code "auth" (401/403 or no token), "offline" (not running / no answer), "server" (anything else).
 */
async function callKolMail(op, payload) {
  if (!OPS.has(op)) return { ok: false, code: "internal", error: ERR.internal };
  return toResult(await rawCall(op, payload));
}

function toResult(r) {
  if (r.noToken) return { ok: false, code: "auth", error: ERR.noToken };
  if (r.net) return { ok: false, code: "offline", error: r.error };
  if (r.status === 401 || r.status === 403) return { ok: false, code: "auth", status: r.status, error: ERR.auth };
  const body = r.body;
  const bodyError = body && typeof body.error === "string" && body.error.trim() ? body.error.trim() : "";
  if (r.status < 200 || r.status >= 300 || (body && body.ok === false)) {
    return { ok: false, code: "server", status: r.status, error: bodyError || `KOL Mail 出错（HTTP ${r.status}）` };
  }
  if (!body || typeof body !== "object") return { ok: false, code: "server", status: r.status, error: ERR.badReply };
  return { ok: true, data: body };
}

/** Worth trying again later: KOL Mail not reachable, or failing on its side. */
const retryable = (r) => !!(r.noToken || r.net || r.status >= 500);

// ------------------------------------------------------------------ outbox for sent / dropped / filled
// A status change made while KOL Mail was down (or this worker was asleep) must still reach it,
// otherwise KOL Mail keeps offering a message that was already sent. Kept in chrome.storage.session.
// Any answer from KOL Mail other than an outage is final: {"ok": true, "changed": false} means the
// item was already final there, and a 4xx will not get better by asking again.

let lock = Promise.resolve();
function locked(fn) {
  const run = lock.then(fn, fn);
  lock = run.catch(() => {});
  return run;
}

async function loadOutbox() {
  const s = await chrome.storage.session.get(OUTBOX_KEY);
  return Array.isArray(s[OUTBOX_KEY]) ? s[OUTBOX_KEY] : [];
}

async function saveOutbox(box) {
  await chrome.storage.session.set({ [OUTBOX_KEY]: box.slice(-OUTBOX_MAX) });
}

/** Deliver queued updates in order. Returns the result per update uid; stops at the first outage. */
async function flushLocked() {
  const box = await loadOutbox();
  const results = {};
  let lastFailure = null;
  while (box.length) {
    const u = box[0];
    if (Date.now() - (u.at || 0) > OUTBOX_MAX_AGE_MS || (u.tries || 0) >= OUTBOX_MAX_TRIES) {
      results[u.uid] = { ok: false, code: "expired", error: ERR.offline };
      box.shift();
      continue;
    }
    const r = await rawCall("queue/update", { channel_id: u.channel_id, item_id: u.item_id, status: u.status });
    if (retryable(r)) {
      u.tries = (u.tries || 0) + 1;
      lastFailure = toResult(r);
      break;
    }
    results[u.uid] = r.status >= 200 && r.status < 300
      ? { ok: true, data: { changed: !!(r.body && r.body.changed === true) } }
      : toResult(r);
    box.shift();
  }
  await saveOutbox(box);
  return { results, lastFailure };
}

function flushOutbox() {
  return locked(flushLocked);
}

function enqueueUpdate(channelId, itemId, status) {
  return locked(async () => {
    const box = await loadOutbox();
    let entry = box.find((u) => u.channel_id === channelId && u.item_id === itemId && u.status === status);
    if (!entry) {
      entry = { uid: `${Date.now()}-${Math.random().toString(36).slice(2)}`, channel_id: channelId, item_id: itemId, status,
        at: Date.now(), tries: 0 };
      box.push(entry);
      await saveOutbox(box);
    }
    const { results, lastFailure } = await flushLocked();
    const r = results[entry.uid];
    if (r) return r;
    return Object.assign({}, lastFailure || { ok: false, code: "offline", error: ERR.offline }, { queued: true });
  });
}

/**
 * Auto-fill's claim: KOL Mail moves an item from pending to filled once, and answers changed: true to
 * that one caller only. So of two tabs on the same DM only one pastes. Not queued: no answer, no paste.
 */
async function claim(channelId, itemId) {
  const r = await rawCall("queue/update", { channel_id: channelId, item_id: itemId, status: "filled" });
  if (retryable(r) || r.status === 401 || r.status === 403) return toResult(r);
  if (r.status >= 200 && r.status < 300) return { ok: true, data: { changed: !!(r.body && r.body.changed === true) } };
  return toResult(r);
}

// ------------------------------------------------------------------ per-conversation memory for the content script

const stateKey = (ch) => `conv:${ch}`;

async function stateGet(ch) {
  const k = stateKey(ch);
  const s = await chrome.storage.session.get(k);
  return { ok: true, data: s[k] || null };
}

async function stateSet(ch, data) {
  if (!data || typeof data !== "object") return { ok: false, error: ERR.internal };
  const json = JSON.stringify(data);
  if (json.length > 200000) return { ok: false, error: ERR.internal };
  await chrome.storage.session.set({ [stateKey(ch)]: JSON.parse(json) });
  return { ok: true };
}

// ------------------------------------------------------------------ open KOL Mail on a conversation

async function openKolMail(channelId) {
  const { port } = await getSettings();
  const base = `http://127.0.0.1:${port}/`;
  const url = `${base}#chat=dc:${channelId}`;
  // Without the "tabs" permission a tab's url is visible only where the host permission (127.0.0.1) covers it.
  const tabs = (await chrome.tabs.query({})).filter((t) => typeof t.url === "string" && t.url.startsWith(base));
  const tab = tabs.find((t) => t.active) || tabs[0];
  if (tab) {
    await chrome.tabs.update(tab.id, { url, active: true });
    if (tab.windowId != null) await chrome.windows.update(tab.windowId, { focused: true });
  } else {
    await chrome.tabs.create({ url, active: true });
  }
  return { ok: true };
}

// ------------------------------------------------------------------ messages from the content script and the popup

function senderKind(sender) {
  if (!sender || sender.id !== chrome.runtime.id) return null;
  if (sender.tab) {
    let origin = sender.origin || "";
    if (!origin) { try { origin = new URL(sender.url || "").origin; } catch (e) { origin = ""; } }
    return DISCORD_ORIGINS.has(origin) ? "discord" : null;
  }
  return typeof sender.url === "string" && sender.url.startsWith(chrome.runtime.getURL("")) ? "extension" : null;
}

/** The DM channel the sending Discord page is on right now (its address), or null. */
function senderChannel(sender) {
  try {
    const m = /^\/channels\/@me\/(\d{5,25})(?:\/\d{5,25})?\/?$/.exec(new URL(sender.url || (sender.tab && sender.tab.url) || "").pathname);
    return m ? m[1] : null;
  } catch (e) {
    return null;
  }
}

function validSync(p) {
  return Array.isArray(p.messages) && p.messages.length <= 50 && p.peer && typeof p.peer === "object" &&
    p.me && typeof p.me === "object";
}

async function handle(msg, sender, kind) {
  if (!msg || typeof msg !== "object") return { ok: false, error: ERR.internal };
  const ch = String(msg.channel_id || (msg.payload && msg.payload.channel_id) || "");
  // Everything about a conversation must come from a page showing that conversation: a reply for one
  // DM can never be read, claimed or recorded from another.
  const sameDm = kind === "discord" && CHANNEL_ID.test(ch) && senderChannel(sender) === ch;
  switch (msg.type) {
    case "kol": {
      const op = msg.op;
      const p = msg.payload && typeof msg.payload === "object" && !Array.isArray(msg.payload) ? msg.payload : null;
      if (!OPS.has(op) || !p || op === "queue/update") return { ok: false, error: ERR.internal };
      if (op === "hello") return callKolMail("hello", {});
      if (!sameDm || String(p.channel_id) !== ch) return { ok: false, error: ERR.internal };
      if (op === "sync" && !validSync(p)) return { ok: false, error: ERR.internal };
      if (op === "queue") await flushOutbox(); // status changes first, so the queue is not stale
      return callKolMail(op, op === "queue" ? { channel_id: p.channel_id } : p);
    }
    case "claim":
    case "update": {
      const itemId = typeof msg.item_id === "string" ? msg.item_id : "";
      const status = msg.type === "claim" ? "filled" : msg.status;
      if (!sameDm || !itemId || itemId.length > 200 || !STATUSES.has(status)) return { ok: false, error: ERR.internal };
      return msg.type === "claim" ? claim(ch, itemId) : enqueueUpdate(ch, itemId, status);
    }
    case "open":
      if (!CHANNEL_ID.test(ch)) return { ok: false, error: ERR.internal };
      return openKolMail(ch);
    case "state_get":
      if (!sameDm) return { ok: false, error: ERR.internal };
      return stateGet(ch);
    case "state_set":
      if (!sameDm) return { ok: false, error: ERR.internal };
      return stateSet(ch, msg.data);
    case "ui_get": {
      if (kind !== "discord") return { ok: false, error: ERR.internal };
      const s = await chrome.storage.local.get("ui_collapsed");
      return { ok: true, data: { collapsed: !!s.ui_collapsed } };
    }
    case "ui_set":
      if (kind !== "discord") return { ok: false, error: ERR.internal };
      await chrome.storage.local.set({ ui_collapsed: !!msg.collapsed });
      return { ok: true };
    default:
      return { ok: false, error: ERR.internal };
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const kind = senderKind(sender);
  if (!kind) return false;
  handle(msg, sender, kind).then(sendResponse, (e) => {
    console.warn("KOL Mail bridge:", e);
    sendResponse({ ok: false, code: "internal", error: ERR.internal });
  });
  return true; // async response
});
