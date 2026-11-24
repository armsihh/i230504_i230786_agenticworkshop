"""DoubleAAds business logic. Plain functions, no LLM calls: cheap, deterministic, easy to test.

Every function takes user_id first and only ever reads/writes that user's rows.
The tools in tools.py pass user_id from the runtime context, never from the model.
"""
import math
import re

from app.db import execute, new_id, now, query
from app.textnorm import normalize

# Platform rules and rough industry benchmarks (USD). Benchmarks are planning assumptions,
# not guarantees: the estimate tool says so in its output.
PLATFORMS = {
    "google_search": {"headline": 30, "body": 90, "cta": 25, "ctr": 0.032, "cpc": 0.80, "cvr": 0.040},
    "meta":          {"headline": 40, "body": 125, "cta": 25, "ctr": 0.009, "cpc": 0.70, "cvr": 0.025},
    "instagram":     {"headline": 40, "body": 125, "cta": 25, "ctr": 0.008, "cpc": 0.75, "cvr": 0.020},
    "tiktok":        {"headline": 40, "body": 100, "cta": 25, "ctr": 0.008, "cpc": 0.60, "cvr": 0.015},
    "linkedin":      {"headline": 70, "body": 150, "cta": 25, "ctr": 0.005, "cpc": 5.00, "cvr": 0.020},
}
GOALS = {"awareness", "traffic", "leads", "sales"}
MAX_DAILY_BUDGET = 1000.0   # above this, refuse: a typo like 50000 should never get through
MAX_VARIANTS = 10           # per campaign: A to J
TEXT_LIMITS = {"name": 100, "product": 300, "audience": 300}

# Claims ad platforms reject or that mislead customers
BANNED = [
    (r"\bguarantee(d|s)?\b", "guarantee claims"),
    (r"(?<!\w)(no\.?\s*1|#\s*1|number one|best in the world)\b", "unverifiable superlatives"),
    (r"\b(cure|cures|heal|heals)\b", "health claims"),
    (r"\b(risk[- ]free|free money|get rich)\b", "misleading financial claims"),
    (r"\b100\s*%", "absolute percentage claims"),
    (r"click here", "vague CTA ('click here')"),
]


class AdError(ValueError):
    """A problem the agent should explain to the user (bad input, not found, policy)."""


