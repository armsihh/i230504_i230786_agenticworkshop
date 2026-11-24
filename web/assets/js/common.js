// Shared helpers for every page: API calls, safe storage, icons, scroll reveals, count-up numbers.
const DAA = (() => {
  // localStorage can throw (private mode, blocked storage): never let that break the page
  const store = {
    get(key) { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ } },
    del(key) { try { localStorage.removeItem(key); } catch { /* ignore */ } },
  };

  // Production API by default; developers can point the site at a local server with
  // localStorage "daa.api" = "http://127.0.0.1:8799" (only affects their own browser)
  const API = (store.get("daa.api") || window.DOUBLEAADS_API || "https://doubleaads-api.onrender.com").replace(/\/$/, "");

  async function api(path, { method = "GET", body, token, admin, timeoutMs = 90000 } = {}) {
    const headers = { "Content-Type": "application/json" };
    if (token) headers.Authorization = `Bearer ${token}`;
    if (admin) headers["X-Admin-Password"] = admin;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const res = await fetch(API + path, { method, headers, body: body ? JSON.stringify(body) : undefined,
                                            signal: ctrl.signal });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const err = new Error(data.reply || data.detail || `Request failed (${res.status})`);
        err.status = res.status;
        throw err;
      }
      return data;
    } catch (e) {
      if (e.name === "AbortError") throw new Error("The agent took too long to answer. Please try again.");
      if (e instanceof TypeError) throw new Error("Can't reach the DoubleAAds server. Check your connection.");
      throw e;
    } finally {
      clearTimeout(timer);
    }
  }

  const icon = (name, cls = "icon") =>
    `<svg class="${cls}" aria-hidden="true"><use href="assets/icons.svg#i-${name}"></use></svg>`;

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // Reveal sections as they scroll into view
  function reveal(root = document) {
    const els = root.querySelectorAll(".reveal:not(.in)");
    if (!("IntersectionObserver" in window)) { els.forEach((el) => el.classList.add("in")); return; }
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } });
    }, { threshold: 0.15 });
    els.forEach((el) => io.observe(el));
  }

  // Animate a number from 0 to its target (respects reduced motion)
  function countUp(el, target, { decimals = 0, prefix = "", suffix = "", ms = 1200 } = {}) {
    const fmt = (v) => prefix + v.toLocaleString(undefined, { minimumFractionDigits: decimals,
                                                              maximumFractionDigits: decimals }) + suffix;
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) { el.textContent = fmt(target); return; }
    const start = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - start) / ms);
      el.textContent = fmt(target * (1 - Math.pow(1 - t, 3)));
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  return { API, store, api, icon, esc, reveal, countUp };
})();

document.addEventListener("DOMContentLoaded", () => DAA.reveal());
