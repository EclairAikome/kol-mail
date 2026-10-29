/* Telegram in the settings (tgsetup.py): install the Telegram component, then log the colleague's own Telegram
   account in, without a command line. Renders into <div id="tgSetup"></div>; the page calls window.tgSetup.render()
   whenever the tab opens. Names come from Telegram, so everything shown goes in as text, never as HTML. */
(() => {
  "use strict";

  const CSS = `
#tgSetup .tgstep { border-top: 1px solid var(--line); margin-top: 10px; padding-top: 4px; }
#tgSetup .tgstep h4 { margin: 6px 0 4px; font-size: 14px; }
#tgSetup .tgstep.off h4, #tgSetup .tgstep.off ol { color: var(--muted); }
#tgSetup input[type=text], #tgSetup input[type=tel], #tgSetup input[type=password] { font: inherit; color: var(--text); background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 5px 8px; min-width: 0; }
#tgSetup input:focus { outline: 2px solid var(--accent); outline-offset: -1px; border-color: var(--accent); }
#tgSetup input:disabled { opacity: .6; }
#tgSetup .grow { flex: 1; min-width: 180px; }
#tgSetup ol { margin: 4px 0 6px; padding-left: 22px; font-size: 13px; }
#tgSetup .ok { color: var(--ok); font-weight: 600; }
#tgSetup .line { font-family: ui-monospace, Consolas, monospace; font-size: 12px; overflow-wrap: anywhere; }
#tgSetup .who { font-weight: 600; overflow-wrap: anywhere; }
#tgSetup [hidden] { display: none !important; }
`;

  // The page's own helper (index.html) when it is there: same guard headers, same handling of a logged-out page.
  async function call(path, body) {
    if (typeof api === "function") {
      return body === undefined ? api(path, { cache: "no-store" })
        : api(path, { method: "POST", body: JSON.stringify(body) });
    }
    const init = { cache: "no-store", headers: { "X-KOL-Mail": "1" } };
    if (body !== undefined) Object.assign(init, { method: "POST", body: JSON.stringify(body),
      headers: { "X-KOL-Mail": "1", "Content-Type": "application/json" } });
    const r = await fetch(path, init);
    const data = await r.json().catch(() => ({ error: "响应无法解析" }));
    if (!r.ok) { const err = new Error(data.error || r.statusText); err.data = data; err.status = r.status; throw err; }
    return data;
  }

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === "text") node.textContent = v;
      else if (k in node) node[k] = v;
      else node.setAttribute(k, v);
    }
    for (const c of children) if (c != null) node.append(c);
    return node;
  }

  let gen = 0;          // each render() starts a new generation; older polls and answers are dropped
  let timer = null;
  let flash = null;     // {text, warn}: what a logout said, shown once after the redraw it causes
  const typed = { apiId: "", apiHash: "", phone: "" };  // kept while the page is open, so a redraw keeps them

  const visible = root => root.isConnected && root.offsetParent !== null;
  const errText = e => (e && e.message) || String(e);

  async function render() {
    const root = document.getElementById("tgSetup");
    if (!root) return;
    if (!document.getElementById("tgSetupStyle")) document.head.append(el("style", { id: "tgSetupStyle", text: CSS }));
    const my = ++gen;
    clearTimeout(timer);
    let s;
    try { s = await call("/api/telegram/status"); }
    catch (e) {
      if (my === gen) root.replaceChildren(el("p", { className: "note err", text: "读不到 Telegram 的设置，" + errText(e) }));
      return;
    }
    if (my !== gen) return;
    if (s.logged_in) drawAccount(root, s, my, 0);
    else drawSteps(root, s, my);
  }

  const intro = () => el("p", { className: "note",
    text: "登录的是你自己的 Telegram 工作号，KOL Mail 用它读私信、按手册起草、你确认后才发；会出现在这个账号的“设备”列表里，名字是 KOL Mail。" });

  // ------------------------------------------------ logged in

  function drawAccount(root, s, my, tries) {
    const me = s.me;
    const who = el("span", { className: "who",
      text: me ? "已登录：" + (me.name || "（没有名字）") + (me.username ? ` (@${me.username})` : "") : "已登录" });
    const note = el("span", { className: "note", text: me ? "" : "名字过一会儿显示" });
    const out = el("button", { className: "act", type: "button", text: "退出登录" });
    out.onclick = async () => {
      if (!confirm("退出以后 KOL Mail 就读不到这个 Telegram 号的私信了，确定退出吗？")) return;
      out.disabled = true; out.textContent = "正在退出…";
      try {
        const r = await call("/api/telegram/logout", {});
        flash = r.note ? { text: r.note, warn: true } : { text: "已经退出，这个号的“设备”列表里也没有 KOL Mail 了。", warn: false };
        render();
      } catch (e) {
        out.disabled = false; out.textContent = "退出登录";
        note.className = "note err"; note.textContent = "退出失败，" + errText(e);
      }
    };
    root.replaceChildren(intro(), el("div", { className: "row2" }, who, out), note);
    if (!me && tries < 5) {  // the server looks the name up in the background; ask again a few times
      timer = setTimeout(async () => {
        if (my !== gen || !visible(root)) return;
        try {
          const again = await call("/api/telegram/status");
          if (my !== gen) return;
          if (again.logged_in) drawAccount(root, again, my, tries + 1); else render();
        } catch { /* the name is only a nicety */ }
      }, 4000);
    }
  }

  // ------------------------------------------------ not logged in: the three steps

  function drawSteps(root, s, my) {
    // 1. the component
    const compState = el("span");
    const compBtn = el("button", { className: "act primary", type: "button", text: "安装" });
    const compLine = el("div", { className: "note line" });
    const compErr = el("p", { className: "note err" });
    const compNote = el("p", { className: "note",
      text: "要先下载 Telegram 组件，再装它用到的依赖，第一次要几分钟。装的同时可以先去做第 2 步。" });
    const step1 = el("section", { className: "tgstep" }, el("h4", { text: "1. 安装 Telegram 组件" }), compNote,
      el("div", { className: "row2" }, compState, compBtn), compLine, compErr);

    // 2. API ID and hash
    const apiId = el("input", { type: "text", className: "grow", inputMode: "numeric", autocomplete: "off",
      spellcheck: false, maxLength: 12, placeholder: "API ID（一串数字）", value: typed.apiId });
    const apiHash = el("input", { type: "text", className: "grow", autocomplete: "off", spellcheck: false,
      maxLength: 32, placeholder: "API Hash（32 位字母和数字）", value: typed.apiHash });
    apiId.oninput = () => { typed.apiId = apiId.value; };
    apiHash.oninput = () => { typed.apiHash = apiHash.value; };
    const saved = el("p", { className: "note", text: "这台电脑上已经存过一对，两格都留空就用存着的。", hidden: !s.api_ready });
    if (s.api_ready) { apiId.placeholder = "API ID（留空用存着的）"; apiHash.placeholder = "API Hash（留空用存着的）"; }
    const link = el("a", { href: "https://my.telegram.org/apps", target: "_blank", rel: "noopener noreferrer",
      text: "my.telegram.org/apps" });
    const step2 = el("section", { className: "tgstep" }, el("h4", { text: "2. 填 API ID 和 API Hash" }),
      el("p", { className: "note" }, "每个 Telegram 号自己申请一对，免费，在 ", link, " 申请。"),
      el("ol", {},
        el("li", { text: "用这个 Telegram 号的手机号登录 my.telegram.org，验证码会发到 Telegram 里" }),
        el("li", { text: "点 API development tools，App title 和 Short name 随便填，提交" }),
        el("li", { text: "复制页面上的 api_id 和 api_hash，填到下面两格" })),
      el("div", { className: "row2" }, apiId, apiHash), saved);

    // 3. phone, code, 2FA password
    const phone = el("input", { type: "tel", className: "grow", autocomplete: "off",
      placeholder: "手机号，带国家区号，比如 +8613812345678", value: typed.phone });
    phone.oninput = () => { typed.phone = phone.value; };
    const sendBtn = el("button", { className: "act", type: "button", text: "发送验证码" });
    const code = el("input", { type: "text", inputMode: "numeric", autocomplete: "one-time-code", maxLength: 8,
      placeholder: "验证码", style: "width:9em" });
    const codeBtn = el("button", { className: "act primary", type: "button", text: "登录" });
    const codeRow = el("div", { className: "row2", hidden: true }, code, codeBtn);
    const pw = el("input", { type: "password", autocomplete: "off", placeholder: "两步验证密码", className: "grow" });
    const pwBtn = el("button", { className: "act primary", type: "button", text: "登录" });
    const pwRow = el("div", { hidden: true },
      el("p", { className: "note", text: "这个号开了两步验证，再填一下两步验证的密码。" }),
      el("div", { className: "row2" }, pw, pwBtn));
    const msg = el("p", { className: "note" });
    const err = el("p", { className: "note err" });
    const step3 = el("section", { className: "tgstep" }, el("h4", { text: "3. 登录 Telegram" }),
      el("div", { className: "row2" }, phone, sendBtn), codeRow, pwRow, msg, err);

    const said = flash && el("p", { className: flash.warn ? "note err" : "note", text: flash.text });
    flash = null;
    root.replaceChildren(intro(), ...(said ? [said] : []), step1, step2, step3);

    let curStep = null;
    function showStep(step) {
      curStep = step;
      codeRow.hidden = step !== "code" && step !== "password";
      code.disabled = codeBtn.disabled = step !== "code";
      pwRow.hidden = step !== "password";
    }

    function apply(st) {
      const ok = st.component === "ok", busy = st.component === "installing", inst = st.install || {};
      compNote.hidden = ok;
      compState.className = ok ? "ok" : "note";
      compState.textContent = ok ? "已安装" : busy ? (inst.text || "正在安装") + "…" : st.component === "failed" ? "安装失败" : "还没安装";
      compBtn.hidden = ok || busy;
      compBtn.textContent = st.component === "failed" ? "重试" : "安装";
      compLine.textContent = busy ? (inst.line || "") : "";
      compErr.textContent = st.component === "failed" ? (inst.error || "") : "";
      step3.classList.toggle("off", !ok);
      for (const x of [phone, sendBtn, code, codeBtn, pw, pwBtn]) x.disabled = !ok;
      const step = ok && st.login ? st.login.step : null;
      showStep(step);
      if (!step) msg.textContent = "";
      else if (step === "code" && !msg.textContent) msg.textContent = `验证码已经发到 ${st.login.phone} 这个号，填进来就行。`;
    }

    // After a failure the server knows best whether the login is still waiting for a code or a password.
    async function resync() {
      try { const st = await call("/api/telegram/status"); if (my === gen) apply(st); } catch { /* keep what is shown */ }
    }

    async function poll() {
      if (my !== gen || !visible(root)) return;
      try {
        const st = await call("/api/telegram/status");
        if (my !== gen) return;
        apply(st);
        if (st.component === "installing") timer = setTimeout(poll, 1500);
        else if (st.component === "ok") compLine.textContent = "";
      } catch (e) { compErr.textContent = "读不到安装进度，" + errText(e); timer = setTimeout(poll, 4000); }
    }

    // Run one click's request with its button showing it is busy; a failure lands in errBox and the steps follow
    // the server again.
    async function busy(btn, label, errBox, work) {
      const was = btn.textContent;
      let failed = false;
      btn.disabled = true; btn.textContent = label; errBox.textContent = "";
      try { await work(); }
      catch (e) { failed = true; errBox.textContent = errText(e); }
      finally { btn.disabled = false; btn.textContent = was; showStep(curStep); }  // the code's 登录 stays off at the password step
      if (failed && btn !== compBtn) await resync();
    }

    compBtn.onclick = () => busy(compBtn, "正在开始…", compErr, async () => {
      const r = await call("/api/telegram/install", {});
      apply(r.status);
      clearTimeout(timer); timer = setTimeout(poll, 1000);
    });

    sendBtn.onclick = () => busy(sendBtn, "正在发送…", err, async () => {
      msg.textContent = "";
      const r = await call("/api/telegram/login/start",
        { api_id: apiId.value.trim(), api_hash: apiHash.value.trim(), phone: phone.value.trim() });
      msg.textContent = r.via === "app"
        ? `验证码发到了 ${r.phone} 这个号已经登录着的 Telegram 里（“Telegram”官方账号发来的消息），短信里没有。`
        : r.via === "sms" ? `验证码用短信发到了 ${r.phone}。` : `验证码已经发给 ${r.phone}。`;
      showStep("code");
      code.value = ""; code.focus();
    });

    codeBtn.onclick = () => busy(codeBtn, "正在登录…", err, async () => {
      const r = await call("/api/telegram/login/code", { code: code.value.trim() });
      if (r.step === "password") { showStep("password"); msg.textContent = ""; pw.focus(); return; }
      render();
    });

    pwBtn.onclick = () => busy(pwBtn, "正在登录…", err, async () => {
      const password = pw.value;
      pw.value = "";  // never left in the field, whatever the answer
      await call("/api/telegram/login/password", { password });
      render();
    });

    const onEnter = (input, btn) => input.addEventListener("keydown", ev => {
      if (ev.key === "Enter" && !ev.isComposing && !btn.disabled) { ev.preventDefault(); btn.click(); }
    });
    onEnter(phone, sendBtn); onEnter(apiHash, sendBtn); onEnter(code, codeBtn); onEnter(pw, pwBtn);

    apply(s);
    if (s.component === "installing") timer = setTimeout(poll, 1500);
  }

  window.tgSetup = { render };
})();
