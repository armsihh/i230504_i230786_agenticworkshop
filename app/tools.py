"""DoubleAAds agent tools.

Rule: user_id always comes from runtime.context (set by the API), never from a model argument,
so a prompt like "show me user B's campaigns" can't reach another user's data.
Tools return short text: fewer tokens back into the model means lower cost.
"""
import json
import re

from langchain.tools import ToolRuntime, tool
from langchain.messages import ToolMessage
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from app import ads
from app.context import AdContext
from app.memory import remember
from app.security import creative_sig
from app.textnorm import normalize


class AdCopy(BaseModel):
    headline: str = Field(description="Short headline")
    body: str = Field(description="Main ad text")
    cta: str = Field(description="Call to action button text, e.g. 'Shop now'")


_YES = re.compile(r"^(yes|yep|yeah|yup|y|sure|ok|okay|confirm(ed)?|approve(d)?|go ahead|do it|launch( it)?)\b")
_HEDGE = re.compile(r"\b(no|not|don'?t|dont|wait|later|but|stop|cancel|hold|maybe|unless|until)\b")


def is_approval(answer) -> bool:
    """Spending money needs a clear yes. 'yes please' launches; 'yes but not now', 'maybe', '' don't."""
    text = re.sub(r"[^\w\s']", " ", normalize(str(answer or "")).lower()).strip()
    return bool(_YES.match(text)) and not _HEDGE.search(text)


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
        sig = creative_sig(v["id"])   # signed link: only someone given the URL can open it
        return f"Creative ready: /creatives/{v['id']}.svg?size={size}&theme={theme}&sig={sig}"
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
        c = ads.check_launchable(uid, campaign_id)   # fail BEFORE asking: never ask to approve the impossible
        total = c["daily_budget"] * c["days"]
        # Code enforces the approval: the run pauses here until the API resumes it with the owner's answer
        answer = interrupt({"action": "launch_campaign", "args": {"campaign_id": campaign_id},
                            "question": f"Launch '{c['name']}' on {c['platform']}? It will spend up to "
                                        f"${c['daily_budget']:.0f}/day for {c['days']} days (${total:,.0f} total). "
                                        f"Reply yes to confirm."})
        if not is_approval(answer):
            return "Launch cancelled by the owner. Nothing was spent."
        ads.launch(uid, campaign_id)
        return f"Campaign {campaign_id} is now live (simulated: no real ad account is connected)."
    return _safe(run)


# ── handoffs between specialists ──
# A handoff ends this agent's run and records next_agent; app/graph.py routes it back to the router.
# The router enforces the handoff limit, so two agents can't bounce a task back and forth forever.

def _handoff(target: str, role_description: str):
    @tool(f"transfer_to_{target}", description=(
        f"Hand this chat to the {target} ({role_description}). Call it ALONE, not alongside other tools. "
        "task: one line saying what they should do."))
    def handoff(task: str, runtime: ToolRuntime[AdContext]) -> Command:
        done = ToolMessage(f"Handed to the {target}.", tool_call_id=runtime.tool_call_id)
        # No goto: the parent graph's conditional edge sees next_agent and routes to the router.
        # (A goto here would ALSO fire the static edge to finalize, running both branches at once.)
        return Command(graph=Command.PARENT,
                       update={"messages": [*runtime.state["messages"], done], "next_agent": target,
                               "handoff_task": task})
    return handoff


to_strategist = _handoff("strategist", "creates, estimates and launches campaigns")
to_copywriter = _handoff("copywriter", "writes and saves ad copy variants, makes creatives")
to_analyst = _handoff("analyst", "records results and runs A/B analysis")


STRATEGIST_TOOLS = [create_campaign, list_campaigns, estimate_budget, launch_campaign, remember,
                    to_copywriter, to_analyst]
COPYWRITER_TOOLS = [list_campaigns, list_ad_variants, check_ad_copy, save_ad_variants, make_creative, remember,
                    to_strategist, to_analyst]
ANALYST_TOOLS = [list_campaigns, list_ad_variants, record_results, analyze_ab_test, remember,
                 to_copywriter, to_strategist]
ALL_TOOLS = [create_campaign, list_campaigns, estimate_budget, launch_campaign, check_ad_copy, save_ad_variants,
             list_ad_variants, make_creative, record_results, analyze_ab_test, remember]
ROLE_TOOLS = {"strategist": STRATEGIST_TOOLS, "copywriter": COPYWRITER_TOOLS, "analyst": ANALYST_TOOLS}
