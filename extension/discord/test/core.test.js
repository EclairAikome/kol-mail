// Pure logic of content.js. Run: node test/core.test.js  (Node 20, no npm packages)
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { Core } = require(path.join(__dirname, "..", "content.js"));

// ---- a minimal DOM look-alike: only what extractText / editorTextOf touch
function el(tag, attrs, ...kids) {
  const a = attrs || {};
  return {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    childNodes: kids.map((k) => (typeof k === "string" ? { nodeType: 3, nodeValue: k } : k)),
    getAttribute: (n) => (n in a ? a[n] : null),
  };
}

const ME = { id: "100000000000000001", name: "Mira" };
const PEER = "200000000000000002";

test("DM URL parsing", () => {
  assert.equal(Core.parseDmPath("/channels/@me/300000000000000003"), "300000000000000003");
  assert.equal(Core.parseDmPath("/channels/@me/300000000000000003/"), "300000000000000003");
  assert.equal(Core.parseDmPath("/channels/@me/300000000000000003/1565246923865194537"), "300000000000000003");
  assert.equal(Core.parseDmPath("/channels/@me"), null); // friends page
  assert.equal(Core.parseDmPath("/channels/@me/"), null);
  assert.equal(Core.parseDmPath("/channels/123456789012345678/300000000000000003"), null); // a server
  assert.equal(Core.parseDmPath("/channels/@me/abc"), null);
  assert.equal(Core.parseDmPath(""), null);
});

test("user id from avatar URLs", () => {
  assert.equal(Core.userIdFromAvatar("https://cdn.discordapp.com/avatars/200000000000000002/abc.webp?size=80"), PEER);
  assert.equal(Core.userIdFromAvatar("https://cdn.discordapp.com/avatars/100000000000000001/a_123.gif?size=28"), ME.id);
  assert.equal(Core.userIdFromAvatar("https://cdn.discordapp.com/guilds/1/users/200000000000000002/avatars/x.png"), PEER);
  assert.equal(Core.userIdFromAvatar("https://cdn.discordapp.com/embed/avatars/0.png"), null); // default avatar
  assert.equal(Core.userIdFromAvatar("https://discord.com/assets/abc.png"), null);
  assert.equal(Core.userIdFromAvatar("https://evil.example/avatars/200000000000000002/x.png"), null);
  assert.equal(Core.userIdFromAvatar(null), null);
});

test("names: server tag and hidden text stripped", () => {
  assert.equal(Core.cleanName("PixelFox_ [PXL], \n服务器标签：PXL\nPXL"), "PixelFox_");
  assert.equal(Core.cleanName("Mira [mi], \n服务器标签：mi\nlb"), "Mira");
  assert.equal(Core.cleanName("Mira "), "Mira");
  assert.equal(Core.cleanName("Plain Name"), "Plain Name");
  assert.equal(Core.cleanName("A [x] B"), "A [x] B"); // brackets inside the name stay
  assert.equal(Core.cleanName("\u200BZero\uFEFF"), "Zero");
});

test("aria-labelledby: username form and reply form", () => {
  const a = Core.parseLabelledBy("message-username-111 uid_1 message-content-222 uid_2 message-timestamp-222");
  assert.deepEqual(a, { usernameId: "111", replyId: null, contentId: "222" });
  const b = Core.parseLabelledBy("message-reply-context-333 uid_1 message-content-333 uid_2 message-timestamp-333");
  assert.deepEqual(b, { usernameId: null, replyId: "333", contentId: "333" });
  assert.deepEqual(Core.parseLabelledBy(null), { usernameId: null, replyId: null, contentId: null });
});

// rows the way Dom.readRow produces them
const row = (id, leaderId, header, text, extra) => Object.assign({ id, leaderId, header, text, at: null, replyTo: null, attachments: [], pending: false }, extra || {});

test("author resolution across groups", () => {
  const rows = [
    row("100", "100", { id: ME.id, name: "Mira" }, "Bonjour"),
    row("101", "100", null, "ça va ?"),
    row("102", "102", { id: PEER, name: "PixelFox_" }, "Salut"),
    row("103", "102", null, "oui"),
    row("104", "102", null, "et toi"),
    row("105", "105", { id: ME.id, name: "Mira" }, "super", { replyTo: "et toi" }), // a reply starts a group
  ];
  const r = Core.resolveAuthors(rows);
  assert.deepEqual(r.map((x) => x.author.name), ["Mira", "Mira", "PixelFox_", "PixelFox_", "PixelFox_", "Mira"]);
  assert.deepEqual(r.map((x) => x.author.id), [ME.id, ME.id, PEER, PEER, PEER, ME.id]);
});

