// Pure logic of page.js. Run: node test/page.test.js  (Node 20, no npm packages)
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { Pure } = require(path.join(__dirname, "..", "page.js"));

const ME = "100000000000000001";
const PEER = "200000000000000002";
const raw = (over) => Object.assign({
  id: "400000000000000001", type: 0, channel_id: "300000000000000003", content: "Hi there",
  timestamp: "2026-11-10T08:00:00.000000+00:00", author: { id: PEER, username: "pixelfox_", global_name: "PixelFox_" },
  attachments: [], mentions: [],
}, over || {});

test("a server message and a store record give the same message", () => {
  const fromServer = Pure.normMessage(raw(), { meId: ME });
  const record = { id: "400000000000000001", type: 0, content: "Hi there", state: "SENT",
    timestamp: new Date("2026-11-10T08:00:00.000Z"), author: { id: PEER, username: "pixelfox_", globalName: "PixelFox_" },
    attachments: [] };
  const fromStore = Pure.normMessage(record, { meId: ME });
  assert.equal(fromServer.author, "PixelFox_");
  assert.equal(fromStore.author, "PixelFox_");
  assert.equal(fromServer.out, false);
  assert.equal(fromStore.text, "Hi there");
  assert.equal(new Date(fromServer.at).getTime(), new Date(fromStore.at).getTime());
  assert.deepEqual(Object.keys(fromServer).sort(), ["at", "attachments", "author", "author_id", "id", "out", "reply_to", "text"]);
});

test("the owner's own messages are told by the account id, never by the name", () => {
  const mine = Pure.normMessage(raw({ author: { id: ME, username: "mira" } }), { meId: ME });
  assert.equal(mine.out, true);
  const sameName = Pure.normMessage(raw({ author: { id: PEER, username: "mira", global_name: "Mira" } }), { meId: ME });
  assert.equal(sameName.out, false);
  assert.equal(Pure.normMessage(raw(), {}).out, false); // not known who the owner is: never taken for theirs
});

test("what is not a sent chat message is left out", () => {
  assert.equal(Pure.normMessage(raw({ id: "abc" }), { meId: ME }), null);
  assert.equal(Pure.normMessage(raw({ type: 3 }), { meId: ME }), null);           // a call
  assert.equal(Pure.normMessage(raw({ type: 6 }), { meId: ME }), null);           // a pin
  assert.equal(Pure.normMessage(raw({ state: "SENDING" }), { meId: ME }), null);  // not accepted by the server yet
  assert.equal(Pure.normMessage(raw({ state: "SEND_FAILED" }), { meId: ME }), null);
  assert.equal(Pure.normMessage(raw({ author: null }), { meId: ME }), null);
  assert.equal(Pure.normMessage(raw({ timestamp: "not a date" }), { meId: ME }), null);
  assert.equal(Pure.normMessage(null, { meId: ME }), null);
  assert.notEqual(Pure.normMessage(raw({ type: 19 }), { meId: ME }), null);       // a reply
});

test("mentions, custom emoji and stickers read like text", () => {
  const m = raw({ content: "hey <@100000000000000001> and <@!555555555555555555> <:pog:123456789012345678> <a:wave:223456789012345678>",
    mentions: [{ id: ME, username: "mira", global_name: "Mira" }] });
  const out = Pure.normMessage(m, { meId: ME, lookup: (id) => (id === "555555555555555555" ? "Bob" : "") });
  assert.equal(out.text, "hey @Mira and @Bob :pog: :wave:");
  const unknown = Pure.normMessage(raw({ content: "<@777777777777777777>" }), { meId: ME });
  assert.equal(unknown.text, "<@777777777777777777>");  // nobody known by that id: left as it is
  const sticker = Pure.normMessage(raw({ content: "", sticker_items: [{ name: "Wave" }] }), { meId: ME });
  assert.equal(sticker.text, "[贴纸 Wave]");
});

test("attachments and the quoted message", () => {
  const m = raw({ attachments: [{ filename: "kit.pdf", url: "https://cdn.discordapp.com/attachments/1/2/kit.pdf?ex=1" }, null],
    referenced_message: { content: "What is your rate?" } });
  const out = Pure.normMessage(m, { meId: ME });
  assert.deepEqual(out.attachments, [{ name: "kit.pdf", url: "https://cdn.discordapp.com/attachments/1/2/kit.pdf?ex=1" }]);
  assert.equal(out.reply_to, "What is your rate?");
  const byRef = Pure.normMessage(raw({ messageReference: { message_id: "400000000000000000" } }),
    { meId: ME, quoted: (id) => (id === "400000000000000000" ? "earlier words" : null) });
  assert.equal(byRef.reply_to, "earlier words");
  assert.equal(Pure.normMessage(raw({ content: "x".repeat(9000) }), { meId: ME }).text.length, 4000);
});

test("a batch keeps the newest hundred, one per id, the later version of an edit", () => {
  const list = [];
  for (let i = 0; i < 130; i++) list.push({ id: String(400000000000000000n + BigInt(i)), text: "v1" });
  list.push({ id: "400000000000000129", text: "edited" });
  list.push({ id: "99999", text: "short id sorts first" });
  const out = Pure.newestBatch(list);
  assert.equal(out.length, 100);
  assert.equal(out.at(-1).id, "400000000000000129");
  assert.equal(out.at(-1).text, "edited");
  assert.equal(out[0].id, "400000000000000030");
});

test("stores and the loader are found among webpack's modules, look-alikes are not", () => {
  const dispatcher = { subscribe() {} };
  const store = (name) => ({ getName: () => name, _dispatcher: dispatcher });
  const everything = new Proxy({}, { get: () => () => "x", ownKeys: () => ["getName", "fetchMessages", "sendMessage"],
    getOwnPropertyDescriptor: () => ({ enumerable: true, configurable: true }) });
  const loader = { fetchMessages() {}, jumpToMessage() {}, receiveMessage() {}, sendMessage() {} };
  const throwing = {};
  Object.defineProperty(throwing, "exports", { get() { throw new Error("not ready"); } });
  const caches = [
    {},
    {
      1: { exports: { A: store("ChannelStore"), B: 5 } },
      2: { exports: store("UserStore") },
      3: { exports: { default: store("MessageStore") } },
      4: { exports: { default: everything } },             // answers every property: not a store, not the loader
      5: { exports: { A: { fetchMessages() {} } } },        // loads something else
      6: { exports: { A: loader } },
      7: throwing,
      8: { exports: { getName: () => "NoDispatcher" } },
      9: { exports: null },
    },
  ];
  const { stores, loader: found } = Pure.scanModules(caches);
  assert.deepEqual(Object.keys(stores).sort(), ["ChannelStore", "MessageStore", "UserStore"]);
  assert.equal(found, loader);
  assert.equal(Pure.storeName(everything), null);
  assert.equal(Pure.isLoader(everything), false);
});
