"""The DoubleAAds agent graph.

    START -> guard -> trim -> router -> strategist | copywriter | analyst -> finalize -> END
               |                 ^                    |
               +-> END (blocked) +---- handoff -------+

guard      Zero-token safety: redacts card numbers/API keys, blocks injection, off-topic and oversized messages.
trim       Short-term memory upkeep: when a chat passes TRIM_AT messages, older turns become a short summary.
router     Picks the specialist in code (no model call): keyword intent, else the specialist already
           handling this chat. Also receives handoffs and caps them at MAX_HANDOFFS per turn.
specialists  Three LangChain agents with distinct prompts and tools (see app/tools.py ROLE_TOOLS).
finalize   Long-term memory capture from this turn's tool calls, and the prompt-leak output guard.
"""
import re
from typing import Annotated, Literal, NotRequired, TypedDict

from langchain.agents import AgentState, create_agent
from langchain.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from langgraph.types import Command

from app.context import AdContext
from app.db import log_event
from app.memory import capture_from_tool_calls, get_checkpointer, get_store
from app.middleware import INJECTION_REPLY, REPLIES, build_middleware, classify, leaks_prompt, redact
from app.tools import ROLE_TOOLS

AGENTS = ("strategist", "copywriter", "analyst")
MAX_HANDOFFS = 2
TRIM_AT, KEEP_LAST = 30, 12


class AdState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    summary: NotRequired[str]          # summary of trimmed older turns
    active_agent: NotRequired[str]     # who handled the last turn: follow-ups ("yes", "shorter") go back to them
    next_agent: NotRequired[str | None]  # set by a handoff tool
    handoff_task: NotRequired[str | None]  # what the previous specialist asked the next one to do
    handoffs: NotRequired[int]         # handoffs so far this turn
    turn_id: NotRequired[str]          # id of this turn's user message


class SpecialistState(AgentState):
    summary: NotRequired[str]
    handoff_task: NotRequired[str | None]


# ── intent routing: plain code, zero tokens ──

INTENTS = {
    "analyst": re.compile(r"\b(impressions?|clicks?|conversions?|ctr|results?|performance|perform\w*|analy[sz]\w*|"
                          r"a/?b test|which (ad|variant|one) (won|is better|did better)|winner|roas|cpa)\b", re.I),
    "strategist": re.compile(r"\b(campaigns?|budget|launch\w*|go live|estimate|reach|platform|plan|"
                             r"per day|/day|daily|days|week|month)\b|\$\s?\d", re.I),
    "copywriter": re.compile(r"\b(write|rewrite|copy|headlines?|captions?|variants?|creatives?|banners?|images?|"
                             r"taglines?|slogans?|tone|wording|shorter|longer|cta)\b", re.I),
}


def route_intent(text: str, active: str | None) -> str:
    """Results numbers go to the analyst; planning words to the strategist; copy words to the copywriter;
    anything else continues with whoever handled the chat last (default: strategist)."""
    for agent in ("analyst", "strategist", "copywriter"):
        if INTENTS[agent].search(text):
            return agent
    return active or "strategist"


# ── helpers ──

def _turn_messages(state: AdState) -> list[AnyMessage]:
    msgs, turn_id = state["messages"], state.get("turn_id")
    idx = next((i for i, m in enumerate(msgs) if m.id == turn_id), None)
    return msgs[idx + 1:] if idx is not None else []


def _repair_dangling_tool_calls(messages: list[AnyMessage]) -> list[ToolMessage]:
    """Every tool call needs a result or the next model call fails. Patch any that were cut short
    (a parallel call next to a handoff, a call limit hit mid-turn)."""
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    return [ToolMessage("Skipped.", tool_call_id=c["id"])
            for m in messages if isinstance(m, AIMessage) for c in m.tool_calls if c["id"] not in answered]


# ── nodes ──

def guard(state: AdState, runtime: Runtime[AdContext]) -> Command[Literal["trim", "__end__"]]:
    last = next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)
    if last is None:
        return Command(goto=END)
    update = {"turn_id": last.id, "handoffs": 0, "next_agent": None, "handoff_task": None}
    clean = redact(last.text)
    if clean != last.text:   # same id: replaces the stored message, so the card number is never saved
        update["messages"] = [HumanMessage(clean, id=last.id)]
    verdict = classify(clean)
    if verdict:
        log_event(runtime.context.user_id, f"blocked_{verdict}", clean)
        update["messages"] = update.get("messages", []) + [AIMessage(REPLIES[verdict])]
        return Command(goto=END, update=update)
    return Command(goto="trim", update=update)


def make_trim(model):
    def trim(state: AdState) -> dict | None:
        msgs = state["messages"]
        if len(msgs) <= TRIM_AT:
            return None
        # Cut at a user message so a tool call is never separated from its result
        cut = next((i for i in range(len(msgs) - KEEP_LAST, 0, -1) if isinstance(msgs[i], HumanMessage)), None)
        if not cut:
            return None
        old = "\n".join(f"{m.type}: {m.text[:300]}" for m in msgs[:cut] if m.text)
        previous = state.get("summary", "")
        reply = model.invoke([HumanMessage(
            "Summarize this ad-agency chat in under 120 words. Keep campaign ids, variant ids, budgets, "
            f"decisions and open questions.\n\nEarlier summary:\n{previous or '(none)'}\n\nMessages:\n{old}")])
        return {"summary": reply.text, "messages": [RemoveMessage(id=m.id) for m in msgs[:cut]]}
    return trim