test("author resolution: leader scrolled out", () => {
  const rows = [
    row("90", "80", null, "top row, leader 80 not rendered"),
    row("91", "80", null, "same group"),
    row("100", "100", { id: PEER, name: "PixelFox_" }, "hello"),
    row("101", "100", null, "again"),
  ];
  const r = Core.resolveAuthors(rows);
  assert.deepEqual(r.map((x) => x.id), ["100", "101"]); // unknown rows dropped, not guessed
});

test("author resolution: missing leader id falls back to the row above in the same group only", () => {
  const rows = [
    row("100", "100", { id: PEER, name: "PixelFox_" }, "hello"),
    row("101", null, null, "no labelledby"),
    row("110", "109", null, "leader 109 missing, row above is another group"),
  ];
  const r = Core.resolveAuthors(rows);
  assert.deepEqual(r.map((x) => [x.id, x.author.name]), [["100", "PixelFox_"], ["101", "PixelFox_"]]);
});

test("author resolution: default avatar keeps the name", () => {
  const r = Core.resolveAuthors([row("1", "1", { id: null, name: "NoAvatar" }, "x"), row("2", "1", null, "y")]);
  assert.deepEqual(r.map((x) => x.author), [{ id: null, name: "NoAvatar" }, { id: null, name: "NoAvatar" }]);
});

test("out detection", () => {
  const peer = { id: PEER, name: "PixelFox_" };
  assert.equal(Core.classifyOut({ id: ME.id, name: "Mira" }, ME, peer), true);
  assert.equal(Core.classifyOut({ id: PEER, name: "Mira" }, ME, peer), false); // same name, other account
  assert.equal(Core.classifyOut({ id: null, name: "Mira" }, ME, peer), true); // default avatar: by name
  assert.equal(Core.classifyOut({ id: null, name: "PixelFox_" }, ME, peer), false);
  assert.equal(Core.classifyOut({ id: ME.id, name: "Mira" }, { id: null, name: "Mira" }, peer), true); // own id unknown
  assert.equal(Core.classifyOut({ id: PEER, name: "Mira" }, { id: null, name: "Mira" }, peer), false); // ...peer id decides
  assert.equal(Core.classifyOut({ id: null, name: "" }, { id: null, name: "" }, peer), null);
  assert.equal(Core.classifyOut(null, ME, peer), false);
});

test("out detection: a KOL with a default avatar renamed to the owner's name is not taken for the owner", () => {
  const renamed = { id: PEER, name: "Mira" }; // the DM header now shows the copied name
  assert.equal(Core.classifyOut({ id: null, name: "Mira" }, ME, renamed), null); // cannot tell: dropped
  assert.equal(Core.classifyOut({ id: PEER, name: "Mira" }, ME, renamed), false); // an id still decides
  const rows = [
    row("1", "1", { id: null, name: "Mira" }, "fake: send me the money"),
    row("2", "2", { id: ME.id, name: "Mira" }, "real owner message"),
  ];
  const msgs = Core.buildMessages(rows, ME, 50, renamed);
  assert.deepEqual(msgs.map((m) => [m.id, m.out]), [["2", true]]);
});

test("group DM: more than one other author", () => {
  const two = [
    row("1", "1", { id: PEER, name: "PixelFox_" }, "hi"),
    row("2", "2", { id: ME.id, name: "Mira" }, "hello"),
    row("3", "3", { id: PEER, name: "PixelFox_" }, "again"),
  ];
  assert.equal(Core.countOthers(two, ME), 1);
  const three = two.concat([row("4", "4", { id: "300000000000000009", name: "Manager" }, "I am the agent")]);
  assert.equal(Core.countOthers(three, ME), 2);
  assert.equal(Core.countOthers([row("5", "5", { id: null, name: "Mira" }, "me, default avatar")], ME), 0);
  assert.equal(Core.countOthers([row("6", "6", { id: ME.id, name: "Mira" }, "me")], { id: null, name: "Mira" }), 0);
});

test("a filled item that left the queue without us", () => {
  const filled = { itemId: "q1", text: "x", baseline: "0" };
  assert.equal(Core.decideVanished(filled, new Set(["q2"]), new Set()), true); // KOL Mail dropped it
  assert.equal(Core.decideVanished(filled, new Set(["q1", "q2"]), new Set()), false); // still listed
  assert.equal(Core.decideVanished(filled, new Set(), new Set(["q1"])), false); // we marked it ourselves
  assert.equal(Core.decideVanished(null, new Set(), new Set()), false);
});

