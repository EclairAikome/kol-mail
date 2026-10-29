// background.js against a mock KOL Mail. Run: node test/background.test.js  (Node 20, no npm packages)
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const vm = require("node:vm");

const TOKEN = "test-token";
const CH = "300000000000000003";
const OTHER = "300000000000000004";
const dmSender = (ch) => ({ id: "ext", tab: { id: 1 }, url: `https://discord.com/channels/@me/${ch}`, origin: "https://discord.com" });
const DISCORD = dmSender(CH);
const POPUP = { id: "ext", url: "chrome-extension://ext/popup.html" };
const plain = (x) => JSON.parse(JSON.stringify(x)); // results come from another realm

// ---- mock KOL Mail, the way app.py answers
const seen = [];
let mode = "ok"; // "ok" | "update404" | "update500" | "oldNoop"
const claimed = new Set();
const server = http.createServer((req, res) => {
  let body = "";
  req.on("data", (c) => (body += c));
  req.on("end", () => {
    const op = req.url.replace("/ext/discord/", "");
    const b = body ? JSON.parse(body) : null;
    seen.push({ op, token: req.headers["x-kol-ext"], body: b });
    const send = (status, obj) => { res.writeHead(status, { "Content-Type": "application/json" }); res.end(JSON.stringify(obj)); };
    if (req.headers["x-kol-ext"] !== TOKEN) return send(401, { error: "bad token" });
    if (op === "hello") return send(200, { ok: true, version: "0.7.0" });
    if (op === "sync") {
      if (body.includes("FAIL500")) return send(500, { ok: false, error: "总表正在被别的程序占用" });
      return send(200, { ok: true, conv: { key: "dc:1", linked: null, suggest: [], added: 0 }, queue: [] });
    }
    if (op === "queue") return send(200, { items: [] });
    if (op === "queue/update") {
      if (mode === "update404") return send(404, { error: "没有这条" });
      if (mode === "update500") return send(500, { error: "数据库忙" });
      if (mode === "oldNoop") return send(200, { ok: false }); // an older answer for an item no longer pending
      if (b.status === "filled") {
        const first = !claimed.has(b.item_id);
        claimed.add(b.item_id);
        return send(200, { ok: true, changed: first });
      }
      return send(200, { ok: true, changed: true });
    }
    send(404, { error: "no route" });
  });
});

// ---- fake chrome APIs, background.js loaded into a fresh context
function load(settings, opts) {
  const local = Object.assign({}, settings);
  const session = {};
  const tabs = [];
  const actions = [];
  const access = [];
  let listener = null;
  const localArea = {
    get: async (keys) => Object.fromEntries([].concat(keys).filter((k) => k in local).map((k) => [k, local[k]])),
    set: async (o) => Object.assign(local, o),
  };
  if (!(opts && opts.noAccessLevel)) localArea.setAccessLevel = async (o) => { access.push(o.accessLevel); };
  const chrome = {
    runtime: { id: "ext", getURL: (p) => "chrome-extension://ext/" + p, onMessage: { addListener: (fn) => (listener = fn) } },
    storage: {
      local: localArea,
      session: {
        get: async (k) => (k in session ? { [k]: JSON.parse(JSON.stringify(session[k])) } : {}),
        set: async (o) => Object.assign(session, JSON.parse(JSON.stringify(o))),
      },
    },
    tabs: {
      query: async (q) => { actions.push(["query", JSON.stringify(q)]); return tabs; },
      update: async (id, p) => { actions.push(["update", id, p.url, p.active]); return {}; },
      create: async (p) => { actions.push(["create", p.url]); return {}; },
    },
    windows: { update: async (id, p) => { actions.push(["focus", id, p.focused]); return {}; } },
  };
  const ctx = vm.createContext({ chrome, fetch, AbortController, setTimeout, clearTimeout, URL, console, JSON, Promise, Date, Math, Object, Array, String, Number, Set });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8"), ctx);
  const send = (msg, sender) => new Promise((resolve) => {
    const async = listener(msg, sender, resolve);
    if (async !== true) resolve("NO-RESPONSE");
  });
  return { send, local, session, tabs, actions, access };
}

