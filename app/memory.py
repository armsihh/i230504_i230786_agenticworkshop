"""Memory for DoubleAAds.

Short-term (per conversation): LangGraph checkpointer, keyed by thread_key(user_id, thread_id).
    Holds the messages of one chat. Long chats are trimmed by the graph's `trim` node.
Long-term (per business, across chats): LangGraph store, namespace ("brand", user_id).
    Holds lasting facts: what they sell, audience, platforms, tone, winning ad angles.
    Most facts are captured by code from tool calls (free); the `remember` tool covers stated preferences.

Both live in Postgres (DATABASE_URL), so a server restart loses nothing. Without DATABASE_URL
(local dev, tests) they fall back to in-memory versions.
"""
import hashlib
import re

from langchain.tools import ToolRuntime, tool

from app.context import AdContext
from app.db import IS_PG, pg_pool

MAX_FACTS = 25      # per business; oldest dropped first, keeps the prompt small
MAX_FACT_CHARS = 200

_checkpointer = None
_store = None


def get_checkpointer():
    global _checkpointer
    if _checkpointer is None:
        if IS_PG:
            from langgraph.checkpoint.postgres import PostgresSaver
            _checkpointer = PostgresSaver(pg_pool())
            _checkpointer.setup()
        else:
            from langgraph.checkpoint.memory import InMemorySaver
            _checkpointer = InMemorySaver()
    return _checkpointer


def get_store():
    global _store
    if _store is None:
        if IS_PG:
            from langgraph.store.postgres import PostgresStore
            _store = PostgresStore(pg_pool())
            _store.setup()
        else:
            from langgraph.store.memory import InMemoryStore
            _store = InMemoryStore()
    return _store


def namespace(user_id: str) -> tuple[str, str]:
    return ("brand", user_id)


def save_fact(store, user_id: str, fact: str, key: str | None = None) -> str:
    """Save one fact. A stable key overwrites (e.g. 'product'); otherwise the key is a hash of the text."""
    fact = re.sub(r"\s+", " ", fact).strip()[:MAX_FACT_CHARS]
    if not fact:
        return ""
    key = key or "note-" + hashlib.sha1(fact.lower().encode()).hexdigest()[:10]
    store.put(namespace(user_id), key, {"fact": fact})
    items = store.search(namespace(user_id), limit=100)
    if len(items) > MAX_FACTS:   # drop the oldest
        for old in sorted(items, key=lambda i: i.updated_at)[: len(items) - MAX_FACTS]:
            store.delete(namespace(user_id), old.key)
    return key


def list_facts(store, user_id: str) -> list[str]:
    items = sorted(store.search(namespace(user_id), limit=MAX_FACTS), key=lambda i: i.key)
    return [i.value["fact"] for i in items]


def capture_from_tool_calls(store, user_id: str, calls: list[dict], tool_results: dict[str, str]) -> None:
    """Free long-term memory: learn lasting facts from what the agent just did, no extra model call."""
    for c in calls:
        name, args = c["name"], c.get("args", {})
        ok = not tool_results.get(c.get("id"), "").startswith("Error")
        if not ok:
            continue
        if name == "create_campaign":
            if args.get("product"):
                save_fact(store, user_id, f"Sells: {args['product']}", key="product")
            if args.get("audience"):
                save_fact(store, user_id, f"Target audience: {args['audience']}", key="audience")
            if args.get("platform"):
                p = str(args["platform"]).strip().lower()
                save_fact(store, user_id, f"Advertises on: {p}", key=f"platform-{p}")
            if args.get("daily_budget"):
                save_fact(store, user_id, f"Typical daily budget: ${float(args['daily_budget']):.0f}", key="budget")
        elif name == "analyze_ab_test":
            m = re.search(r'"winner": \{"label": "(\w)"', tool_results.get(c.get("id"), ""))
            if m:
                save_fact(store, user_id, f"Last A/B winner was variant {m.group(1)} (campaign {args.get('campaign_id')})",
                          key="last-winner")


@tool
def remember(fact: str, runtime: ToolRuntime[AdContext]) -> str:
    """Save a lasting fact about this business for future chats: brand name, tone of voice, products,
    audience, preferred platforms or things to avoid. Not for one-off details or anything sensitive."""
    if runtime.store is None:
        return "Memory is not available right now."
    if re.search(r"\b(password|card|cvv|api[_ ]?key|token|secret|\d{12,})\b", fact, re.I):
        return "Not saved: that looks sensitive."
    save_fact(runtime.store, runtime.context.user_id, fact)
    return "Saved for next time."
