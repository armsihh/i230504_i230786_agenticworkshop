"""Graph behaviour: routing, handoffs, short-term and long-term memory, trimming. Scripted model, no cost."""
import pytest
from langchain.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.store.memory import InMemoryStore

from app import ads
from app.context import AdContext
from app.graph import MAX_HANDOFFS, TRIM_AT, route_intent
from app.memory import list_facts
from conftest import ScriptedModel, call, chat, make_agent

GOOD = {"headline": "Fresh bread daily", "body": "Baked every morning in F-7.", "cta": "Order now"}


def system_prompt(i=-1) -> str:
    msgs = ScriptedModel.inputs[i]
    return msgs[0].text if isinstance(msgs[0], SystemMessage) else ""


def state(agent, thread):
    return agent.get_state({"configurable": {"thread_id": thread}}).values


# ── routing ──

@pytest.mark.parametrize("text, expected", [
    ("Variant A got 5000 impressions and 120 clicks", "analyst"),
    ("Which ad won?", "analyst"),
    ("Plan a campaign for my bakery, $20/day", "strategist"),
    ("Launch it", "strategist"),
    ("Write two headlines for my cafe", "copywriter"),
    ("Make the copy shorter", "copywriter"),
])
def test_route_intent(text, expected):
    assert route_intent(text, None) == expected


def test_follow_up_goes_back_to_the_same_specialist():
    agent = make_agent(AIMessage("Here are two headlines: ..."), AIMessage("Great, saving them."))
    chat(agent, "Write two headlines for my cafe", thread="sticky")
    assert state(agent, "sticky")["active_agent"] == "copywriter"
    chat(agent, "yes", thread="sticky")                       # no keywords: stays with the copywriter
    assert state(agent, "sticky")["active_agent"] == "copywriter"


# ── handoffs ──

def test_strategist_hands_off_to_copywriter_and_both_do_their_part():
    camp_args = dict(name="Bread", product="Sourdough bread", audience="Families in F-7", platform="instagram",
                     goal="sales", daily_budget=20, days=7)
    agent = make_agent(
        call("create_campaign", camp_args),                                       # strategist
        call("transfer_to_copywriter", {"task": "write 2 variants"}, cid="h1"),   # strategist hands off
        call("list_campaigns", {}, cid="lc"),                                     # copywriter
        AIMessage("Campaign ready, here are your ads: ..."),                      # copywriter
    )
    out = chat(agent, "Make Instagram ads for my bakery, $20/day for a week", user="g-bakery", thread="handoff")
    assert out["messages"][-1].text == "Campaign ready, here are your ads: ..."
    assert state(agent, "handoff")["active_agent"] == "copywriter"
    assert ads.list_campaigns("g-bakery")
    names = [c["name"] for m in out["messages"] if isinstance(m, AIMessage) for c in m.tool_calls]
    assert names == ["create_campaign", "transfer_to_copywriter", "list_campaigns"]


def test_handoff_ping_pong_is_capped():
    bounce = [call("transfer_to_copywriter", {"task": "x"}, cid="b0")]
    for i in range(1, 8):
        target = "transfer_to_strategist" if i % 2 else "transfer_to_copywriter"
        bounce.append(call(target, {"task": "x"}, cid=f"b{i}"))
    agent = make_agent(*bounce)
    out = chat(agent, "plan a campaign", thread="pingpong")
    assert ScriptedModel.calls == MAX_HANDOFFS + 1
    assert "passed this between my specialists" in out["messages"][-1].text


def test_parallel_call_next_to_handoff_is_repaired():
    both = AIMessage("", tool_calls=[
        {"name": "remember", "args": {"fact": "Brand name is Crumbs"}, "id": "r1"},
        {"name": "transfer_to_copywriter", "args": {"task": "write ads"}, "id": "t1"}])
    agent = make_agent(both, AIMessage("Here are your ads."))
    out = chat(agent, "plan a campaign for Crumbs", thread="parallel")
    calls = {c["id"] for m in out["messages"] if isinstance(m, AIMessage) for c in m.tool_calls}
    results = {m.tool_call_id for m in out["messages"] if isinstance(m, ToolMessage)}
    assert calls <= results, "every tool call must have a result or the next model call fails"