const update = (bg, item, status, sender) =>
  bg.send({ type: "update", channel_id: CH, item_id: item, status }, sender || DISCORD).then(plain);

let port;
test.before(() => new Promise((r) => server.listen(0, "127.0.0.1", () => { port = server.address().port; r(); })));
test.after(() => new Promise((r) => server.close(r)));

test("manifest: only storage and the 127.0.0.1 host permission", () => {
  const m = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "manifest.json"), "utf8"));
  assert.deepEqual(m.permissions, ["storage"]);
  assert.deepEqual(m.host_permissions, ["http://127.0.0.1/*"]);
});

test("storage.local is closed to content scripts", async () => {
  const bg = load({ token: TOKEN, port });
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual(bg.access, ["TRUSTED_CONTEXTS"]);
  load({ token: TOKEN, port }, { noAccessLevel: true }); // an older Chrome without it must still start
});

test("hello from the popup", async () => {
  const bg = load({ token: TOKEN, port });
  const r = await bg.send({ type: "kol", op: "hello", payload: {} }, POPUP);
  assert.deepEqual(plain(r), { ok: true, data: { ok: true, version: "0.7.0" } });
  assert.equal(seen.at(-1).token, TOKEN);
});

test("error messages: wrong token, no token, not running, server error field", async () => {
  let r = await load({ token: "nope", port }).send({ type: "kol", op: "hello", payload: {} }, POPUP);
  assert.equal(r.error, "令牌不对，到 KOL Mail 设置 → 私信渠道 复制令牌");
  r = await load({ port }).send({ type: "kol", op: "hello", payload: {} }, POPUP);
  assert.equal(r.code, "auth");
  assert.match(r.error, /还没填令牌/);
  r = await load({ token: TOKEN, port: 1 }).send({ type: "kol", op: "hello", payload: {} }, POPUP);
  assert.equal(r.error, "KOL Mail 没在运行");
  const payload = { channel_id: CH, peer: { id: null, name: "FAIL500", username: null }, me: { id: null, name: "Mira" }, messages: [] };
  r = await load({ token: TOKEN, port }).send({ type: "kol", op: "sync", payload }, DISCORD);
  assert.deepEqual([r.ok, r.error], [false, "总表正在被别的程序占用"]);
});

test("who may ask for what", async () => {
  const bg = load({ token: TOKEN, port });
  const payload = { channel_id: CH, peer: { id: null, name: "PixelFox_", username: null }, me: { id: null, name: "Mira" }, messages: [] };
  assert.equal((await bg.send({ type: "kol", op: "sync", payload }, DISCORD)).ok, true);
  assert.equal(await bg.send({ type: "kol", op: "sync", payload }, { id: "ext", tab: { id: 2 }, url: "https://evil.example/", origin: "https://evil.example" }), "NO-RESPONSE");
  assert.equal(await bg.send({ type: "kol", op: "sync", payload }, { id: "other-extension", url: "chrome-extension://other/x.html" }), "NO-RESPONSE");
  assert.equal((await bg.send({ type: "kol", op: "sync", payload }, POPUP)).ok, false); // the popup only tests the connection
  assert.equal((await bg.send({ type: "kol", op: "sync", payload: Object.assign({}, payload, { channel_id: "../x" }) }, DISCORD)).ok, false);
  assert.equal((await bg.send({ type: "kol", op: "boom", payload: {} }, DISCORD)).ok, false); // only the four ops
  assert.equal((await bg.send({ type: "kol", op: "queue/update", payload: {} }, DISCORD)).ok, false); // goes through "update"
  const tooMany = Object.assign({}, payload, { messages: new Array(51).fill({}) });
  assert.equal((await bg.send({ type: "kol", op: "sync", payload: tooMany }, DISCORD)).ok, false);
});

