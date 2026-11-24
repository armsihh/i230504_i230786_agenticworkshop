// Agent workspace: sign in, chat with the DoubleAAds agents, see campaigns, banners and remembered facts.
(() => {
  const { api, store, icon, esc, API } = DAA;
  const $ = (id) => document.getElementById(id);
  const AGENT_ICON = { strategist: "target", copywriter: "pen-line", analyst: "trending-up" };
  let session = store.get("daa.session");          // { token, user_id, business_name }
  let busy = false;
  let selectedCampaign = null;

  // ── session ──
  function threadId() {
    let t = store.get("daa.thread");
    if (!t) { t = "web-" + Math.random().toString(36).slice(2, 10); store.set("daa.thread", t); }
    return t;
  }
  function historyKey() { return `daa.history.${session.user_id}.${threadId()}`; }

  function showWorkspace() {
    $("signin").hidden = true;
    $("workspace").hidden = false;
    $("bizTitle").textContent = session.business_name;
    $("avatar").textContent = session.business_name.split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
    renderHistory();
    refreshPanel();
    $("msg").focus();
  }

  function showSignin() {
    $("workspace").hidden = true;
    $("signin").hidden = false;
    $("bizName").focus();
  }

  $("signinForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("bizName").value.trim();
    if (!name) { $("signinErr").textContent = "Enter your business name to continue."; return; }
    $("signinErr").textContent = "";
    $("signinBtn").disabled = true;
    try {
      session = await api("/api/session", { method: "POST", body: { business_name: name }, timeoutMs: 75000 });
      store.set("daa.session", session);
      store.del("daa.thread");
      showWorkspace();
    } catch (err) {
      $("signinErr").textContent = err.message;
    } finally {
      $("signinBtn").disabled = false;
    }
  });
  $("bizName").addEventListener("input", () => { $("signinErr").textContent = ""; });

  $("signOutBtn").addEventListener("click", () => {
    store.del("daa.session"); store.del("daa.thread"); session = null; showSignin();
  });
  $("newChatBtn").addEventListener("click", () => {
    store.del("daa.thread"); threadId(); renderHistory(); $("msg").focus();
  });

  // ── rendering ──
  // Minimal, safe markdown: everything is escaped first, then **bold**, `code`, bullet/numbered lists,
  // and creative links (/creatives/....svg) become image previews.
  function md(text) {
    const lines = esc(text).split("\n");
    let html = "", list = null;
    const inline = (s) => s
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/(\/creatives\/[\w-]+\.svg\?[\w=&;.-]+)/g, (m) =>
        `<img class="creative" alt="Ad creative" loading="lazy" src="${API}${m.replace(/&amp;/g, "&")}">`);
    for (const raw of lines) {
      const line = raw.trim();
      const bullet = line.match(/^[-*•]\s+(.*)/), num = line.match(/^\d+[.)]\s+(.*)/);
      const kind = bullet ? "ul" : num ? "ol" : null;
      if (kind) {
        if (list !== kind) { if (list) html += `</${list}>`; html += `<${kind}>`; list = kind; }
        html += `<li>${inline((bullet || num)[1])}</li>`;
        continue;
      }
      if (list) { html += `</${list}>`; list = null; }
      if (line) html += `<p>${inline(line)}</p>`;
    }
    if (list) html += `</${list}>`;
    return html;
  }

  function addUser(text, animate = true) {
    const el = document.createElement("div");
    el.className = "msg-user" + (animate ? " slide-in" : "");
    el.textContent = text;
    $("feed").appendChild(el);
    scroll();
  }

  function addBot({ reply, agent, tool_calls = [], error = false }, animate = true) {
    const el = document.createElement("div");
    el.className = "msg-bot" + (animate ? " slide-in" : "");
    const who = agent || "DoubleAAds";
    const tools = tool_calls.length
      ? `<div class="tools">${tool_calls.map((t) => `<span>${esc(t.name)}</span>`).join("")}</div>` : "";
    el.innerHTML = `<div class="bot-ic ${esc(agent || "")}">${icon(AGENT_ICON[agent] || "sparkles", "icon icon-sm")}</div>
      <div class="bubble glass"><div class="who">${esc(who)}</div>
      <div class="${error ? "err" : ""}">${md(reply)}</div>${tools}</div>`;
    $("feed").appendChild(el);
    scroll();
  }

  function addApproval(question) {
    const el = document.createElement("div");
    el.className = "approve glass glass-warn slide-in";
    el.innerHTML = `<div class="approve-row"><div class="approve-ic">${icon("lock")}</div>
      <div><div style="font-weight: 600">Approve this launch?</div>
      <div style="font-size: 13px; color: #e8d9c4">${esc(question)}</div></div></div>
      <div class="approve-btns"><button class="btn btn-warn" data-a="yes">Approve launch</button>
      <button class="btn btn-ghost" data-a="no">Not now</button></div>`;
    el.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      el.querySelectorAll("button").forEach((x) => (x.disabled = true));
      send(b.dataset.a === "yes" ? "yes" : "no, not now", b.dataset.a === "yes" ? "Approve launch" : "Not now");
    }));
    $("feed").appendChild(el);
    scroll();
  }

  function setAgent(agent) {
    document.querySelectorAll(".agents-switch span").forEach((s) => s.classList.toggle("on", s.dataset.agent === agent));
  }

  function scroll() { const f = $("feed"); f.scrollTop = f.scrollHeight; }

  function renderHistory() {
    $("feed").innerHTML = "";
    const hist = store.get(historyKey()) || [];
    if (!hist.length) {
      addBot({ reply: `Hi ${session.business_name}! I'm your AI ad team: a strategist, a copywriter and an analyst.\n` +
        "- Tell me what you sell and your daily budget, and I'll plan a campaign\n" +
        "- I'll write two competing ads and make the banners\n" +
        "- Share your results later and I'll tell you which ad really won" }, false);
    }
    hist.forEach((m) => (m.role === "user" ? addUser(m.text, false) : addBot(m, false)));
    setAgent(hist.length ? hist[hist.length - 1].agent : null);
  }

  function remember(entry) {
    const hist = store.get(historyKey()) || [];
    hist.push(entry);
    store.set(historyKey(), hist.slice(-60));
  }

  // ── sending ──
  async function send(text, shown = text) {
    if (busy || !text.trim()) return;
    busy = true;
    $("sendBtn").disabled = true;
    addUser(shown);
    remember({ role: "user", text: shown });

    const thinking = document.createElement("div");
    thinking.className = "thinking slide-in";
    thinking.innerHTML = `<span class="typing"><span></span><span></span><span></span></span><span class="shimmer" id="thinkText">Thinking…</span>`;
    $("feed").appendChild(thinking);
    scroll();
    const slow = setTimeout(() => {
      const t = document.getElementById("thinkText");
      if (t) t.textContent = "Waking up the agent server, this can take up to a minute the first time…";
    }, 9000);

    try {
      const res = await api("/api/chat", { method: "POST", token: session.token,
                                           body: { thread_id: threadId(), message: text } });
      thinking.remove();
      setAgent(res.agent);
      if (res.needs_approval) addApproval(res.reply);   // the card carries the question; no duplicate bubble
      else addBot(res);
      remember({ role: "bot", reply: res.reply, agent: res.agent, tool_calls: res.tool_calls });
      refreshPanel();
    } catch (err) {
      thinking.remove();
      if (err.status === 401) { store.del("daa.session"); session = null; showSignin(); return; }
      addBot({ reply: err.message, error: true });
    } finally {
      clearTimeout(slow);
      busy = false;
      $("sendBtn").disabled = false;
      $("msg").focus();
    }
  }

  $("chatForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("msg").value.trim();
    if (!text) return;
    $("msg").value = "";
    send(text);
  });
  $("quick").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-prompt]");
    if (b) { $("msg").value = b.dataset.prompt; $("msg").focus(); }
  });

  // ── right panel: campaigns, creatives, A/B, memory ──
  async function refreshPanel() {
    try {
      const [campaigns, memory] = await Promise.all([
        api("/api/me/campaigns", { token: session.token }),
        api("/api/me/memory", { token: session.token }),
      ]);
      renderCampaigns(campaigns);
      renderFacts(memory.facts || []);
    } catch (err) {
      if (err.status === 401) { store.del("daa.session"); showSignin(); }
    }
  }

  async function renderCampaigns(campaigns) {
    const area = $("campaignArea");
    if (!campaigns.length) {
      area.innerHTML = `<div class="panel-card glass empty">${icon("megaphone", "icon")}<div>Your campaigns appear here.</div>
        <div style="font-size: 13px; margin-top: 4px">Try "Plan a campaign" below.</div></div>`;
      return;
    }
    const camp = campaigns.find((c) => c.id === selectedCampaign) || campaigns[0];
    selectedCampaign = camp.id;
    const tabs = campaigns.length > 1
      ? `<div class="camp-tabs">${campaigns.slice(0, 6).map((c) =>
          `<button class="glass ${c.id === camp.id ? "on" : ""}" data-id="${esc(c.id)}">${esc(c.name.slice(0, 22))}</button>`).join("")}</div>` : "";
    const status = camp.status === "active" ? `<span class="badge badge-mint">Live</span>` : `<span class="badge badge-amber">Draft</span>`;
    let report = null;
    if (camp.variants.length) {
      try { report = await api(`/api/me/campaigns/${encodeURIComponent(camp.id)}/report`, { token: session.token }); }
      catch { report = null; }
    }
    const stats = Object.fromEntries((report?.variants || []).map((v) => [v.id, v]));
    const creatives = camp.variants.length
      ? `<div class="creatives">${camp.variants.map((v) => {
          const s = stats[v.id];
          const ctr = s && s.ctr != null ? `CTR ${(s.ctr * 100).toFixed(1)}%` : "No results yet";
          const win = report?.winner?.label === v.label;
          return `<div class="creative-card glass ${win ? "glass-hi" : ""}">
            <img alt="Banner for variant ${esc(v.label)}: ${esc(v.headline)}" loading="lazy"
                 src="${API}${esc(v.creative_url)}&theme=${v.label === "A" ? "fresh" : "bold"}">
            <div class="creative-meta"><span>Variant ${esc(v.label)}</span><span style="color: var(--mint)">${win ? "Winner" : esc(ctr)}</span></div>
          </div>`;
        }).join("")}</div>`
      : `<div class="panel-card glass empty">${icon("image", "icon")}<div>No ads yet. Ask the copywriter to write two.</div></div>`;
    const ab = report && report.variants.some((v) => v.impressions)
      ? `<div class="panel-card glass"><div style="display: flex; justify-content: space-between; font-size: 13px; gap: 8px">
          <span style="color: var(--text-2)">A/B result</span>
          <span style="color: var(--mint); font-weight: 600">${report.winner
            ? `Variant ${esc(report.winner.label)} wins · ${Math.round(report.winner.confidence * 100)}%` : "No winner yet"}</span></div>
          <div style="font-size: 13px; color: var(--text-3); margin-top: 8px">${esc(report.suggestions[0] || "")}</div></div>` : "";
    area.innerHTML = `${tabs}
      <div class="panel-card glass slide-in" style="margin-top: ${tabs ? "12px" : "0"}">
        <div style="display: flex; justify-content: space-between; align-items: center; gap: 8px"><h2>${esc(camp.name)}</h2>${status}</div>
        <div class="muted" style="font-size: 13px; margin-top: 2px">${esc(camp.platform)} · ${esc(camp.goal)}</div>
        <div class="kpis"><div><div class="l">Daily</div><div class="v">$${Math.round(camp.daily_budget)}</div></div>
          <div><div class="l">Days</div><div class="v">${camp.days}</div></div>
          <div><div class="l">Total</div><div class="v">$${Math.round(camp.daily_budget * camp.days)}</div></div></div>
      </div>
      <div style="margin-top: 16px">${creatives}</div>
      ${ab ? `<div style="margin-top: 16px">${ab}</div>` : ""}`;
    area.querySelectorAll(".camp-tabs button").forEach((b) => b.addEventListener("click", () => {
      selectedCampaign = b.dataset.id; renderCampaigns(campaigns);
    }));
  }

  function renderFacts(facts) {
    $("facts").innerHTML = facts.length
      ? facts.map((f) => `<span class="slide-in">${esc(f.replace(/^(Sells|Target audience|Advertises on|Typical daily budget): /, ""))}</span>`).join("")
      : `<span class="muted" style="background: none; padding: 0">Nothing yet. Tell me about your brand.</span>`;
  }

  $("forgetBtn").addEventListener("click", async () => {
    if (!confirm("Forget everything DoubleAAds remembers about your business?")) return;
    try { await api("/api/me/memory", { method: "DELETE", token: session.token }); renderFacts([]); }
    catch (err) { alert(err.message); }
  });

  // ── start ──
  if (session && session.token) showWorkspace(); else showSignin();
})();
