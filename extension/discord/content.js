/*
 * KOL Mail Discord bridge, content script (runs on discord.com).
 *
 * Reads the DM the owner has open, hands it to the background service worker (the only part that
 * talks to KOL Mail), and puts KOL Mail's approved replies into Discord's input box. It never sends
 * anything to Discord: no Enter key, no send button, no API calls, no token. The owner presses Enter.
 *
 * Three layers:
 *   Core  pure functions on plain values; tested with node (test/core.test.js)
 *   Dom   reads Discord's page into plain values; checked with test/fixture.html
 *   App   timers, the floating panel, filling the input; only runs as a real content script
 */
(function () {
  "use strict";

  // =================================================================================== Core

  const ZERO_WIDTH = /[\u200B-\u200D\u2060\uFEFF]/g;
  const DISCORD_EPOCH = 1420070400000n;

  /** Channel id of a DM page (/channels/@me/<id>, or /<id>/<messageId> for a jump link), else null. */
  function parseDmPath(pathname) {
    const m = /^\/channels\/@me\/(\d{5,25})(?:\/\d{5,25})?\/?$/.exec(String(pathname || ""));
    return m ? m[1] : null;
  }

  /** User id inside a Discord avatar URL. Default avatars carry no id: null, as for any other host. */
  function userIdFromAvatar(src) {
    const s = String(src || "");
    if (!/^https:\/\/(?:cdn\.discordapp\.com|media\.discordapp\.net)\//.test(s)) return null;
    const m = /\/users\/(\d{15,22})\/avatars\//.exec(s) || /^https:\/\/[^/]+\/avatars\/(\d{15,22})\//.exec(s);
    return m ? m[1] : null;
  }

  /**
   * Display name from a header's text. Discord appends a server tag and hidden a11y text, e.g.
   * "PixelFox_ [PXL], \n服务器标签：PXL\nPXL": keep the first line, drop the trailing comma and " [TAG]".
   */
  function cleanName(raw) {
    let s = String(raw || "").split("\n")[0].replace(ZERO_WIDTH, "").trim();
    s = s.replace(/[,，]\s*$/, "").trim();
    s = s.replace(/\s*\[[^[\]\n]{1,16}\]$/, "").trim();
    return s.replace(/[,，]\s*$/, "").trim();
  }

  /** Ids named in a message article's aria-labelledby. Replies name their reply context, not the username. */
  function parseLabelledBy(value) {
    const out = { usernameId: null, replyId: null, contentId: null };
    for (const tok of String(value || "").split(/\s+/)) {
      let m;
      if ((m = /^message-username-(\d+)$/.exec(tok))) out.usernameId = m[1];
      else if ((m = /^message-reply-context-(\d+)$/.exec(tok))) out.replyId = m[1];
      else if ((m = /^message-content-(\d+)$/.exec(tok))) out.contentId = m[1];
    }
    return out;
  }

  /** Text as recorded: line breaks kept, zero-width and trailing spaces gone, at most one blank line in a row. */
  function tidyText(s) {
    return String(s || "")
      .replace(/\r\n?/g, "\n")
      .replace(ZERO_WIDTH, "")
      .replace(/\u00A0/g, " ")
      .split("\n").map((l) => l.replace(/[ \t]+$/, "")).join("\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  /** Comparison form: every run of whitespace (line breaks too) is one space. */
  function normForMatch(s) {
    return String(s || "").normalize("NFC").replace(ZERO_WIDTH, "").replace(/\s+/g, " ").trim();
  }

  /** Letters and digits only, lower case: survives Discord rendering markdown (**, -, >) and emoji. */
  function looseKey(s) {
    return normForMatch(s).toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");
  }

  /** The rendered message is this queue item, allowing for whitespace and markdown rendering. */
  function textsMatch(a, b) {
    const x = normForMatch(a);
    if (x && x === normForMatch(b)) return true;
    const k = looseKey(a);
    return k.length > 0 && k === looseKey(b);
  }

  /** Dice coefficient on character bigrams of looseKey: 1 for the same text, 0 for nothing shared. */
  function similarity(a, b) {
    const x = Array.from(looseKey(a));
    const y = Array.from(looseKey(b));
    if (!x.length || !y.length) return 0;
    if (x.join("") === y.join("")) return 1;
    if (x.length < 2 || y.length < 2) return 0;
    const grams = new Map();
    for (let i = 0; i < x.length - 1; i++) {
      const g = x[i] + x[i + 1];
      grams.set(g, (grams.get(g) || 0) + 1);
    }
    let hit = 0;
    for (let i = 0; i < y.length - 1; i++) {
      const g = y[i] + y[i + 1];
      const c = grams.get(g);
      if (c) { hit++; grams.set(g, c - 1); }
    }
    return (2 * hit) / (x.length - 1 + y.length - 1);
  }

  /** Compare two snowflake ids (decimal strings, too big for Number). */
  function cmpId(a, b) {
    const x = String(a || "0").replace(/^0+(?=\d)/, "");
    const y = String(b || "0").replace(/^0+(?=\d)/, "");
    if (x.length !== y.length) return x.length < y.length ? -1 : 1;
    return x < y ? -1 : x > y ? 1 : 0;
  }

  /** A snowflake encodes its creation time; used when a row has no <time>. */
  function atFromSnowflake(id) {
    try {
      return new Date(Number((BigInt(id) >> 22n) + DISCORD_EPOCH)).toISOString();
    } catch (e) {
      return null;
    }
  }

  const ATTACHMENT_URL = /^https:\/\/(?:cdn\.discordapp\.com|media\.discordapp\.net)\/attachments\/\d+\/\d+\/[^/?#]+/;

  /** Attachment links (originals first) to [{name, url}], one per file, other links ignored. */
  function collectAttachments(urls) {
    const seen = new Set();
    const out = [];
    for (const u of urls || []) {
      const s = String(u || "");
      const m = ATTACHMENT_URL.exec(s);
      if (!m) continue;
      const path = m[0].replace(/^https:\/\/[^/]+/, "");
      if (seen.has(path)) continue;
      seen.add(path);
      let name = path.split("/").pop();
      try { name = decodeURIComponent(name); } catch (e) { /* keep it encoded */ }
      out.push({ name, url: s });
    }
    return out;
  }

  /**
   * Give every row its author. Only a group's first row (the leader) shows name and avatar; the rest
   * name the leader in aria-labelledby. A leader scrolled out of the rendered window leaves its rows
   * unknown, unless the row above is in the same group. Unknown rows are dropped: guessing would
   * record the owner's own words as the KOL's. They come back once the owner scrolls up.
   */
  function resolveAuthors(rows) {
    const byId = new Map();
    const out = [];
    let prev = null; // { author, group } of the row above
    for (const r of rows || []) {
      let author = null;
      const group = r.header ? r.id : r.leaderId || null;
      if (r.header && (r.header.id || r.header.name)) {
        author = { id: r.header.id || null, name: r.header.name || "" };
      } else if (!r.header && r.leaderId && byId.has(r.leaderId)) {
        author = byId.get(r.leaderId);
      } else if (!r.header && prev && prev.author && (!r.leaderId || prev.group === r.leaderId)) {
        author = prev.author;
      }
      if (author) byId.set(r.id, author);
      prev = { author, group };
      if (author) out.push(Object.assign({}, r, { author }));
    }
    return out;
  }

  /**
   * Did the owner write this? true / false, or null when it cannot be told safely (the row is then dropped).
   * Ids decide whenever they are known. A default avatar carries no id and leaves only the display name,
   * which anyone can copy: if the KOL's current name (the DM header) equals the owner's, names prove nothing.
   * peer = { id, name } of the other side of this 1:1 DM.
   */
  function classifyOut(author, me, peer) {
    if (!author || !me) return false;
    if (author.id && me.id) return author.id === me.id;
    if (author.id && peer && peer.id) return author.id !== peer.id; // a 1:1 DM has two authors
    if (!author.name) return null;
    if (peer && peer.name && me.name && peer.name === me.name) return null;
    return !!me.name && author.name === me.name;
  }

  /** Rows (oldest first) to the sync contract's messages: the newest `limit`, without unsent or unclear ones. */
  function buildMessages(rows, me, limit, peer) {
    const out = [];
    for (const r of resolveAuthors(rows)) {
      if (r.pending) continue;
      const o = classifyOut(r.author, me, peer);
      if (o === null) continue;
      out.push({
        id: r.id,
        at: r.at || atFromSnowflake(r.id),
        author_id: r.author.id || null,
        author: r.author.name || "",
        out: o,
        text: r.text || "",
        attachments: r.attachments || [],
        reply_to: r.replyTo || null,
      });
    }
    return out.slice(-(limit || 50));
  }

  /** How many different people other than the owner wrote in the rendered rows. More than one: a group DM. */
  function countOthers(rows, me) {
    const keys = new Set();
    for (const r of resolveAuthors(rows)) {
      const a = r.author;
      const mine = a.id && me && me.id ? a.id === me.id : !!(me && me.name && a.name === me.name);
      if (!mine) keys.add(a.id ? "id:" + a.id : "name:" + a.name);
    }
    return keys.size;
  }

  /** The item in the input left the queue without us marking it: KOL Mail dropped it (the KOL wrote again). */
  function decideVanished(filled, listedIds, finalIds) {
    return !!filled && !listedIds.has(filled.itemId) && !(finalIds && finalIds.has(filled.itemId));
  }

  /** The KOL this DM is with: header first, DM-list avatar for the id, incoming messages as fallback. */
  function pickPeer(header, linkUserId, messages) {
    const incoming = (messages || []).filter((m) => !m.out);
    const withId = incoming.find((m) => m.author_id);
    return {
      id: linkUserId || (withId ? withId.author_id : null),
      name: (header && header.name) || (incoming[0] ? incoming[0].author : "") || "",
      username: (header && header.username) || null,
    };
  }

  /**
   * Was the filled item sent? Yes when a new own message (newer than what was on screen at fill time)
   * shows its text, or when the input is empty again and the newest new own message is close to it
   * (the owner touched up the text before pressing Enter). Returns that message's id, or null.
   */
  function decideSent(filled, messages, editorEmpty) {
    if (!filled) return null;
    const base = filled.baseline || "0";
    const own = (messages || []).filter((m) => m.out && cmpId(m.id, base) > 0);
    const hit = own.find((m) => textsMatch(filled.text, m.text));
    if (hit) return hit.id;
    if (editorEmpty && own.length) {
      const last = own[own.length - 1];
      if (similarity(filled.text, last.text) >= 0.5) return last.id;
    }
    return null;
  }

  /**
   * Which item to fill on our own, if any: the first pending one, only into an empty input, only once
   * (if the owner clears it, it stays cleared), and never while the owner is typing somewhere else.
   */
  function decideAutoFill(s) {
    if (!s || s.busy || !s.focusOk || !s.listReady) return null; // listReady: the conversation is on screen
    const first = (s.items || [])[0];
    if (!first) return null;
    if (normForMatch(s.editorText)) return null;
    if (first.status === "filled" || (s.filledIds && s.filledIds.has(first.id))) return null;
    return first;
  }

  // ---- text out of DOM nodes (real nodes or plain look-alikes: nodeType, tagName, childNodes, getAttribute)

  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "TEMPLATE", "SVG", "BUTTON", "TEXTAREA", "INPUT"]);
  const BLOCK_TAGS = new Set(["DIV", "P", "LI", "UL", "OL", "PRE", "BLOCKQUOTE", "H1", "H2", "H3", "H4", "H5", "H6",
    "TABLE", "TR", "SECTION", "ARTICLE"]);

  function classOf(n) {
    const c = n && typeof n.getAttribute === "function" ? n.getAttribute("class") : null;
    return typeof c === "string" ? c : "";
  }

  function someDescendant(n, pred) {
    const kids = (n && n.childNodes) || [];
    for (let i = 0; i < kids.length; i++) {
      const k = kids[i];
      if (k.nodeType === 1 && (pred(k) || someDescendant(k, pred))) return true;
    }
    return false;
  }

  const isEdited = (n) => /(^|\s)edited_/.test(classOf(n));

  function skipElement(n, editor) {
    const c = classOf(n);
    if (c.includes("immersive-translate")) return true; // translation injected by 沉浸式翻译
    if (isEdited(n)) return true; // "(已编辑)"
    if (/(^|\s)timestamp_/.test(c) && someDescendant(n, isEdited)) return true; // its wrapper and hidden tooltip
    if (editor && n.getAttribute && n.getAttribute("data-slate-placeholder") != null) return true;
    return false;
  }

  /** Visible text of a node: emoji as their alt, <br> and block boundaries as line breaks. */
  function extractText(root, opts) {
    const editor = !!(opts && opts.editor);
    let s = "";
    const newline = () => { if (s && !s.endsWith("\n")) s += "\n"; };
    const walk = (n) => {
      if (!n) return;
      if (n.nodeType === 3) { s += n.nodeValue || ""; return; }
      if (n.nodeType !== 1) return;
      const tag = String(n.tagName || "").toUpperCase();
      if (SKIP_TAGS.has(tag) || skipElement(n, editor)) return;
      if (tag === "BR") { if (!editor) s += "\n"; return; } // Slate puts a <br> in every empty line
      if (tag === "IMG") { const alt = n.getAttribute && n.getAttribute("alt"); if (alt) s += alt; return; }
      const block = BLOCK_TAGS.has(tag);
      if (block) newline();
      const kids = n.childNodes || [];
      for (let i = 0; i < kids.length; i++) walk(kids[i]);
      if (block) newline();
    };
    const kids = (root && root.childNodes) || [];
    for (let i = 0; i < kids.length; i++) walk(kids[i]);
    return tidyText(s);
  }

  /** What is in Discord's Slate editor: one line per [data-slate-node="element"] child. */
  function editorTextOf(editor) {
    if (!editor) return "";
    const lines = [];
    const kids = editor.childNodes || [];
    for (let i = 0; i < kids.length; i++) {
      const k = kids[i];
      if (k.nodeType === 1 && k.getAttribute && k.getAttribute("data-slate-node") === "element") {
        lines.push(extractText(k, { editor: true }));
      }
    }
    return lines.length ? tidyText(lines.join("\n")) : extractText(editor, { editor: true });
  }

  const Core = {
    parseDmPath, userIdFromAvatar, cleanName, parseLabelledBy, tidyText, normForMatch, looseKey, textsMatch,
    similarity, cmpId, atFromSnowflake, collectAttachments, resolveAuthors, classifyOut, buildMessages, countOthers,
    pickPeer, decideSent, decideAutoFill, decideVanished, extractText, editorTextOf,
  };

  // =================================================================================== Dom

  const Dom = (function () {
    const inside = (root, el) => !!(root && el && root.contains(el));

    function findMessageList(doc) {
      return doc.querySelector('ol[data-list-id="chat-messages"]');
    }

    /** The owner's account, from the user panel at the bottom left (its label is localized: not used). */
    function readMe(doc) {
      const panel = doc.querySelector('section[class*="panels"]');
      if (!panel) return null;
      let id = null;
      for (const img of panel.querySelectorAll('img[src*="/avatars/"]')) {
        id = userIdFromAvatar(img.getAttribute("src")) || id; // the account area comes last
      }
      const title = panel.querySelector('[class*="panelTitleContainer"]');
      const tag = panel.querySelector('[class*="nameTag"]');
      const name = cleanName(title ? title.textContent : tag ? tag.innerText : "");
      return id || name ? { id, name } : null;
    }

    /** DM header: "PixelFox_\npixelfox_" (display name, username). */
    function readPeerHeader(doc) {
      const h1 = doc.querySelector('[class*="titleWrapper"] h1') || doc.querySelector('section[class*="title"] h1');
      if (!h1) return null;
      const lines = String(h1.innerText || h1.textContent || "").split("\n").map((l) => l.trim()).filter(Boolean);
      if (!lines.length) return null;
      return { name: cleanName(lines[0]), username: lines[1] ? lines[1].replace(ZERO_WIDTH, "") : null };
    }

    const dmLink = (doc, channelId) => doc.querySelector(`a[href="/channels/@me/${channelId}"]`);

    /** The peer's user id from this DM's avatar in the DM list (absent for group DMs or when scrolled away). */
    function readPeerLinkId(doc, channelId) {
      const link = dmLink(doc, channelId);
      const img = link && link.querySelector('img[src*="/avatars/"]');
      return img ? userIdFromAvatar(img.getAttribute("src")) : null;
    }

    /** This DM's label in the DM list ("PixelFox_ （私信）"); used to tell a stale header from the right one. */
    function readPeerLinkLabel(doc, channelId) {
      const link = dmLink(doc, channelId);
      return link ? String(link.getAttribute("aria-label") || link.textContent || "") : "";
    }

    /** A group DM with its own icon. Default group icons look like default avatars, so authors are counted too. */
    function isGroupMarker(doc, channelId) {
      const link = dmLink(doc, channelId);
      return !!(link && link.querySelector('img[src*="/channel-icons/"]'));
    }

    function readRow(li, channelId) {
      const m = /^chat-messages-(\d+)-(\d+)$/.exec(li.id || "");
      if (!m || m[1] !== channelId) return null;
      const id = m[2];
      const article = li.querySelector('[role="article"]');
      const lb = parseLabelledBy(article && article.getAttribute("aria-labelledby"));
      const reply = li.querySelector('[id^="message-reply-context"]');
      const accessories = li.querySelector(`[id="message-accessories-${id}"]`) || li.querySelector('[id^="message-accessories-"]');
      const own = (el) => !inside(reply, el) && !inside(accessories, el);

      // Header (name + avatar) only on a group's first row.
      let header = null;
      const userEl = Array.from(li.querySelectorAll('[id^="message-username-"]')).find(own);
      if (userEl) {
        const nameEl = userEl.querySelector('[class*="username_"]');
        const name = cleanName(nameEl ? nameEl.textContent : userEl.innerText || userEl.textContent);
        let uid = null;
        for (const img of li.querySelectorAll('img[class*="avatar"]')) {
          if (!own(img)) continue;
          uid = userIdFromAvatar(img.getAttribute("src")); // decorations have no id: keep looking
          if (uid) break;
        }
        header = { name, id: uid };
      }

      let timeEl = li.querySelector(`[id="message-timestamp-${id}"]`);
      if (timeEl && !timeEl.getAttribute("datetime")) timeEl = timeEl.querySelector("time[datetime]");
      const datetime = timeEl ? timeEl.getAttribute("datetime") : null;

      // The row's own content; a reply also contains the quoted message's content.
      let content = li.querySelector(`[id="message-content-${id}"]`);
      if (content && !own(content)) content = null;
      if (!content) content = Array.from(li.querySelectorAll('[id^="message-content-"]')).filter(own).pop() || null;

      let replyTo = null;
      if (reply) {
        const quoted = reply.querySelector('[id^="message-content-"]');
        replyTo = extractText(quoted || reply) || null;
      }

      const urls = [];
      if (accessories) {
        for (const a of accessories.querySelectorAll("a[href]")) urls.push(a.href);
        for (const el of accessories.querySelectorAll("img[src], video[src], source[src]")) urls.push(el.src);
      }

      // Discord shows a message before the server accepts it, under a temporary id.
      const pending = /isSending|isFailed/.test(classOf(content) + " " + classOf(article) + " " + classOf(li));

      return {
        id,
        leaderId: header ? id : lb.usernameId,
        header,
        at: datetime || null,
        text: content ? extractText(content) : "",
        replyTo,
        attachments: collectAttachments(urls),
        pending,
      };
    }

    /** Rendered rows of this channel, oldest first. */
    function readRows(doc, channelId) {
      const out = [];
      for (const li of doc.querySelectorAll(`li[id^="chat-messages-${channelId}-"]`)) {
        const r = readRow(li, channelId);
        if (r) out.push(r);
      }
      return out;
    }

    /**
     * The input box of this DM: inside the main chat area together with a message list that shows only this
     * channel's rows. Not the inline editor of a message being edited, not a dialog, not another channel's.
     */
    function findEditor(doc, channelId) {
      const all = Array.from(doc.querySelectorAll('[role="textbox"][data-slate-editor="true"]'))
        .filter((e) => !e.closest('li[id^="chat-messages-"]') && !e.closest('[role="dialog"]'));
      for (const e of all) {
        const main = e.closest("main");
        const list = main && main.querySelector('ol[data-list-id="chat-messages"]');
        if (!list) continue;
        const rows = Array.from(list.querySelectorAll('li[id^="chat-messages-"]'));
        if (rows.every((li) => li.id.startsWith(`chat-messages-${channelId}-`))) return e;
      }
      return null;
    }

    function readEditorText(editor) {
      return editorTextOf(editor);
    }

    function newestRowId(doc, channelId) {
      let max = "0";
      for (const li of doc.querySelectorAll(`li[id^="chat-messages-${channelId}-"]`)) {
        const m = /-(\d+)$/.exec(li.id);
        if (m && cmpId(m[1], max) > 0) max = m[1];
      }
      return max;
    }

    return { findMessageList, readMe, readPeerHeader, readPeerLinkId, readPeerLinkLabel, isGroupMarker, readRow, readRows,
      findEditor, readEditorText, newestRowId };
  })();

  // ---- filling the input (shared by App and the fixture page)

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const isBlank = (s) => !normForMatch(s);

  function selectAllIn(editor) {
    editor.focus();
    let ok = false;
    try { ok = document.execCommand("selectAll"); } catch (e) { ok = false; } // stays inside the editing host
    if (ok) return;
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(editor);
    sel.removeAllRanges();
    sel.addRange(range);
  }

  function caretToEnd(editor) {
    editor.focus();
    const sel = window.getSelection();
    if (sel && sel.rangeCount && editor.contains(sel.anchorNode)) return; // the owner's caret is already there
    const range = document.createRange();
    range.selectNodeContents(editor);
    range.collapse(false);
    sel.removeAllRanges();
    sel.addRange(range);
  }

  /** Paste the way Discord's editor expects it: a paste event carrying text/plain. */
  function pasteText(editor, text) {
    const dt = new DataTransfer();
    dt.setData("text/plain", text);
    editor.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
  }

  /** Thrown by insertIntoEditor when its guard says the page moved on (another DM, a server channel). */
  const MOVED = Object.freeze({ moved: true });

  /**
   * Put text into the editor and check it is there. Replaces existing content only when asked.
   * Synthetic paste first; execCommand("insertText") only for one-line text the paste did not touch at all
   * (for several lines insertText can act like Enter). Never keystroke by keystroke, never Enter.
   * guard() is asked before every step that changes the input; false throws MOVED.
   */
  async function insertIntoEditor(editor, text, replace, guard) {
    const check = () => { if (!editor.isConnected || (guard && !guard())) throw MOVED; };
    const want = normForMatch(text);
    check();
    // Slate copies the DOM selection into its own state on selectionchange, throttled: without a
    // selection it drops the paste. So set one, give it a moment, then paste.
    if (replace) selectAllIn(editor); else caretToEnd(editor);
    await sleep(120);
    check();
    const before = editorTextOf(editor);
    pasteText(editor, text);
    await sleep(150);
    const after = editorTextOf(editor);
    if (normForMatch(after) === want) return true;
    if (/\n/.test(text) || after !== before) return false; // leave it to the 复制 button
    check();
    selectAllIn(editor);
    await sleep(120);
    check();
    try { document.execCommand("insertText", false, text); } catch (e) { /* checked below */ }
    await sleep(150);
    return normForMatch(editorTextOf(editor)) === want;
  }

  // ---- environments without the extension: node tests and the fixture page

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { Core };
    return;
  }

  let extensionContext = false;
  try { extensionContext = !!(typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.id); } catch (e) { /* page */ }
  if (!extensionContext) {
    // Plain page (test/fixture.html): expose the pieces instead of starting.
    window.KolMailDiscord = { Core, Dom, insertIntoEditor, MOVED };
    return;
  }

  // =================================================================================== App

  const TICK_MS = 1000;
  const SYNC_DEBOUNCE_MS = 1500;
  const SYNC_EVERY_MS = 30000;
  const QUEUE_EVERY_MS = 3000;
  const AFTER_SENT_MS = 800;
  const SENT_CHECK_MS = 250;
  const MAX_MESSAGES = 50;
  const KEEP_IDS = 300; // cap on remembered item ids per conversation

  const S = {
    dead: false,
    ch: null,             // channel id of the open DM, null elsewhere
    loaded: false,        // per-conversation memory restored from the background
    conn: null,           // { ok, error } of the last KOL Mail call
    conv: null,           // conv from the last sync
    items: [],            // pending queue items, in order
    filled: null,         // { itemId, text, n, of, baseline }: in the input, waiting for the owner's Enter
    filledIds: new Set(), // filled once already: never auto-filled again
    finalIds: new Set(),  // marked sent or dropped here: hidden even if a stale queue still lists them
    notice: null,         // { text, copyText?, warn? }
    group: false,         // a group DM: neither synced nor filled
    collapsed: false,
    lastSyncKey: "",
    lastSyncAt: 0,
    lastQueueAt: 0,
    wasVisible: false,
    syncing: false,
    syncAgain: false,
    queueing: false,
    busy: false,          // filling
    markingSent: false,
    timers: { tick: 0, sync: 0, sent: 0 },
    observer: null,
    observed: null,
  };

  function alive() {
    try { return !S.dead && !!(chrome.runtime && chrome.runtime.id); } catch (e) { return false; }
  }

  /** The page's address is still this DM. Discord is a SPA: it can change under any await. */
  const onChannel = (ch) => !!ch && parseDmPath(location.pathname) === ch;

  /** onChannel, and if the page moved on, switch to where it is now. Callers abort on false. */
  function stillHere(ch) {
    if (!S.dead && S.ch === ch && onChannel(ch)) return true;
    const here = parseDmPath(location.pathname);
    if (!S.dead && here !== S.ch) enterChannel(here);
    return false;
  }

  /** One request to the background. Resolves { ok, data } or { ok: false, error }; never throws. */
  function ask(msg) {
    return new Promise((resolve) => {
      if (!alive()) { teardown(); resolve({ ok: false, error: "", code: "dead" }); return; }
      try {
        chrome.runtime.sendMessage(msg, (res) => {
          if (chrome.runtime.lastError || !res) {
            if (!alive()) teardown();
            resolve({ ok: false, error: "扩展后台没响应，刷新页面再试", code: "ext" });
            return;
          }
          resolve(res);
        });
      } catch (e) {
        teardown(); // "Extension context invalidated": the extension was reloaded or updated
        resolve({ ok: false, error: "", code: "dead" });
      }
    });
  }

  function setConn(ok, error) {
    S.conn = { ok: !!ok, error: ok ? "" : String(error || "KOL Mail 出错了") };
  }

  // ---- per-conversation memory (kept by the background in chrome.storage.session, survives a reload)

  function persist() {
    if (!S.ch || !S.loaded) return;
    const last = (set) => Array.from(set).slice(-KEEP_IDS);
    ask({ type: "state_set", channel_id: S.ch, data: { filled: S.filled, filledIds: last(S.filledIds), finalIds: last(S.finalIds) } });
  }

  function restore(d) {
    if (d.filled && typeof d.filled.itemId === "string" && typeof d.filled.text === "string") S.filled = d.filled;
    if (Array.isArray(d.filledIds)) S.filledIds = new Set(d.filledIds.map(String));
    if (Array.isArray(d.finalIds)) S.finalIds = new Set(d.finalIds.map(String));
  }

  // ---- conversation lifecycle

  async function enterChannel(ch) {
    S.ch = ch;
    S.loaded = false;
    S.conv = null;
    S.items = [];
    S.filled = null;
    S.filledIds = new Set();
    S.finalIds = new Set();
    S.notice = null;
    S.group = false;
    S.lastSyncKey = "";
    S.lastSyncAt = 0;
    S.lastQueueAt = 0;
    S.wasVisible = document.visibilityState === "visible";
    clearTimeout(S.timers.sync);
    attachObserver();
    render();
    if (!ch) return;
    const r = await ask({ type: "state_get", channel_id: ch });
    if (!stillHere(ch)) return;
    if (r.ok && r.data) restore(r.data);
    S.loaded = true;
    S.lastSyncAt = Date.now(); // the first sync is the scheduled one below
    scheduleSync(800);
    pollQueue();
  }

  function attachObserver() {
    if (S.observer) S.observer.disconnect();
    S.observed = null;
    if (!S.ch) return;
    const list = Dom.findMessageList(document);
    if (!list) return;
    if (!S.observer) S.observer = new MutationObserver(onMutations);
    S.observer.observe(list, { childList: true, subtree: true, characterData: true });
    S.observed = list;
  }

  function onMutations() {
    scheduleSync(SYNC_DEBOUNCE_MS);
    if (S.filled && !S.timers.sent) {
      S.timers.sent = setTimeout(() => { S.timers.sent = 0; checkSent(); }, SENT_CHECK_MS);
    }
  }

  function scheduleSync(ms) {
    clearTimeout(S.timers.sync);
    S.timers.sync = setTimeout(() => doSync(false), ms);
  }

  function tick() {
    if (!alive()) { teardown(); return; }
    const ch = parseDmPath(location.pathname);
    if (ch !== S.ch) { enterChannel(ch); return; }
    updateTheme();
    if (!ch) return;
    if (!S.observed || !S.observed.isConnected) attachObserver(); // Discord re-rendered the list
    const visible = document.visibilityState === "visible";
    if (visible && !S.wasVisible) { S.lastSyncAt = 0; S.lastQueueAt = 0; } // back on the tab: refresh now
    S.wasVisible = visible;
    if (!visible || !S.loaded) return;
    const now = Date.now();
    if (now - S.lastSyncAt >= SYNC_EVERY_MS) doSync(true);
    if (now - S.lastQueueAt >= QUEUE_EVERY_MS) pollQueue();
  }

  function teardown() {
    if (S.dead) return;
    S.dead = true;
    clearInterval(S.timers.tick);
    clearTimeout(S.timers.sync);
    clearTimeout(S.timers.sent);
    if (S.observer) S.observer.disconnect();
    try { render(); } catch (e) { /* page is going away */ }
  }

  // ---- sync

  /** The other side as this page shows it: header name and DM-list avatar id. Null while the header is stale. */
  function currentPeer(ch) {
    const header = Dom.readPeerHeader(document);
    const label = Dom.readPeerLinkLabel(document, ch);
    // Right after a switch the header can still show the previous DM: its name is then not in this DM's label.
    if (header && label && !label.includes(header.name)) return null;
    return { header, id: Dom.readPeerLinkId(document, ch), name: header ? header.name : "" };
  }

  /** Group DMs are left alone: more than one other author, or a group icon in the DM list. */
  function isGroup(ch) {
    if (S.group) return true;
    const me = Dom.readMe(document);
    if (Dom.isGroupMarker(document, ch) || (me && countOthers(Dom.readRows(document, ch), me) > 1)) {
      S.group = true;
      S.items = [];
      S.notice = null;
      render();
    }
    return S.group;
  }

  function buildSyncPayload(ch) {
    if (!onChannel(ch) || !Dom.findMessageList(document)) return null; // not this DM, or not rendered yet
    const me = Dom.readMe(document);
    if (!me) return null; // without it every message would look incoming
    if (isGroup(ch)) return null;
    const p = currentPeer(ch);
    if (!p) return null;
    const messages = buildMessages(Dom.readRows(document, ch), me, MAX_MESSAGES, p);
    const peer = pickPeer(p.header, p.id, messages);
    if (!peer.name && !peer.id) return null;
    return { channel_id: ch, peer, me: { id: me.id || null, name: me.name || "" }, messages };
  }

  async function doSync(force) {
    const ch = S.ch;
    if (!ch || S.dead || !S.loaded || !stillHere(ch)) return;
    if (S.syncing) { S.syncAgain = true; return; }
    if (force) S.lastSyncAt = Date.now();
    const payload = buildSyncPayload(ch);
    if (!payload) { render(); return; }
    const key = JSON.stringify(payload);
    if (!force && key === S.lastSyncKey) return; // e.g. only 沉浸式翻译 touched the list
    S.syncing = true;
    S.lastSyncAt = Date.now();
    const r = await ask({ type: "kol", op: "sync", payload });
    S.syncing = false;
    // A sync asked for while this one was in flight (maybe for the DM the owner just switched to).
    if (S.syncAgain) { S.syncAgain = false; scheduleSync(SYNC_DEBOUNCE_MS); }
    if (!stillHere(ch) || S.group) return;
    if (r.ok) {
      const d = r.data || {};
      S.lastSyncKey = key;
      setConn(true);
      if (d.conv && typeof d.conv === "object") S.conv = d.conv;
      if (Array.isArray(d.queue)) { setItems(d.queue); S.lastQueueAt = Date.now(); }
    } else if (r.code !== "dead") {
      setConn(false, r.error);
    }
    render();
    maybeAutoFill();
  }

  // ---- queue

  /** The queue as KOL Mail just listed it. */
  function setItems(list) {
    const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : 0);
    const all = (Array.isArray(list) ? list : [])
      .filter((it) => it && (typeof it.id === "string" || typeof it.id === "number") && typeof it.text === "string" && it.text.trim())
      .map((it) => ({
        id: String(it.id),
        n: num(it.n),
        of: num(it.of),
        text: it.text.replace(/\r\n?/g, "\n"),
        zh: typeof it.zh === "string" ? it.zh : "",
        status: typeof it.status === "string" ? it.status : "",
      }));
    S.items = all.filter((it) => !S.finalIds.has(it.id));
    if (decideVanished(S.filled, new Set(all.map((it) => it.id)), S.finalIds)) filledVanished();
  }

  /**
   * The item in the input is gone from KOL Mail's queue. KOL Mail drops a draft once the KOL wrote after it,
   * and also marks one sent when it sees our own message with its words: tell the two apart.
   */
  function filledVanished() {
    const f = S.filled;
    const ch = S.ch;
    const me = Dom.readMe(document);
    const p = currentPeer(ch);
    if (!me || !p || !onChannel(ch)) return; // page mid-switch: decide on the next poll
    const messages = buildMessages(Dom.readRows(document, ch), me, 1000, p);
    const editor = Dom.findEditor(document, ch);
    const text = editor ? Dom.readEditorText(editor) : "";
    if (decideSent(f, messages, isBlank(text))) { markSent(f); return; }
    S.filled = null;
    S.finalIds.add(f.itemId);
    S.notice = isBlank(text) ? null : { text: "对方有新消息，这条草稿已作废，输入框里的字先别发", warn: true };
    persist();
  }

  async function pollQueue() {
    const ch = S.ch;
    if (!ch || S.dead || !S.loaded || S.queueing || !stillHere(ch) || isGroup(ch)) return;
    S.queueing = true;
    S.lastQueueAt = Date.now();
    const r = await ask({ type: "kol", op: "queue", payload: { channel_id: ch } });
    S.queueing = false;
    if (!stillHere(ch) || S.group) return;
    if (r.ok) {
      setConn(true);
      setItems(r.data && r.data.items);
    } else if (r.code !== "dead") {
      setConn(false, r.error);
    }
    render();
    checkSent();
    maybeAutoFill();
  }

  /** Status change for KOL Mail. The background retries it until KOL Mail has it. */
  async function sendUpdate(ch, itemId, status) {
    const r = await ask({ type: "update", channel_id: ch, item_id: itemId, status });
    if (S.ch === ch && r.code !== "dead") {
      if (r.ok) setConn(true); else setConn(false, r.error);
      render();
    }
    return r;
  }

  // ---- filling and sent detection

  function focusAllowsAutofill(editor) {
    const a = document.activeElement;
    if (!a || a === document.body || a === document.documentElement || a === ui.host) return true;
    if (editor.contains(a)) return true;
    // The owner is typing elsewhere (search, another box): do not pull the caret away.
    return !(a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT" || a.isContentEditable);
  }

  async function maybeAutoFill() {
    const ch = S.ch;
    if (S.dead || !ch || !S.loaded || S.busy || S.markingSent || document.visibilityState !== "visible") return;
    if (!stillHere(ch) || isGroup(ch)) return;
    const editor = Dom.findEditor(document, ch);
    if (!editor) return;
    const item = decideAutoFill({
      items: S.items,
      editorText: Dom.readEditorText(editor),
      filledIds: S.filledIds,
      // Only the tab the owner is looking at: with two tabs on one DM the claim below picks one anyway.
      focusOk: document.hasFocus() && focusAllowsAutofill(editor),
      busy: S.busy,
      listReady: !!Dom.findMessageList(document) && Dom.newestRowId(document, ch) !== "0",
    });
    if (item) await fillItem(item, false);
  }

  async function fillItem(item, manual) {
    const ch = S.ch;
    if (S.busy || !ch || !stillHere(ch) || isGroup(ch)) return;
    S.busy = true;
    try {
      let editor = Dom.findEditor(document, ch);
      if (!manual) {
        if (!editor || !isBlank(Dom.readEditorText(editor))) return; // auto-fill never overwrites the owner's typing
        // Claim first: KOL Mail says yes to one caller only, so two tabs never both paste it.
        const c = await ask({ type: "claim", channel_id: ch, item_id: item.id });
        if (!stillHere(ch)) return;
        if (!c.ok) {
          if (c.code !== "dead") { setConn(false, c.error); render(); }
          return; // KOL Mail unreachable: try again on the next poll
        }
        setConn(true);
        S.filledIds.add(item.id); // claimed or refused: never auto-filled again either way (填入 still works)
        persist();
        if (!(c.data && c.data.changed === true)) { render(); return; } // another tab has it, or it is final
        editor = Dom.findEditor(document, ch);
        if (!editor || !isBlank(Dom.readEditorText(editor))) { render(); return; } // the owner started typing
      }
      const before = editor ? Dom.readEditorText(editor) : "";
      const baseline = Dom.newestRowId(document, ch);
      let ok = false;
      if (editor) {
        try {
          ok = await insertIntoEditor(editor, item.text, !isBlank(before), () => stillHere(ch) && Dom.findEditor(document, ch) === editor);
        } catch (e) {
          if (e === MOVED) return; // the page moved to another conversation: paste nothing anywhere
          throw e;
        }
      }
      if (!stillHere(ch)) return;
      // Remember it either way: sent detection still works after a manual paste, and no retry loop.
      S.filled = { itemId: item.id, text: item.text, n: item.n, of: item.of, baseline };
      S.filledIds.add(item.id);
      persist();
      if (ok) {
        S.notice = { text: `已填入第 ${item.n || 1} 条，检查后按回车发送` };
        render();
        if (manual) sendUpdate(ch, item.id, "filled"); // auto-fill already claimed it
      } else {
        S.notice = { text: "自动填入失败，点复制后在输入框里粘贴", copyText: item.text };
        render();
      }
    } finally {
      S.busy = false;
    }
  }

  function checkSent() {
    const f = S.filled;
    if (!f || !S.ch || S.dead || !S.loaded || S.busy || S.markingSent) return;
    const me = Dom.readMe(document);
    const p = currentPeer(S.ch);
    if (!me || !p || !onChannel(S.ch)) return;
    const messages = buildMessages(Dom.readRows(document, S.ch), me, 1000, p);
    const editor = Dom.findEditor(document, S.ch);
    const empty = editor ? isBlank(Dom.readEditorText(editor)) : false;
    if (decideSent(f, messages, empty)) markSent(f);
  }

  async function markSent(f) {
    const ch = S.ch;
    S.markingSent = true;
    S.filled = null;
    S.finalIds.add(f.itemId);
    S.items = S.items.filter((i) => i.id !== f.itemId);
    S.notice = null;
    persist();
    render();
    await sendUpdate(ch, f.itemId, "sent");
    await sleep(AFTER_SENT_MS);
    S.markingSent = false;
    if (!stillHere(ch)) return;
    if (S.queueing) maybeAutoFill(); else pollQueue(); // pollQueue fills the next one
  }

  async function skipItem(item) {
    const ch = S.ch;
    if (!stillHere(ch)) return; // the panel was still showing the previous DM
    S.finalIds.add(item.id);
    S.items = S.items.filter((i) => i.id !== item.id);
    if (S.filled && S.filled.itemId === item.id) { S.filled = null; S.notice = null; }
    persist();
    render();
    await sendUpdate(ch, item.id, "dropped");
    if (S.ch === ch) pollQueue();
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) {
      // Fallback inside our shadow root: select a textarea and copy.
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.cssText = "position:fixed;opacity:0;left:0;top:0;";
      ui.root.appendChild(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (e2) { ok = false; }
      ta.remove();
      return ok;
    }
  }

  // ---- panel (closed shadow root: Discord's CSS stays out, Discord's scripts cannot read it)

  const CSS = `
    :host { all: initial; }
    * { box-sizing: border-box; }
    .wrap { --bg:#1e1f22; --bg2:#2b2d31; --line:#3f4147; --fg:#dbdee1; --muted:#949ba4; --accent:#5865f2;
            --accent-fg:#fff; --ok:#23a55a; --err:#f23f43; --warn:#f0b232;
            font: 13px/1.45 "Microsoft YaHei", "PingFang SC", system-ui, sans-serif; color: var(--fg); }
    .wrap.light { --bg:#ffffff; --bg2:#f2f3f5; --line:#d4d7dc; --fg:#313338; --muted:#5c5e66; }
    .panel { width: 290px; max-height: 62vh; display: flex; flex-direction: column; background: var(--bg);
             border: 1px solid var(--line); border-radius: 10px; box-shadow: 0 6px 24px rgba(0,0,0,.35); overflow: hidden; }
    .head { display: flex; align-items: center; gap: 6px; padding: 7px 8px 7px 10px; border-bottom: 1px solid var(--line); }
    .title { font-weight: 600; flex: 1; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); flex: none; }
    .dot.ok { background: var(--ok); } .dot.err { background: var(--err); }
    .body { padding: 8px 10px 10px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }
    .muted { color: var(--muted); }
    .err-text { color: var(--err); }
    .row { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
    .row.gap { margin-top: 5px; }
    button { font: inherit; color: var(--fg); background: var(--bg2); border: 1px solid var(--line); border-radius: 6px;
             padding: 3px 9px; cursor: pointer; }
    button:hover { border-color: var(--muted); }
    button.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-fg); }
    button.link { background: none; border: none; padding: 0 2px; color: var(--muted); }
    .notice { background: var(--bg2); border-left: 3px solid var(--warn); border-radius: 4px; padding: 6px 8px; }
    .notice.ok { border-left-color: var(--ok); }
    .notice.warn { border-left-color: var(--err); color: var(--err); font-weight: 600; }
    .item { border: 1px solid var(--line); border-radius: 8px; padding: 7px 8px; display: flex; flex-direction: column; gap: 5px; }
    .item.filled { border-color: var(--accent); }
    .item-head { display: flex; justify-content: space-between; color: var(--muted); font-size: 12px; }
    .text { white-space: pre-wrap; word-break: break-word; max-height: 160px; overflow-y: auto; }
    .zh-label { color: var(--muted); font-size: 12px; }
    .zh { white-space: pre-wrap; word-break: break-word; color: var(--muted); max-height: 100px; overflow-y: auto; }
    .pill { display: inline-flex; align-items: center; gap: 6px; padding: 5px 11px; background: var(--bg);
            border: 1px solid var(--line); border-radius: 999px; box-shadow: 0 4px 16px rgba(0,0,0,.3); cursor: pointer; user-select: none; }
    .badge { background: var(--accent); color: var(--accent-fg); border-radius: 999px; padding: 0 7px; font-size: 12px; }
  `;

  const ui = (function () {
    document.getElementById("kolmail-dc")?.remove(); // a leftover from a previous instance
    const host = document.createElement("div");
    host.id = "kolmail-dc";
    host.style.cssText = "all:initial;position:fixed;right:16px;bottom:88px;z-index:1000;display:none;";
    const root = host.attachShadow({ mode: "closed" });
    try {
      const sheet = new CSSStyleSheet();
      sheet.replaceSync(CSS);
      root.adoptedStyleSheets = [sheet];
    } catch (e) {
      const style = document.createElement("style");
      style.textContent = CSS;
      root.appendChild(style);
    }
    const wrap = document.createElement("div");
    wrap.className = "wrap";
    root.appendChild(wrap);
    // Keys typed while a panel button has focus must not reach Discord's global shortcuts.
    for (const t of ["keydown", "keyup", "keypress"]) root.addEventListener(t, (e) => e.stopPropagation());
    document.documentElement.appendChild(host);
    return { host, root, wrap, lastKey: "" };
  })();

  /** Tiny element builder. Text goes in as textContent only: page and KOL Mail text are data. */
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k === "onclick") {
        el.addEventListener("mousedown", (e) => e.preventDefault()); // keep the caret where it was
        el.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); v(e); });
      } else el.setAttribute(k, v);
    }
    for (const kid of kids) if (kid) el.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
    return el;
  }

  function updateTheme() {
    const light = document.documentElement.classList.contains("theme-light");
    if (ui.wrap.classList.contains("light") !== light) ui.wrap.classList.toggle("light", light);
  }

  function openKolMail() {
    const ch = S.ch;
    if (!ch || !stillHere(ch)) return;
    ask({ type: "open", channel_id: ch }).then((r) => {
      if (!r.ok && r.code !== "dead" && S.ch === ch) { setConn(false, r.error); render(); }
    });
  }

  function toggleCollapsed() {
    S.collapsed = !S.collapsed;
    ask({ type: "ui_set", collapsed: S.collapsed }); // storage.local is the background's: it holds the token
    render();
  }

  function render() {
    const onDm = !!parseDmPath(location.pathname);
    const view = {
      show: S.dead ? onDm : !!S.ch,
      dead: S.dead,
      collapsed: S.collapsed,
      conn: S.conn,
      conv: S.conv,
      items: S.items.map((i) => [i.id, i.n, i.of, i.text, i.zh]),
      filled: S.filled ? S.filled.itemId : null,
      notice: S.notice,
      group: S.group,
    };
    const key = JSON.stringify(view);
    if (key === ui.lastKey) return; // unchanged: keep the DOM (and hover, scroll) as it is
    ui.lastKey = key;
    ui.host.style.display = view.show ? "block" : "none";
    ui.wrap.textContent = "";
    if (!view.show) return;

    if (S.dead) {
      ui.wrap.appendChild(h("div", { class: "pill" }, h("span", { class: "dot err" }), "KOL Mail 扩展已更新，刷新页面后继续用"));
      return;
    }

    const dotClass = "dot" + (S.conn ? (S.conn.ok ? " ok" : " err") : "");

    if (S.collapsed) {
      const pill = h("div", { class: "pill", title: "展开" }, h("span", { class: dotClass }), "KOL Mail");
      if (S.items.length) pill.appendChild(h("span", { class: "badge", text: `${S.items.length} 条待发` }));
      pill.addEventListener("mousedown", (e) => e.preventDefault());
      pill.addEventListener("click", toggleCollapsed);
      ui.wrap.appendChild(pill);
      return;
    }

    const body = h("div", { class: "body" });

    // connection
    if (!S.conn) body.appendChild(h("div", { class: "muted", text: "正在连接 KOL Mail…" }));
    else if (S.conn.ok) body.appendChild(h("div", { class: "muted", text: "已连接 KOL Mail" }));
    else body.appendChild(h("div", { class: "err-text", text: S.conn.error }));

    if (S.group) {
      body.appendChild(h("div", { class: "notice", text: "这是群聊私信，扩展在这里不同步，也不填入" }));
      ui.wrap.appendChild(h("div", { class: "panel" },
        h("div", { class: "head" },
          h("span", { class: dotClass }),
          h("span", { class: "title", text: "KOL Mail" }),
          h("button", { class: "link", text: "收起", onclick: toggleCollapsed })),
        body));
      return;
    }

    // link to the master sheet
    const conv = S.conv;
    if (conv) {
      const linked = conv.linked && typeof conv.linked === "object" ? conv.linked : null;
      if (linked) {
        body.appendChild(h("div", { text: `已关联总表 ${linked.id || ""} ${linked.kol || ""}`.trim() }));
      } else {
        const box = h("div", {}, h("div", { text: "还没关联总表" }));
        const sug = (Array.isArray(conv.suggest) ? conv.suggest : []).filter((s) => s && (s.kol || s.id)).slice(0, 3);
        if (sug.length) {
          const names = sug.map((s) => (s.id ? `${s.kol || ""}（${s.id}）` : s.kol)).join("、");
          box.appendChild(h("div", { class: "muted", text: `可能是 ${names}` }));
        }
        box.appendChild(h("div", { class: "row gap" }, h("button", { text: "去 KOL Mail 关联", onclick: openKolMail })));
        body.appendChild(box);
      }
    }

    body.appendChild(h("div", { class: "row" }, h("button", { class: "primary", text: "在 KOL Mail 里起草", onclick: openKolMail })));

    // notice (filled / copy fallback)
    if (S.notice) {
      const kind = S.notice.warn ? " warn" : S.notice.copyText ? "" : " ok";
      const n = h("div", { class: "notice" + kind }, h("div", { text: S.notice.text }));
      if (S.notice.copyText) {
        const copy = S.notice.copyText;
        n.appendChild(h("div", { class: "row gap" }, h("button", {
          class: "primary",
          text: "复制",
          onclick: async () => {
            const ok = await copyText(copy);
            S.notice = ok
              ? { text: "已复制，到输入框里粘贴，检查后按回车发送" }
              : { text: "复制也失败了，请在下面的队列里手动选中文字复制", copyText: copy };
            ui.lastKey = "";
            render();
          },
        })));
      }
      body.appendChild(n);
    }

    // queue
    for (const it of S.items) {
      const filled = !!(S.filled && S.filled.itemId === it.id);
      const label = it.n && it.of ? `第 ${it.n}/${it.of} 条` : "待发";
      const box = h("div", { class: "item" + (filled ? " filled" : "") },
        h("div", { class: "item-head" }, h("span", { text: label }), filled ? h("span", { text: "已填入" }) : null),
        h("div", { class: "text", text: it.text }));
      if (it.zh) {
        box.appendChild(h("div", { class: "zh-label", text: "中文大意" }));
        box.appendChild(h("div", { class: "zh", text: it.zh }));
      }
      box.appendChild(h("div", { class: "row" },
        h("button", { class: "primary", text: "填入", onclick: () => fillItem(it, true) }),
        h("button", { text: "跳过", onclick: () => skipItem(it) })));
      body.appendChild(box);
    }

    const panel = h("div", { class: "panel" },
      h("div", { class: "head" },
        h("span", { class: dotClass }),
        h("span", { class: "title", text: "KOL Mail" }),
        h("button", { class: "link", text: "收起", onclick: toggleCollapsed })),
      body);
    ui.wrap.appendChild(panel);
  }

  // ---- start

  ask({ type: "ui_get" }).then((r) => {
    if (!r.ok || !r.data) return;
    S.collapsed = !!r.data.collapsed;
    render();
  });
  updateTheme();
  S.timers.tick = setInterval(tick, TICK_MS);
  tick();
})();
