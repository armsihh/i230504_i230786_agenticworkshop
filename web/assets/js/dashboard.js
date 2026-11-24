// Admin metrics dashboard: every number comes from the live admin API.
(() => {
  const { api, icon, esc, countUp, API } = DAA;
  const $ = (id) => document.getElementById(id);
  let password = null;
  try { password = sessionStorage.getItem("daa.admin"); } catch { /* storage blocked */ }

  const svgNS = 'xmlns="http://www.w3.org/2000/svg"';

  async function load() {
    const auth = { admin: password, timeoutMs: 75000 };
    const [ov, usage, events] = await Promise.all([
      api("/api/admin/overview", auth), api("/api/admin/usage?limit=1000", auth), api("/api/admin/events?limit=12", auth),
    ]);
    render(ov, usage, events);
  }

  function render(ov, usage, events) {
    const byStatus = Object.fromEntries(ov.campaigns.map((c) => [c.status, c.n]));
    const blocked = ov.events.filter((e) => e.kind.startsWith("blocked")).reduce((s, e) => s + e.n, 0);
    countUp($("sBiz"), ov.businesses);
    countUp($("sLive"), byStatus.active || 0);
    $("sDrafts").textContent = `${byStatus.draft || 0} drafts waiting`;
    countUp($("sBlocked"), blocked);
    countUp($("sCost"), ov.usage.spent_today_usd, { decimals: 4, prefix: "$" });
    const pct = Math.min(100, (ov.usage.spent_today_usd / ov.usage.daily_budget_usd) * 100);
    $("capBar").style.width = Math.max(pct, 1.5) + "%";
    $("sCap").textContent = `${pct.toFixed(1)}% of the $${ov.usage.daily_budget_usd.toFixed(2)} daily cap`;

    usageChart(usage);
    donut(byStatus);
    tokens(ov.usage);
    renderEvents(events);
    results(ov.results);
    DAA.reveal();
  }

  // Requests per day (bars) and average latency (line), last 7 days
  function usageChart(rows) {
    const days = [...Array(7)].map((_, i) => {
      const d = new Date(Date.now() - (6 - i) * 864e5);
      return { key: d.toISOString().slice(0, 10), label: d.toLocaleDateString(undefined, { weekday: "short" }), n: 0, lat: 0 };
    });
    const idx = Object.fromEntries(days.map((d, i) => [d.key, i]));
    rows.forEach((r) => { const i = idx[r.created_at.slice(0, 10)]; if (i != null) { days[i].n++; days[i].lat += r.latency_ms; } });
    days.forEach((d) => (d.lat = d.n ? d.lat / d.n / 1000 : null));
    const W = 700, H = 220, maxN = Math.max(1, ...days.map((d) => d.n));
    const maxL = Math.max(1, ...days.map((d) => d.lat || 0));
    const slot = W / 7, bw = slot * 0.55;
    const bars = days.map((d, i) => {
      const h = (d.n / maxN) * (H - 40), x = i * slot + (slot - bw) / 2, y = H - h;
      return `<rect class="grow-bar" style="animation-delay:${i * 0.07}s" x="${x}" y="${y}" width="${bw}" height="${Math.max(h, 2)}" rx="8"
        fill="${i === 6 ? "#98E8DE" : "#45A9A9"}"><title>${d.n} requests</title></rect>
        <text x="${i * slot + slot / 2}" y="${y - 8}" fill="#CFC2E0" font-size="13" text-anchor="middle">${d.n || ""}</text>`;
    }).join("");
    const pts = days.map((d, i) => d.lat == null ? null : [i * slot + slot / 2, H - 20 - (d.lat / maxL) * (H - 70)]).filter(Boolean);
    const line = pts.length > 1 ? `<path d="M${pts.map((p) => p.join(" ")).join(" L")}" stroke="#FFB86B" stroke-width="3" fill="none" stroke-linejoin="round"/>` : "";
    const dots = days.map((d, i) => d.lat == null ? "" :
      `<circle cx="${i * slot + slot / 2}" cy="${H - 20 - (d.lat / maxL) * (H - 70)}" r="4.5" fill="#FFB86B"><title>${d.lat.toFixed(1)} s avg</title></circle>`).join("");
    $("usageChart").innerHTML =
      `<svg ${svgNS} width="100%" height="230" viewBox="0 0 ${W} ${H + 4}" preserveAspectRatio="none" role="img"
        aria-label="Requests per day and average latency for the last 7 days">
        <path d="M0 ${H}H${W}" stroke="rgba(255,255,255,0.1)"/>${bars}${line}${dots}</svg>
       <div style="display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); font-size: 12px; color: var(--text-4); text-align: center; margin-top: 6px">
        ${days.map((d) => `<span>${esc(d.label)}</span>`).join("")}</div>`;
  }

  function donut(byStatus) {
    const parts = [["Live", byStatus.active || 0, "#98E8DE"], ["Draft", byStatus.draft || 0, "#6F6FB0"]];
    const total = parts.reduce((s, p) => s + p[1], 0);
    let offset = 25;
    const rings = total ? parts.map(([, n, c]) => {
      const len = (n / total) * 100;
      const el = `<circle cx="21" cy="21" r="15.9" fill="none" stroke="${c}" stroke-width="6" stroke-dasharray="${len} ${100 - len}" stroke-dashoffset="${offset}"/>`;
      offset -= len;
      return el;
    }).join("") : "";
    $("donut").innerHTML = `<div style="display: flex; align-items: center; gap: 22px">
      <svg ${svgNS} width="120" height="120" viewBox="0 0 42 42" role="img" aria-label="${parts.map((p) => `${p[0]} ${p[1]}`).join(", ")}">
        <circle cx="21" cy="21" r="15.9" fill="none" stroke="rgba(255,255,255,0.08)" stroke-width="6"/>${rings}
        <text x="21" y="23.5" fill="#F4EEFB" font-size="8" text-anchor="middle" font-family="Space Grotesk, sans-serif">${total}</text></svg>
      <div style="display: flex; flex-direction: column; gap: 10px; font-size: 14px">
        ${parts.map(([l, n, c]) => `<span style="display: flex; align-items: center; gap: 8px"><i class="sw" style="background: ${c}"></i>${l} · ${n}</span>`).join("")}
      </div></div>`;
  }

  function tokens(u) {
    const n = Math.max(1, u.requests);
    const tin = u.tokens_in / n, tout = u.tokens_out / n;
    $("tokAvg").textContent = `${Math.round(tin).toLocaleString()} · ${Math.round(tout).toLocaleString()}`;
    const total = tin + tout || 1;
    $("tokSplit").innerHTML = `<div class="grow-x" style="flex: ${tin / total}; border-radius: 999px 0 0 999px; background: var(--teal)"></div>
      <div class="grow-x" style="flex: ${Math.max(tout / total, 0.02)}; border-radius: 0 999px 999px 0; background: var(--amber)"></div>`;
    $("tokNote").textContent = `${u.requests.toLocaleString()} chats · avg ${(u.avg_latency_ms / 1000).toFixed(1)} s · $${u.cost_usd.toFixed(4)} all time`;
  }

  function renderEvents(events) {
    const meta = (k) => k.startsWith("blocked") ? ["shield-x", "warn", k.replace("blocked_", "Blocked: ").replace("_", " ")]
      : k === "tool_call" ? ["wand-sparkles", "", "Tool call"] : ["activity", "", k];
    $("events").innerHTML = events.length ? events.slice(0, 8).map((e) => {
      const [ic, cls, title] = meta(e.kind);
      const detail = e.kind === "tool_call" ? e.detail.split(" ")[0] : e.detail;
      return `<div class="ev"><span class="ev-ic ${cls}">${icon(ic, "icon icon-sm")}</span>
        <div style="flex: 1; min-width: 0"><div class="ev-t">${esc(title)}</div><div class="ev-d">${esc(detail)}</div></div>
        <span class="muted" style="font-size: 12px; white-space: nowrap">${esc(e.created_at.slice(5, 16))}</span></div>`;
    }).join("") : `<div class="empty">No events yet.</div>`;
  }

  function results(r) {
    const max = Math.max(1, r.impressions);
    const rows = [["Impressions", r.impressions, "#6F6FB0"], ["Clicks", r.clicks, "#45A9A9"], ["Conversions", r.conversions, "#98E8DE"]];
    $("results").innerHTML = rows.map(([l, v, c]) => `<div>
      <div style="display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 6px"><span>${l}</span><span style="color: var(--text-2)">${Number(v).toLocaleString()}</span></div>
      <div class="bar"><div class="grow-x" style="width: ${Math.max((v / max) * 100, 1)}%; background: ${c}"></div></div></div>`).join("") +
      `<div class="muted" style="font-size: 13px">Spend recorded: $${Number(r.spend).toFixed(2)}</div>`;
  }

  async function health() {
    try {
      const res = await fetch(API + "/health");
      const ok = res.ok && (await res.text()).trim() === "ok";
      $("health").innerHTML = `<span class="dot ${ok ? "dot-live" : ""}" style="${ok ? "" : "background: #ff8a8a"}"></span>${ok ? "API healthy" : "API not responding"}`;
      $("health").style.color = ok ? "var(--mint-soft)" : "#ffb4b4";
    } catch {
      $("health").innerHTML = `<span class="dot" style="background: #ff8a8a"></span>API not reachable`;
    }
  }

  async function open() {
    $("gate").hidden = true;
    $("dash").hidden = false;
    health();
    try { await load(); }
    catch (err) {
      if (err.status === 401) return lock("That password didn't work.");
      $("events").innerHTML = `<div class="error">${esc(err.message)}</div>`;
    }
  }

  function lock(msg = "") {
    password = null;
    try { sessionStorage.removeItem("daa.admin"); } catch { /* ignore */ }
    $("dash").hidden = true;
    $("gate").hidden = false;
    $("gateErr").textContent = msg;
    $("pw").focus();
  }

  $("gateForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const pw = $("pw").value;
    if (!pw) { $("gateErr").textContent = "Enter the admin password."; return; }
    password = pw;
    try { sessionStorage.setItem("daa.admin", pw); } catch { /* ignore */ }
    open();
  });
  $("pw").addEventListener("input", () => { $("gateErr").textContent = ""; });
  $("refreshBtn").addEventListener("click", () => load().catch((err) => alert(err.message)));
  $("lockBtn").addEventListener("click", () => lock());

  if (password) open(); else lock();
})();