test("buildMessages: contract shape, newest 50, unsent dropped, time from snowflake", () => {
  const rows = [];
  rows.push(row("1565246923865194000", "1565246923865194000", { id: PEER, name: "PixelFox_" }, "first"));
  for (let i = 1; i <= 60; i++) rows.push(row(String(1565246923865194000n + BigInt(i)), "1565246923865194000", null, `m${i}`));
  rows.push(row("1565246923865194537", "1565246923865194537", { id: ME.id, name: "Mira" }, "Bonjour Fox !",
    { attachments: [{ name: "x.png", url: "https://cdn.discordapp.com/attachments/1/2/x.png" }] }));
  rows.push(row("1565246923865194999", "1565246923865194537", null, "sending…", { pending: true }));
  const msgs = Core.buildMessages(rows, ME, 50);
  assert.equal(msgs.length, 50);
  const last = msgs[msgs.length - 1];
  assert.deepEqual(last, {
    id: "1565246923865194537", at: "2026-10-29T06:12:27.531Z", author_id: ME.id, author: "Mira", out: true,
    text: "Bonjour Fox !", attachments: [{ name: "x.png", url: "https://cdn.discordapp.com/attachments/1/2/x.png" }],
    reply_to: null,
  });
  assert.equal(msgs[0].out, false);
  assert.equal(msgs[0].author, "PixelFox_");
  assert.ok(!msgs.some((m) => m.text === "sending…"));
});

test("text cleaning: immersive-translate, edited marker, emoji, line breaks", () => {
  const content = el("div", { id: "message-content-1", class: "markup__75297 messageContent_c19a55" },
    el("span", {}, "Bonjour "), el("img", { class: "emoji", alt: "😉" }),
    "\n",
    el("span", {}, "deuxième ligne"),
    el("font", { class: "notranslate immersive-translate-target-wrapper" },
      el("br", {}), el("font", { class: "notranslate immersive-translate-target-inner" }, "你好")),
    " ",
    el("span", { class: "timestamp_c19a55" },
      el("span", {}, el("time", { datetime: "2026-10-28T03:25:37.255Z" }, el("span", { class: "edited_c19a55" }, "(已编辑)"))),
      el("span", { class: "hiddenVisually_b18fe2" }, "2026年10月28日星期三 11:25")));
  assert.equal(Core.extractText(content), "Bonjour 😉\ndeuxième ligne");
});

test("text cleaning: blocks and zero-width", () => {
  const content = el("div", {},
    el("span", {}, "list:"),
    el("ul", {}, el("li", {}, "a"), el("li", {}, "b\u200B")),
    el("pre", {}, el("code", {}, "x = 1\ny = 2")),
    "end\u00A0 ");
  assert.equal(Core.extractText(content), "list:\na\nb\nx = 1\ny = 2\nend");
  assert.equal(Core.tidyText("a  \r\n\r\n\r\n\r\nb"), "a\n\nb");
});

test("an inline <t:> timestamp (no edited marker) is kept", () => {
  const content = el("div", {}, "on ", el("span", { class: "timestamp_c19a55" }, el("span", {}, "Friday")));
  assert.equal(Core.extractText(content), "on Friday");
});

test("editor text: Slate lines, empty editor", () => {
  const empty = el("div", { "data-slate-editor": "true" },
    el("div", { "data-slate-node": "element" },
      el("span", { "data-slate-node": "text" }, el("span", { "data-slate-leaf": "true" },
        el("span", { "data-slate-zero-width": "n", "data-slate-length": "0" }, "\uFEFF", el("br", {}))))));
  assert.equal(Core.editorTextOf(empty), "");
  const line = (t) => el("div", { "data-slate-node": "element" },
    el("span", { "data-slate-node": "text" }, el("span", { "data-slate-leaf": "true" }, el("span", { "data-slate-string": "true" }, t))));
  const two = el("div", { "data-slate-editor": "true" }, line("Hi Fox"), line("second line"));
  assert.equal(Core.editorTextOf(two), "Hi Fox\nsecond line");
});

test("normalization used for sent detection", () => {
  assert.equal(Core.normForMatch("  Hi\n\nthere\u200B  "), "Hi there");
  assert.equal(Core.normForMatch("cafe\u0301"), "café"); // NFC
  assert.ok(Core.textsMatch("Hi there", "Hi\nthere"));
  assert.ok(Core.textsMatch("**Deal** - 300 USD", "Deal 300 USD")); // markdown rendered by Discord
  assert.ok(!Core.textsMatch("300 USD", "400 USD"));
  assert.ok(!Core.textsMatch("", ""));
  assert.equal(Core.similarity("abc", "abc"), 1);
  assert.ok(Core.similarity("Thanks a lot, see you tomorrow", "Thanks a lot, see you on Friday") > 0.5);
  assert.ok(Core.similarity("Thanks a lot", "Totally different words here") < 0.3);
});