test("a page on another DM (or a server channel) cannot act for this one", async () => {
  const bg = load({ token: TOKEN, port });
  const payload = { channel_id: CH, peer: { id: null, name: "PixelFox_", username: null }, me: { id: null, name: "Mira" }, messages: [] };
  const elsewhere = [dmSender(OTHER), { id: "ext", tab: { id: 1 }, url: "https://discord.com/channels/1/2", origin: "https://discord.com" }];
  const before = seen.length;
  for (const s of elsewhere) {
    assert.equal((await bg.send({ type: "kol", op: "sync", payload }, s)).ok, false);
    assert.equal((await bg.send({ type: "kol", op: "queue", payload: { channel_id: CH } }, s)).ok, false);
    assert.equal((await bg.send({ type: "claim", channel_id: CH, item_id: "q9" }, s)).ok, false);
    assert.equal((await update(bg, "q9", "sent", s)).ok, false);
    assert.equal((await bg.send({ type: "state_get", channel_id: CH }, s)).ok, false);
    assert.equal((await bg.send({ type: "state_set", channel_id: CH, data: {} }, s)).ok, false);
  }
  assert.equal(seen.length, before); // nothing reached KOL Mail
  assert.equal((await bg.send({ type: "kol", op: "queue", payload: { channel_id: CH } }, DISCORD)).ok, true);
});

test("an answer that is not an outage is final: the outbox never sticks (stuck.js)", async () => {
  const bg = load({ token: TOKEN, port });
  mode = "oldNoop";
  try {
    const r = await update(bg, "q1", "sent");
    assert.equal(r.queued, undefined);
    assert.equal((bg.session.outbox || []).length, 0);
  } finally {
    mode = "ok";
  }
  const before = seen.length;
  assert.deepEqual(await update(bg, "q2", "sent"), { ok: true, data: { changed: true } });
  await bg.send({ type: "kol", op: "queue", payload: { channel_id: CH } }, DISCORD);
  assert.deepEqual(seen.slice(before).map((s) => s.op + ":" + (s.body.item_id || "")), ["queue/update:q2", "queue:"]);
  assert.equal(bg.session.outbox.length, 0);
});

test("changed:false is passed on, and is final", async () => {
  const bg = load({ token: TOKEN, port });
  claimed.add("q20");
  assert.deepEqual(await update(bg, "q20", "filled"), { ok: true, data: { changed: false } });
  assert.equal(bg.session.outbox.length, 0);
});

test("queue/update waits in the outbox while KOL Mail is down, and goes out before the next queue read", async () => {
  const bg = load({ token: TOKEN, port: 1 }); // nothing listens on port 1
  const r = await update(bg, "q1", "sent");
  assert.deepEqual([r.ok, r.queued, r.error], [false, true, "KOL Mail 没在运行"]);
  assert.equal(bg.session.outbox.length, 1);
  bg.local.port = port; // KOL Mail is back
  const before = seen.length;
  const q = await bg.send({ type: "kol", op: "queue", payload: { channel_id: CH } }, DISCORD);
  assert.equal(q.ok, true);
  assert.deepEqual(seen.slice(before).map((s) => s.op), ["queue/update", "queue"]);
  assert.deepEqual(seen[before].body, { channel_id: CH, item_id: "q1", status: "sent" });
  assert.equal(bg.session.outbox.length, 0);
});

test("a 5xx is retried, a 4xx is not", async () => {
  const bg = load({ token: TOKEN, port });
  mode = "update500";
  try {
    const r = await update(bg, "q30", "dropped");
    assert.deepEqual([r.ok, r.queued], [false, true]);
    assert.equal(bg.session.outbox.length, 1);
    assert.equal(bg.session.outbox[0].tries, 1);
  } finally {
    mode = "ok";
  }
  await bg.send({ type: "kol", op: "queue", payload: { channel_id: CH } }, DISCORD);
  assert.equal(bg.session.outbox.length, 0);
  mode = "update404";
  try {
    const r = await update(bg, "gone", "dropped");
    assert.deepEqual([r.ok, r.error], [false, "没有这条"]);
    assert.equal(bg.session.outbox.length, 0);
  } finally {
    mode = "ok";
  }
  assert.equal((await update(bg, "q2", "posted")).ok, false);
});