# ── short-term memory (per thread) ──

def test_same_thread_remembers_earlier_messages():
    agent = make_agent(AIMessage("Nice to meet you."), AIMessage("You sell candles."))
    chat(agent, "I sell handmade candles", thread="stm")
    chat(agent, "What do I sell?", thread="stm")
    assert "handmade candles" in " ".join(m.text for m in ScriptedModel.inputs[-1])


def test_new_thread_starts_fresh():
    agent = make_agent(AIMessage("Nice to meet you."), AIMessage("I don't know yet."))
    chat(agent, "I sell handmade candles", thread="stm-a")
    chat(agent, "What do I sell?", thread="stm-b")
    assert "handmade candles" not in " ".join(m.text for m in ScriptedModel.inputs[-1][1:])


def test_long_chat_is_trimmed_to_a_summary():
    turns = TRIM_AT // 2          # after these, the next message pushes the chat past TRIM_AT
    replies = [AIMessage(f"reply {i}") for i in range(turns)] + [AIMessage("Summary: sells candles, cmp_x.")] + \
              [AIMessage("final")]
    agent = make_agent(*replies)
    for i in range(turns):
        chat(agent, f"message {i}", thread="long")
    chat(agent, "and one more", thread="long")
    s = state(agent, "long")
    assert s["summary"].startswith("Summary:")
    assert len(s["messages"]) < TRIM_AT
    assert isinstance(s["messages"][0], HumanMessage), "cut must land on a user message"
    assert "Summary: sells candles" in system_prompt()


# ── long-term memory (per user, across threads) ──

def test_campaign_facts_carry_over_to_a_new_chat():
    store = InMemoryStore()
    args = dict(name="Glow", product="Soy candles", audience="Women 25-40", platform="instagram",
                goal="sales", daily_budget=15, days=10)
    agent = make_agent(call("create_campaign", args), AIMessage("Created."), AIMessage("Welcome back!"), store=store)
    chat(agent, "create a campaign for my candles", user="ltm-user", thread="day1")
    assert "Sells: Soy candles" in list_facts(store, "ltm-user")
    chat(agent, "hi again", user="ltm-user", thread="day2")          # brand new thread
    assert "Sells: Soy candles" in system_prompt() and "Women 25-40" in system_prompt()


def test_remember_tool_and_memory_isolation():
    store = InMemoryStore()
    agent = make_agent(call("remember", {"fact": "Brand voice is playful, never formal"}), AIMessage("Noted!"),
                       AIMessage("Hello!"), store=store)
    chat(agent, "our brand voice is playful", user="ltm-a", thread="a1")
    assert "Brand voice is playful, never formal" in list_facts(store, "ltm-a")
    chat(agent, "hello", user="ltm-b", thread="b1")
    assert "playful" not in system_prompt(), "user B must never see user A's memory"


def test_remember_refuses_sensitive_facts():
    store = InMemoryStore()
    agent = make_agent(call("remember", {"fact": "Our card is 4242424242424242 and password hunter2"}),
                       AIMessage("ok"), store=store)
    chat(agent, "remember our payment details", user="ltm-s", thread="s1")
    assert list_facts(store, "ltm-s") == []


def test_failed_tool_calls_are_not_remembered():
    store = InMemoryStore()
    bad = dict(name="X", product="Fake product", audience="a", platform="myspace", goal="sales",
               daily_budget=10, days=3)
    agent = make_agent(call("create_campaign", bad), AIMessage("That platform isn't supported."), store=store)
    chat(agent, "create a campaign on myspace", user="ltm-f", thread="f1")
    assert list_facts(store, "ltm-f") == []


# ── guard at graph level ──