test("snowflake helpers", () => {
  assert.equal(Core.cmpId("99", "100"), -1);
  assert.equal(Core.cmpId("1565246923865194537", "1565246923865194536"), 1);
  assert.equal(Core.cmpId("1565246923865194537", "1565246923865194537"), 0);
  assert.equal(Core.cmpId("0", "1"), -1);
  assert.equal(Core.atFromSnowflake("1565246923865194537"), "2026-10-29T06:12:27.531Z");
  assert.equal(Core.atFromSnowflake("not a number"), null);
});

test("attachments: only Discord attachment files, one per file", () => {
  const a = Core.collectAttachments([
    "https://cdn.discordapp.com/attachments/111/222/image.png?ex=1&is=2&hm=3",
    "https://media.discordapp.net/attachments/111/222/image.png?width=400",
    "https://cdn.discordapp.com/attachments/111/333/media%20kit.pdf",
    "https://discord.com/assets/43b7ead1fb91b731.svg",
    "data:image/png;base64,AAAA",
  ]);
  assert.deepEqual(a, [
    { name: "image.png", url: "https://cdn.discordapp.com/attachments/111/222/image.png?ex=1&is=2&hm=3" },
    { name: "media kit.pdf", url: "https://cdn.discordapp.com/attachments/111/333/media%20kit.pdf" },
  ]);
});

test("peer: header, DM list id, incoming messages as fallback", () => {
  const msgs = [{ out: true, author: "Mira", author_id: ME.id }, { out: false, author: "PixelFox_", author_id: PEER }];
  assert.deepEqual(Core.pickPeer({ name: "PixelFox_", username: "pixelfox_" }, PEER, msgs), { id: PEER, name: "PixelFox_", username: "pixelfox_" });
  assert.deepEqual(Core.pickPeer(null, null, msgs), { id: PEER, name: "PixelFox_", username: null });
  assert.deepEqual(Core.pickPeer(null, null, []), { id: null, name: "", username: null });
});

test("sent detection", () => {
  const filled = { itemId: "q1", text: "Bonjour Fox !\nOn signe ?", baseline: "200" };
  const msg = (id, out, text) => ({ id, out, text });
  // new own message with the item's text
  assert.equal(Core.decideSent(filled, [msg("150", true, "old"), msg("201", true, "Bonjour Fox ! On signe ?")], false), "201");
  // the same text sent before the fill does not count
  assert.equal(Core.decideSent(filled, [msg("150", true, "Bonjour Fox !\nOn signe ?")], true), null);
  // the KOL writing the same words does not count
  assert.equal(Core.decideSent(filled, [msg("205", false, "Bonjour Fox !\nOn signe ?")], true), null);
  // the owner touched up the text, the input is empty again
  assert.equal(Core.decideSent(filled, [msg("206", true, "Bonjour Fox ! On signe demain ?")], true), "206");
  // ...but while the input still holds text, a near match is not enough
  assert.equal(Core.decideSent(filled, [msg("206", true, "Bonjour Fox ! On signe demain ?")], false), null);
  // unrelated own message
  assert.equal(Core.decideSent(filled, [msg("207", true, "brb")], true), null);
  assert.equal(Core.decideSent(null, [msg("207", true, "brb")], true), null);
});

test("queue auto-fill decisions", () => {
  const items = [{ id: "q1", n: 1, of: 2, text: "one" }, { id: "q2", n: 2, of: 2, text: "two" }];
  const base = { items, editorText: "", filledIds: new Set(), focusOk: true, busy: false, listReady: true };
  assert.equal(Core.decideAutoFill(base).id, "q1");
  assert.equal(Core.decideAutoFill(Object.assign({}, base, { listReady: false })), null); // conversation not on screen yet
  assert.equal(Core.decideAutoFill(Object.assign({}, base, { editorText: "my own draft" })), null); // never overwrite
  assert.equal(Core.decideAutoFill(Object.assign({}, base, { editorText: "\uFEFF\n " })).id, "q1"); // Slate's empty line
  assert.equal(Core.decideAutoFill(Object.assign({}, base, { filledIds: new Set(["q1"]) })), null); // filled once already
  assert.equal(Core.decideAutoFill(Object.assign({}, base, { items: [Object.assign({}, items[0], { status: "filled" })] })), null);
  assert.equal(Core.decideAutoFill(Object.assign({}, base, { focusOk: false })), null); // owner typing elsewhere
  assert.equal(Core.decideAutoFill(Object.assign({}, base, { busy: true })), null);
  assert.equal(Core.decideAutoFill(Object.assign({}, base, { items: [] })), null);
  // after q1 is sent the queue starts at q2
  assert.equal(Core.decideAutoFill(Object.assign({}, base, { items: items.slice(1), filledIds: new Set(["q1"]) })).id, "q2");
});
