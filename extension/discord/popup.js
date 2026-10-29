/* Settings popup: the token and port for KOL Mail, and a connection test through the background. */
"use strict";

const $ = (id) => document.getElementById(id);
const DEFAULT_PORT = 8765;

function show(text, kind) {
  const el = $("status");
  el.textContent = text;
  el.className = kind || "";
}

function test() {
  show("正在测试…");
  chrome.runtime.sendMessage({ type: "kol", op: "hello", payload: {} }, (res) => {
    if (chrome.runtime.lastError || !res) { show("扩展后台没响应，到 chrome://extensions 重新加载扩展", "err"); return; }
    if (res.ok) {
      const v = res.data && typeof res.data.version === "string" ? `（版本 ${res.data.version}）` : "";
      show(`已连接 KOL Mail${v}`, "ok");
    } else {
      show(res.error || "连接失败", "err");
    }
  });
}

async function save() {
  const token = $("token").value.trim();
  const raw = $("port").value.trim();
  const port = raw ? Number(raw) : DEFAULT_PORT;
  if (!Number.isInteger(port) || port < 1 || port > 65535) { show("端口要是 1 到 65535 之间的数字", "err"); return; }
  if (!token) { show("先粘贴令牌", "err"); return; }
  $("save").disabled = true;
  try {
    await chrome.storage.local.set({ token, port });
    $("port").value = String(port);
    test();
  } finally {
    $("save").disabled = false;
  }
}

async function init() {
  const s = await chrome.storage.local.get(["token", "port"]);
  $("token").value = typeof s.token === "string" ? s.token : "";
  $("port").value = String(s.port || DEFAULT_PORT);
  $("save").addEventListener("click", save);
  for (const id of ["token", "port"]) {
    $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
  }
  if ($("token").value) test(); // show the current state right away
}

init();
