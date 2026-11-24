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
    setMode("login");   // after logging out, people come back to log in, not to sign up again
  }

  // ── log in / create account ──
  let mode = "login";
  function setMode(next) {
    mode = next;
    const signup = mode === "signup";
    $("tabLogin").setAttribute("aria-selected", String(!signup));
    $("tabSignup").setAttribute("aria-selected", String(signup));
    $("bizRow").hidden = !signup;
    $("authTitle").textContent = signup ? "Create your account" : "Welcome back";
    $("authSub").textContent = signup ? "Your AI ad team is ready in seconds." : "Log in to your AI ad agency.";
    $("signinLabel").textContent = signup ? "Create account" : "Log in";
    $("password").autocomplete = signup ? "new-password" : "current-password";
    $("signinErr").textContent = "";
    (signup ? $("bizName") : $("username")).focus();
  }
  $("tabLogin").addEventListener("click", () => setMode("login"));
  $("tabSignup").addEventListener("click", () => setMode("signup"));

  $("signinForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = $("username").value.trim(), password = $("password").value, name = $("bizName").value.trim();
    const fail = (msg) => { $("signinErr").textContent = msg; };
    if (mode === "signup" && !name) return fail("Enter your business name.");
    if (!username) return fail("Enter a username.");
    if (mode === "signup" && !/^[A-Za-z0-9_.-]{3,30}$/.test(username))
      return fail("Usernames are 3 to 30 characters: letters, numbers, dots, dashes or underscores.");
    if (!password) return fail("Enter your password.");
    if (mode === "signup" && password.length < 8) return fail("Use a password of at least 8 characters.");
    fail("");
    $("signinBtn").disabled = true;
    try {
      const body = mode === "signup" ? { username, password, business_name: name } : { username, password };
      session = await api(mode === "signup" ? "/api/signup" : "/api/login", { method: "POST", body, timeoutMs: 75000 });
      store.set("daa.session", session);
      store.del("daa.thread");
      $("password").value = "";
      showWorkspace();
    } catch (err) {
      fail(err.message);
    } finally {
      $("signinBtn").disabled = false;
    }
  });
  ["username", "password", "bizName"].forEach((id) => $(id).addEventListener("input", () => { $("signinErr").textContent = ""; }));

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

  // The message box grows with the text up to 5 lines, then scrolls. Enter sends, Shift+Enter adds a line.
  function autosize() {
    const box = $("msg");
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight, 5 * 24 + 4) + "px";
    box.style.overflowY = box.scrollHeight > 5 * 24 + 4 ? "auto" : "hidden";
  }
  $("msg").addEventListener("input", autosize);
  // Resizing the window (or the panels) changes how the text wraps, so re-measure when the box's width changes
  if ("ResizeObserver" in window) {
    let lastWidth = 0;
    new ResizeObserver(([entry]) => {
      const w = Math.round(entry.contentRect.width);
      if (w !== lastWidth) { lastWidth = w; autosize(); }
    }).observe($("msg"));
  } else {
    window.addEventListener("resize", autosize);
  }
  $("msg").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("chatForm").requestSubmit(); }
  });
  $("chatForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("msg").value.trim();
    if (!text) return;
    $("msg").value = "";
    autosize();
    send(text);
  });
  $("quick").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-prompt]");
    if (b) { $("msg").value = b.dataset.prompt; autosize(); $("msg").focus(); }
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

  // Facts captured from campaigns carry a prefix ("Advertises on: instagram"); show it as a small label so a
  // bare "$20" or "instagram" never appears without context. Free-form notes from the remember tool: "Note".
  const FACT_LABELS = [
    [/^Sells:\s*/i, "Sells"], [/^Target audience:\s*/i, "Audience"], [/^Advertises on:\s*/i, "Platform"],
    [/^Typical daily budget:\s*/i, "Budget"], [/^Last A\/B winner was\s*/i, "Last winner"],
  ];
  const PLATFORM_NAMES = { instagram: "Instagram", meta: "Meta", tiktok: "TikTok", linkedin: "LinkedIn", google_search: "Google Search" };

  function labelFact(fact) {
    for (const [re, label] of FACT_LABELS) {
      if (re.test(fact)) {
        let value = fact.replace(re, "");
        if (label === "Platform") value = PLATFORM_NAMES[value.toLowerCase()] || value;
        if (label === "Budget") value += " / day";
        return { label, value };
      }
    }
    return { label: "Note", value: fact };
  }

  function renderFacts(facts) {
    const order = ["Sells", "Audience", "Platform", "Budget", "Last winner", "Note"];
    const items = facts.map(labelFact).sort((a, b) => order.indexOf(a.label) - order.indexOf(b.label));
    $("facts").innerHTML = items.length
      ? items.map((f) => `<div class="fact slide-in"><span class="fact-l">${esc(f.label)}</span>${esc(f.value)}</div>`).join("")
      : `<span class="muted">Nothing yet. Tell me about your brand.</span>`;
  }

  $("forgetBtn").addEventListener("click", async () => {
    if (!confirm("Forget everything DoubleAAds remembers about your business?")) return;
    try { await api("/api/me/memory", { method: "DELETE", token: session.token }); renderFacts([]); }
    catch (err) { alert(err.message); }
  });

  // ── start ──
  if (session && session.token) showWorkspace(); else showSignin();
})();