def router(state: AdState) -> Command[Literal["strategist", "copywriter", "analyst", "finalize"]]:
    target = state.get("next_agent")
    if target:
        handoffs = state.get("handoffs", 0) + 1
        if handoffs > MAX_HANDOFFS:
            msg = AIMessage("I've passed this between my specialists a few times without finishing. "
                            "Could you tell me exactly what you'd like next?")
            return Command(goto="finalize", update={"messages": [msg], "next_agent": None})
        update = {"handoffs": handoffs}
    else:
        last = next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)
        target = route_intent(last.text if last else "", state.get("active_agent"))
        update = {}
    patches = _repair_dangling_tool_calls(state["messages"])
    if patches:
        update["messages"] = patches
    return Command(goto=target, update={**update, "active_agent": target, "next_agent": None})


def finalize(state: AdState, runtime: Runtime[AdContext]) -> dict | None:
    turn = _turn_messages(state)
    update: dict = {}
    # Long-term memory: learn lasting facts from this turn's successful tool calls (no model call)
    if runtime.store is not None:
        calls = [c for m in turn if isinstance(m, AIMessage) for c in m.tool_calls]
        results = {m.tool_call_id: m.text for m in turn if isinstance(m, ToolMessage)}
        capture_from_tool_calls(runtime.store, runtime.context.user_id, calls, results)
    patches = _repair_dangling_tool_calls(state["messages"])
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and leaks_prompt(last.text or ""):
        log_event(runtime.context.user_id, "blocked_prompt_leak", last.text)
        update["messages"] = [AIMessage(INJECTION_REPLY, id=last.id)]
    elif patches or not isinstance(last, AIMessage) or not last.text.strip():
        # Never end a turn on silence or an unanswered tool call
        update["messages"] = patches + [AIMessage("Done. Anything else you'd like me to do?")]
    return update or None


WANTS_ADS = re.compile(r"\b(ads?|adverts?|advertisements?|variants?|creatives?|banners?|headlines?|ad copy)\b", re.I)
CREATED = re.compile(r"Created draft campaign (cmp_[0-9a-f]+)")


def _campaign_needing_ads(state: AdState) -> str | None:
    """Benchmarked bug: asked for "a campaign AND ads", the strategist sometimes created the campaign and stopped
    without handing off. If the owner asked for ads, a campaign was created this turn and no ads were saved,
    return that campaign's id so the graph hands it to the copywriter itself."""
    if state.get("handoffs", 0) >= MAX_HANDOFFS:
        return None
    human = next((m for m in state["messages"] if m.id == state.get("turn_id")), None)
    if human is None or not WANTS_ADS.search(human.text):
        return None
    turn = _turn_messages(state)
    if any(c["name"] == "save_ad_variants" for m in turn if isinstance(m, AIMessage) for c in m.tool_calls):
        return None
    created = [CREATED.search(m.text) for m in turn if isinstance(m, ToolMessage)]
    return next((m.group(1) for m in reversed(created) if m), None)


def after_specialist(state: AdState) -> str:
    """A handoff tool set next_agent: back to the router. A campaign that still needs the ads the owner asked
    for: hand it to the copywriter (in code, not left to the model). Otherwise the turn is done."""
    if state.get("next_agent"):
        return "router"
    if state.get("active_agent") == "strategist" and _campaign_needing_ads(state):
        return "auto_handoff"
    return "finalize"


def auto_handoff(state: AdState) -> Command[Literal["router"]]:
    cid = _campaign_needing_ads(state)
    return Command(goto="router", update={
        "next_agent": "copywriter",
        "handoff_task": f"The owner asked for ads. Write 2 ad variants with different angles for campaign {cid} "
                        "and save them with save_ad_variants, then show them."})


# ── build ──

def get_model():
    from langchain_anthropic import ChatAnthropic
    from app.context import MODEL_ID
    # effort=low: fewer thinking tokens; these tasks are short and tool-driven. max_tokens caps every reply.
    import os
    # Strip stray whitespace/quotes: a pasted key with a trailing newline breaks the HTTP header,
    # which the SDK reports as a confusing "Connection error".
    key = os.getenv("ANTHROPIC_API_KEY", "").strip().strip('"').strip("'")
    return ChatAnthropic(model=MODEL_ID, api_key=key or None, max_tokens=1024, output_config={"effort": "low"},
                         max_retries=2, default_request_timeout=45)


def build_graph(model=None, checkpointer=None, store=None, prompt_caching: bool = True):
    model = model or get_model()
    g = StateGraph(AdState, context_schema=AdContext)
    g.add_node("guard", guard)
    g.add_node("trim", make_trim(model))
    g.add_node("router", router)
    for role in AGENTS:
        agent = create_agent(model, tools=ROLE_TOOLS[role], middleware=build_middleware(role, prompt_caching),
                             state_schema=SpecialistState, context_schema=AdContext, name=role)
        g.add_node(role, agent)
        g.add_conditional_edges(role, after_specialist, ["router", "auto_handoff", "finalize"])
    g.add_node("auto_handoff", auto_handoff)
    g.add_node("finalize", finalize)
    g.add_edge(START, "guard")
    g.add_edge("trim", "router")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer if checkpointer is not None else get_checkpointer(),
                     store=store if store is not None else get_store())