test("outbox entries give up after 10 minutes or 30 tries", async () => {
  const bg = load({ token: TOKEN, port });
  bg.session.outbox = [
    { uid: "a", channel_id: CH, item_id: "old", status: "sent", at: Date.now() - 11 * 60 * 1000, tries: 0 },
    { uid: "b", channel_id: CH, item_id: "tired", status: "sent", at: Date.now(), tries: 30 },
    { uid: "c", channel_id: CH, item_id: "fresh", status: "sent", at: Date.now(), tries: 2 },
  ];
  const before = seen.length;
  await bg.send({ type: "kol", op: "queue", payload: { channel_id: CH } }, DISCORD);
  assert.deepEqual(seen.slice(before).map((s) => s.op + ":" + (s.body.item_id || "")), ["queue/update:fresh", "queue:"]);
  assert.equal(bg.session.outbox.length, 0);
});

test("claim: one caller wins, no answer means no paste and nothing queued", async () => {
  const bg = load({ token: TOKEN, port });
  const first = plain(await bg.send({ type: "claim", channel_id: CH, item_id: "q40" }, DISCORD));
  const second = plain(await bg.send({ type: "claim", channel_id: CH, item_id: "q40" }, DISCORD)); // the other tab
  assert.deepEqual([first, second], [{ ok: true, data: { changed: true } }, { ok: true, data: { changed: false } }]);
  const down = load({ token: TOKEN, port: 1 });
  const r = plain(await down.send({ type: "claim", channel_id: CH, item_id: "q41" }, DISCORD));
  assert.deepEqual([r.ok, r.code], [false, "offline"]);
  assert.equal((down.session.outbox || []).length, 0);
});

test("open KOL Mail: reuse a tab, else create one", async () => {
  const bg = load({ token: TOKEN, port: 8765 });
  bg.tabs.push({ id: 5, windowId: 2, active: true }); // a tab whose url we may not see
  bg.tabs.push({ id: 6, windowId: 2, active: true, url: "http://127.0.0.1:9999/" }); // another port
  bg.tabs.push({ id: 7, windowId: 3, active: false, url: "http://127.0.0.1:8765/" });
  await bg.send({ type: "open", channel_id: CH }, DISCORD);
  assert.deepEqual(bg.actions, [
    ["query", "{}"],
    ["update", 7, `http://127.0.0.1:8765/#chat=dc:${CH}`, true],
    ["focus", 3, true],
  ]);
  const bg2 = load({ token: TOKEN });
  await bg2.send({ type: "open", channel_id: CH }, DISCORD);
  assert.deepEqual(bg2.actions.at(-1), ["create", `http://127.0.0.1:8765/#chat=dc:${CH}`]); // default port
});

test("conversation memory and the panel's collapsed state", async () => {
  const bg = load({ token: TOKEN, port });
  const data = { filled: { itemId: "q1", text: "hi", baseline: "5" }, filledIds: ["q1"], finalIds: [] };
  assert.equal((await bg.send({ type: "state_set", channel_id: CH, data }, DISCORD)).ok, true);
  assert.deepEqual(plain((await bg.send({ type: "state_get", channel_id: CH }, DISCORD)).data), data);
  assert.equal((await bg.send({ type: "state_get", channel_id: CH }, POPUP)).ok, false);
  assert.deepEqual(plain(await bg.send({ type: "ui_get" }, DISCORD)), { ok: true, data: { collapsed: false } });
  assert.equal((await bg.send({ type: "ui_set", collapsed: true }, DISCORD)).ok, true);
  assert.deepEqual(plain(await bg.send({ type: "ui_get" }, DISCORD)), { ok: true, data: { collapsed: true } });
  assert.equal(bg.local.token, TOKEN); // ui_set touches nothing else
});