def _number(value, what):
    """Reject NaN and infinity: `nan <= x` is always False, so they slip past range checks and corrupt the DB."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise AdError(f"{what} must be a number.") from None
    if not math.isfinite(value):
        raise AdError(f"{what} must be a real number.")
    return value


def _text(value, what):
    value = (value or "").strip()
    if not value:
        raise AdError(f"{what} can't be empty.")
    if len(value) > TEXT_LIMITS[what]:
        raise AdError(f"{what} is too long (max {TEXT_LIMITS[what]} characters).")
    return value


# ── campaigns ──

def create_campaign(user_id, name, product, audience, platform, goal, daily_budget, days):
    platform, goal = platform.strip().lower(), goal.strip().lower()
    name, product, audience = _text(name, "name"), _text(product, "product"), _text(audience, "audience")
    daily_budget, days = _number(daily_budget, "Daily budget"), _number(days, "Days")
    if platform not in PLATFORMS:
        raise AdError(f"Unknown platform '{platform}'. Choose one of: {', '.join(PLATFORMS)}.")
    if goal not in GOALS:
        raise AdError(f"Unknown goal '{goal}'. Choose one of: {', '.join(sorted(GOALS))}.")
    if not 1 <= daily_budget <= MAX_DAILY_BUDGET:
        raise AdError(f"Daily budget must be between $1 and ${MAX_DAILY_BUDGET:,.0f}.")
    if not 1 <= days <= 90 or days != int(days):
        raise AdError("Campaign length must be 1 to 90 whole days.")
    cid = new_id("cmp")
    execute("INSERT INTO campaigns (id, user_id, name, product, audience, platform, goal, daily_budget, days, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)",
            (cid, user_id, name, product, audience, platform, goal, float(daily_budget), int(days), now()))
    return get_campaign(user_id, cid)


def get_campaign(user_id, campaign_id):
    rows = query("SELECT * FROM campaigns WHERE id = ? AND user_id = ?", (campaign_id, user_id))
    if not rows:   # same message whether it doesn't exist or belongs to someone else
        raise AdError(f"No campaign '{campaign_id}' found in your account.")
    return rows[0]


def list_campaigns(user_id):
    return query("SELECT * FROM campaigns WHERE user_id = ? ORDER BY created_at DESC", (user_id,))


def set_status(user_id, campaign_id, status):
    get_campaign(user_id, campaign_id)
    execute("UPDATE campaigns SET status = ? WHERE id = ? AND user_id = ?", (status, campaign_id, user_id))


# ── ad copy ──

def check_copy(headline, body, cta, platform):
    """Return a list of problems (empty list = passes)."""
    platform = platform.strip().lower()
    if platform not in PLATFORMS:
        return [f"Unknown platform '{platform}'."]
    limits = PLATFORMS[platform]
    problems = []
    for field, text in (("headline", headline), ("body", body), ("cta", cta)):
        if not text.strip():
            problems.append(f"{field} is empty")
        elif len(text) > limits[field]:
            problems.append(f"{field} is {len(text)} chars, {platform} allows {limits[field]}")
    full = f"{headline} {body} {cta}"
    flat = normalize(full)   # catches "G u a r a n t e e d", zero-width tricks, full-width letters
    for pattern, why in BANNED:
        if re.search(pattern, flat, re.I):
            problems.append(f"contains {why}")
    if sum(c.isupper() for c in full) > 0.5 * max(1, sum(c.isalpha() for c in full)):
        problems.append("too many capital letters")
    if full.count("!") > 2:
        problems.append("too many exclamation marks")
    return problems


def save_variants(user_id, campaign_id, variants):
    """variants: list of {headline, body, cta}. All must pass check_copy, or nothing is saved."""
    camp = get_campaign(user_id, campaign_id)
    if not 1 <= len(variants) <= 5:
        raise AdError("Save between 1 and 5 variants at a time.")
    failures = {}
    for i, v in enumerate(variants):
        problems = check_copy(v["headline"], v["body"], v["cta"], camp["platform"])
        if problems:
            failures[chr(65 + i)] = problems
    if failures:
        detail = "; ".join(f"variant {k}: {', '.join(p)}" for k, p in failures.items())
        raise AdError(f"Not saved, fix these first: {detail}")
    existing = len(query("SELECT id FROM variants WHERE campaign_id = ? AND user_id = ?", (campaign_id, user_id)))
    if existing + len(variants) > MAX_VARIANTS:
        raise AdError(f"A campaign can have at most {MAX_VARIANTS} variants ({existing} saved already).")
    saved = []
    for i, v in enumerate(variants):
        vid, label = new_id("var"), chr(65 + existing + i)   # A, B, C ... continuing on
        execute("INSERT INTO variants (id, campaign_id, user_id, label, headline, body, cta, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (vid, campaign_id, user_id, label, v["headline"].strip(), v["body"].strip(), v["cta"].strip(), now()))
        saved.append({"id": vid, "label": label, **v})
    return saved


def list_variants(user_id, campaign_id):
    get_campaign(user_id, campaign_id)
    return query("SELECT * FROM variants WHERE campaign_id = ? AND user_id = ? ORDER BY label",
                 (campaign_id, user_id))


def get_variant(user_id, variant_id):
    rows = query("SELECT * FROM variants WHERE id = ? AND user_id = ?", (variant_id, user_id))
    if not rows:
        raise AdError(f"No ad variant '{variant_id}' found in your account.")
    return rows[0]


# ── budget estimate ──

def estimate(daily_budget, days, platform):
    platform = platform.strip().lower()
    if platform not in PLATFORMS:
        raise AdError(f"Unknown platform '{platform}'. Choose one of: {', '.join(PLATFORMS)}.")
    daily_budget, days = _number(daily_budget, "Daily budget"), _number(days, "Days")
    if daily_budget <= 0 or days <= 0:
        raise AdError("Budget and days must be positive.")
    b = PLATFORMS[platform]
    total = daily_budget * days
    clicks = total / b["cpc"]
    impressions = clicks / b["ctr"]
    conversions = clicks * b["cvr"]
    rng = lambda x: (round(x * 0.7), round(x * 1.3))   # ±30%: benchmarks vary a lot
    return {
        "platform": platform, "total_spend": round(total, 2),
        "impressions": rng(impressions), "clicks": rng(clicks), "conversions": rng(conversions),
        "cost_per_conversion": round(total / conversions, 2) if conversions else None,
        "assumptions": f"CTR {b['ctr']:.1%}, CPC ${b['cpc']:.2f}, conversion rate {b['cvr']:.1%} (typical benchmarks)",
    }


# ── results and A/B analysis ──

def record_results(user_id, variant_id, impressions, clicks, conversions, spend):
    get_variant(user_id, variant_id)
    impressions, clicks, conversions, spend = (_number(x, n) for x, n in
                                               zip((impressions, clicks, conversions, spend),
                                                   ("Impressions", "Clicks", "Conversions", "Spend")))
    if min(impressions, clicks, conversions, spend) < 0:
        raise AdError("Numbers can't be negative.")
    if clicks > impressions or conversions > clicks:
        raise AdError("Clicks can't exceed impressions, and conversions can't exceed clicks.")
    execute("INSERT INTO results (id, variant_id, user_id, impressions, clicks, conversions, spend, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id("res"), variant_id, user_id, int(impressions), int(clicks), int(conversions), float(spend), now()))


def _z_test(c1, n1, c2, n2):
    """Two-proportion z-test. Returns confidence (0-1) that the rates really differ."""
    if min(n1, n2) == 0:
        return 0.0
    p = (c1 + c2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0
    z = abs(c1 / n1 - c2 / n2) / se
    return math.erf(z / math.sqrt(2))   # two-sided confidence


def analyze(user_id, campaign_id):
    camp = get_campaign(user_id, campaign_id)
    rows = query(
        "SELECT v.id, v.label, v.headline, COALESCE(SUM(r.impressions), 0) AS impressions, "
        "COALESCE(SUM(r.clicks), 0) AS clicks, COALESCE(SUM(r.conversions), 0) AS conversions, "
        "COALESCE(SUM(r.spend), 0) AS spend FROM variants v LEFT JOIN results r ON r.variant_id = v.id "
        "AND r.user_id = v.user_id WHERE v.campaign_id = ? AND v.user_id = ? GROUP BY v.id, v.label, v.headline "
        "ORDER BY v.label", (campaign_id, user_id))
    if not rows:
        raise AdError("This campaign has no ad variants yet.")
    for r in rows:
        imp, clk, conv, spend = r["impressions"], r["clicks"], r["conversions"], r["spend"]
        r["ctr"] = round(clk / imp, 4) if imp else None
        r["cvr"] = round(conv / clk, 4) if clk else None
        r["cpa"] = round(spend / conv, 2) if conv else None
    with_data = [r for r in rows if r["impressions"]]
    report = {"campaign": camp["name"], "goal": camp["goal"], "variants": rows, "winner": None, "suggestions": []}
    if len(with_data) < 2:
        report["suggestions"].append("Need results for at least 2 variants to compare.")
        return report

    # Judge on what the goal cares about: conversions for leads/sales, clicks otherwise
    by_conv = camp["goal"] in ("leads", "sales")
    num, den = ("conversions", "clicks") if by_conv else ("clicks", "impressions")
    ranked = sorted(with_data, key=lambda r: (r[num] / r[den]) if r[den] else 0, reverse=True)
    best, second = ranked[0], ranked[1]
    conf = _z_test(best[num], best[den], second[num], second[den])
    metric = "conversion rate" if by_conv else "click-through rate"
    if conf >= 0.95:
        report["winner"] = {"label": best["label"], "metric": metric, "confidence": round(conf, 3)}
        report["suggestions"].append(f"Variant {best['label']} wins on {metric} ({conf:.0%} confidence). "
                                     f"Move budget to it and write a new challenger based on its angle.")
    else:
        report["suggestions"].append(f"No clear winner yet ({conf:.0%} confidence, need 95%). Keep the test running.")

    bench = PLATFORMS[camp["platform"]]
    for r in with_data:
        if r["impressions"] >= 1000 and r["ctr"] is not None and r["ctr"] < bench["ctr"] * 0.5:
            r_tip = f"Variant {r['label']} CTR {r['ctr']:.2%} is under half the {camp['platform']} benchmark: " \
                    f"try a sharper headline or a different audience."
            report["suggestions"].append(r_tip)
        if r["clicks"] >= 100 and r["conversions"] == 0:
            report["suggestions"].append(f"Variant {r['label']} gets clicks but no conversions: check the landing page.")
    return report


def check_launchable(user_id, campaign_id):
    camp = get_campaign(user_id, campaign_id)
    if camp["status"] == "active":
        raise AdError("This campaign is already running.")
    if not list_variants(user_id, campaign_id):
        raise AdError("Add at least one ad variant before launching.")
    return camp


def launch(user_id, campaign_id):
    camp = check_launchable(user_id, campaign_id)
    set_status(user_id, campaign_id, "active")
    return camp
