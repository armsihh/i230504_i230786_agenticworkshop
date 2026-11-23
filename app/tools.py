"""DoubleAAds agent tools.

Rule: user_id always comes from runtime.context (set by the API), never from a model argument,
so a prompt like "show me user B's campaigns" can't reach another user's data.
Tools return short text: fewer tokens back into the model means lower cost.
"""
import json

from langchain.tools import ToolRuntime, tool
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from app import ads
from app.context import AdContext


class AdCopy(BaseModel):
    headline: str = Field(description="Short headline")
    body: str = Field(description="Main ad text")
    cta: str = Field(description="Call to action button text, e.g. 'Shop now'")


def _safe(fn):
    """Turn AdError into a message the model can relay, instead of crashing the run."""
    try:
        return fn()
    except ads.AdError as e:
        return f"Error: {e}"


def _uid(runtime: ToolRuntime[AdContext]) -> str:
    return runtime.context.user_id


# ── campaign tools ──

@tool
def create_campaign(name: str, product: str, audience: str, platform: str, goal: str,
                    daily_budget: float, days: int, runtime: ToolRuntime[AdContext]) -> str:
    """Create a draft ad campaign (nothing is spent). platform: google_search, meta, instagram, tiktok or linkedin.
    goal: awareness, traffic, leads or sales. daily_budget in USD."""
    def run():
        c = ads.create_campaign(_uid(runtime), name, product, audience, platform, goal, daily_budget, days)
        return f"Created draft campaign {c['id']} '{c['name']}' on {c['platform']}, ${c['daily_budget']:.0f}/day for {c['days']} days."
    return _safe(run)


@tool
def list_campaigns(runtime: ToolRuntime[AdContext]) -> str:
    """List this business's campaigns with id, platform, goal, budget and status."""
    rows = ads.list_campaigns(_uid(runtime))
    if not rows:
        return "No campaigns yet."
    return "\n".join(f"{c['id']} | {c['name']} | {c['platform']} | {c['goal']} | ${c['daily_budget']:.0f}/day x "
                     f"{c['days']}d | {c['status']}" for c in rows)


@tool
def estimate_budget(daily_budget: float, days: int, platform: str) -> str:
    """Estimate impressions, clicks and conversions for a budget on a platform, from industry benchmarks."""
    return _safe(lambda: json.dumps(ads.estimate(daily_budget, days, platform)))


# ── copywriting tools ──

@tool
def check_ad_copy(headline: str, body: str, cta: str, platform: str) -> str:
    """Check ad copy against the platform's length limits and ad policy (banned claims, all caps, etc.).
    Use this before saving variants."""
    problems = ads.check_copy(headline, body, cta, platform)
    return "Passes all checks." if not problems else "Problems: " + "; ".join(problems)


@tool
def save_ad_variants(campaign_id: str, variants: list[AdCopy], runtime: ToolRuntime[AdContext]) -> str:
    """Save 1-5 ad copy variants to a campaign for A/B testing. Each is checked first; nothing saves if any fails."""
    def run():
        saved = ads.save_variants(_uid(runtime), campaign_id, [v.model_dump() for v in variants])
        return "Saved: " + ", ".join(f"variant {v['label']} ({v['id']})" for v in saved)
    return _safe(run)


@tool
def list_ad_variants(campaign_id: str, runtime: ToolRuntime[AdContext]) -> str:
    """List a campaign's ad variants with their ids and copy."""
    def run():
        rows = ads.list_variants(_uid(runtime), campaign_id)
        return "\n".join(f"{v['label']} ({v['id']}): {v['headline']} | {v['body']} | {v['cta']}" for v in rows) \
            or "No variants yet."
    return _safe(run)


@tool
def make_creative(variant_id: str, runtime: ToolRuntime[AdContext], size: str = "square", theme: str = "bold") -> str:
    """Make an image creative (banner) for a saved ad variant. size: square, story or landscape.
    theme: bold, fresh, minimal or warm. Returns a link the website shows."""
    def run():
        v = ads.get_variant(_uid(runtime), variant_id)
        return f"Creative ready: /creatives/{v['id']}.svg?size={size}&theme={theme}"
    return _safe(run)


# ── results tools ──

@tool
def record_results(variant_id: str, impressions: int, clicks: int, conversions: int, spend: float,
                   runtime: ToolRuntime[AdContext]) -> str:
    """Record performance numbers the business reports for one ad variant (spend in USD)."""
    def run():
        ads.record_results(_uid(runtime), variant_id, impressions, clicks, conversions, spend)
        return f"Recorded results for {variant_id}."
    return _safe(run)


@tool
def analyze_ab_test(campaign_id: str, runtime: ToolRuntime[AdContext]) -> str:
    """Compare a campaign's variants: CTR, conversion rate, cost per conversion, statistical winner and fixes."""
    return _safe(lambda: json.dumps(ads.analyze(_uid(runtime), campaign_id)))


# ── risky action: needs a human yes ──

@tool
def launch_campaign(campaign_id: str, runtime: ToolRuntime[AdContext]) -> str:
    """Launch a campaign so it starts spending money. Always pauses for the owner's explicit approval first."""
    def run():
        uid = _uid(runtime)
        c = ads.get_campaign(uid, campaign_id)
        total = c["daily_budget"] * c["days"]
        # Code enforces the approval: the run pauses here until the API resumes it with the owner's answer
        answer = interrupt({"action": "launch_campaign", "campaign_id": campaign_id,
                            "question": f"Launch '{c['name']}' on {c['platform']}? It will spend up to "
                                        f"${c['daily_budget']:.0f}/day for {c['days']} days (${total:,.0f} total). "
                                        f"Reply yes to confirm."})
        if str(answer).strip().lower() not in ("yes", "y", "confirm", "approve", "approved"):
            return "Launch cancelled by the owner. Nothing was spent."
        ads.launch(uid, campaign_id)
        return f"Campaign {campaign_id} is now live (simulated: no real ad account is connected)."
    return _safe(run)


CAMPAIGN_TOOLS = [create_campaign, list_campaigns, estimate_budget, launch_campaign]
COPY_TOOLS = [check_ad_copy, save_ad_variants, list_ad_variants, make_creative]
ANALYST_TOOLS = [record_results, analyze_ab_test, list_ad_variants]
ALL_TOOLS = [*CAMPAIGN_TOOLS, check_ad_copy, save_ad_variants, list_ad_variants, make_creative,
             record_results, analyze_ab_test]