def test_card_number_never_stored_in_chat_history():
    agent = make_agent(AIMessage("Thanks, let's plan your ad."))
    chat(agent, "My card is 4242 4242 4242 4242, plan a campaign for my shop", thread="pii-g")
    stored = " ".join(m.text for m in state(agent, "pii-g")["messages"])
    assert "4242 4242" not in stored and "[REDACTED_CARD]" in stored
    assert "4242 4242" not in " ".join(m.text for m in ScriptedModel.inputs[-1])


def test_order_numbers_are_not_redacted():
    agent = make_agent(AIMessage("ok"))
    chat(agent, "Order 1234567890123 came from my ad campaign", thread="pii-ok")
    assert "1234567890123" in state(agent, "pii-ok")["messages"][0].text   # fails the Luhn check: not a card


def test_empty_reply_never_reaches_the_user():
    agent = make_agent(AIMessage(""))
    out = chat(agent, "plan a campaign", thread="empty")
    assert out["messages"][-1].text.strip()


def test_resume_after_restart_uses_saved_state():
    """Short-term memory lives in the checkpointer: a new graph object on the same saver continues the chat."""
    from langgraph.checkpoint.memory import InMemorySaver
    from app.graph import build_graph
    saver = InMemorySaver()
    ScriptedModel.inputs = []
    m1 = ScriptedModel(messages=iter([AIMessage("Noted.")]))
    build_graph(model=m1, checkpointer=saver, store=InMemoryStore(), prompt_caching=False).invoke(
        {"messages": [{"role": "user", "content": "I sell kites"}]}, {"configurable": {"thread_id": "r"}},
        context=AdContext("restart"))
    m2 = ScriptedModel(messages=iter([AIMessage("Kites!")]))
    build_graph(model=m2, checkpointer=saver, store=InMemoryStore(), prompt_caching=False).invoke(
        {"messages": [{"role": "user", "content": "what do I sell?"}]}, {"configurable": {"thread_id": "r"}},
        context=AdContext("restart"))
    assert "I sell kites" in " ".join(m.text for m in ScriptedModel.inputs[-1])


def test_receiving_specialist_is_told_its_task():
    """Live bug: the copywriter read the handoff in the history as its own action and only summarised."""
    agent = make_agent(call("transfer_to_copywriter", {"task": "write 2 variants for cmp_x"}, cid="h"),
                       call("list_campaigns", {}, cid="lc"), AIMessage("Here are 2 variants."))
    chat(agent, "plan a campaign and write ads", thread="task")
    first_call, second_call = ScriptedModel.inputs[1], ScriptedModel.inputs[2]
    note = first_call[-1]
    assert isinstance(note, HumanMessage) and "You are the copywriter now" in note.text
    assert "write 2 variants for cmp_x" in note.text
    assert not any("Note from your colleague" in m.text for m in second_call), "note only on the first call"
    stored = state(agent, "task")["messages"]
    assert not any("Note from your colleague" in m.text for m in stored), "note is never saved to the chat"
    assert "Note from your colleague" not in ScriptedModel.inputs[0][-1].text   # first specialist gets none


def test_handoff_task_cleared_on_next_turn():
    agent = make_agent(call("transfer_to_copywriter", {"task": "write ads"}, cid="h"), AIMessage("Saved."),
                       AIMessage("Anything else?"))
    chat(agent, "plan a campaign", thread="clear")
    chat(agent, "thanks", thread="clear")
    assert "handed to you by a colleague" not in system_prompt(-1)
    assert "Note from your colleague" not in ScriptedModel.inputs[-1][-1].text


def test_launch_without_variants_fails_before_asking_for_money():
    c = ads.create_campaign("g-empty", "Empty", "Kites", "Kids", "meta", "sales", 10, 3)
    agent = make_agent(call("launch_campaign", {"campaign_id": c["id"]}), AIMessage("Add ads first."))
    out = chat(agent, "launch it", user="g-empty", thread="empty-launch")
    assert "__interrupt__" not in out
    assert "at least one ad variant" in next(m for m in out["messages"] if isinstance(m, ToolMessage)).text
