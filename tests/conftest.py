"""Test setup. Runs before any app module is imported.

Default: a fresh SQLite file per run (fast, offline, keeps test rows out of the real database).
Set TEST_PG=1 to run the same tests against DATABASE_URL (Neon) instead.
"""
import os
import tempfile
from pathlib import Path

os.environ["LANGSMITH_TRACING"] = "false"   # fake-model test runs would flood LangSmith with junk traces
os.environ["LANGCHAIN_TRACING_V2"] = "false"

if os.getenv("TEST_PG") != "1":
    os.environ["DATABASE_URL"] = ""   # load_dotenv never overrides an existing variable, even an empty one
    os.environ["DOUBLEAADS_SQLITE"] = str(Path(tempfile.mkdtemp()) / "test.db")


# ── shared helpers: the real graph with a scripted fake model, so agent tests cost nothing ──
from langchain.messages import AIMessage  # noqa: E402
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.store.memory import InMemoryStore  # noqa: E402

from app.context import AdContext  # noqa: E402
from app.graph import build_graph  # noqa: E402


class ScriptedModel(GenericFakeChatModel):
    """Replies with the given messages in order (shared by all specialists); counts calls."""
    calls: int = 0
    inputs: list = []   # the messages each call received (system prompt first), to check memory

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, *args, **kwargs):
        # Same rule as the real Claude API: a request must not end on an assistant message (no prefill)
        assert not isinstance(messages[-1], AIMessage), "model called with history ending in an AI message"
        type(self).calls += 1
        type(self).inputs.append(messages)
        return super()._generate(messages, *args, **kwargs)


def make_agent(*replies, store=None):
    """The full DoubleAAds graph (guard, trim, router, 3 specialists, finalize) on a scripted model."""
    ScriptedModel.calls = 0
    ScriptedModel.inputs = []
    model = ScriptedModel(messages=iter(replies))
    return build_graph(model=model, checkpointer=InMemorySaver(), store=store or InMemoryStore(),
                       prompt_caching=False)


def chat(agent, text, user="mw-alice", thread="t1"):
    return agent.invoke({"messages": [{"role": "user", "content": text}]},
                        {"configurable": {"thread_id": thread}}, context=AdContext(user))


def call(name, args, cid=None):
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": cid or f"c-{name}"}])
