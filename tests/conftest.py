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


# ── shared helpers: a scripted fake model, so agent tests cost nothing ──
from langchain.agents import create_agent  # noqa: E402
from langchain.messages import AIMessage  # noqa: E402
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from app.context import AdContext  # noqa: E402
from app.middleware import build_middleware  # noqa: E402
from app.tools import ALL_TOOLS  # noqa: E402


class ScriptedModel(GenericFakeChatModel):
    """Replies with the given messages in order; counts calls so tests can prove the model was skipped."""
    calls: int = 0

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, *args, **kwargs):
        type(self).calls += 1
        return super()._generate(*args, **kwargs)


def make_agent(*replies, middleware=None):
    ScriptedModel.calls = 0
    model = ScriptedModel(messages=iter(replies))
    return create_agent(model, tools=ALL_TOOLS, middleware=build_middleware() if middleware is None else middleware,
                        context_schema=AdContext, checkpointer=InMemorySaver())


def chat(agent, text, user="mw-alice", thread="t1"):
    return agent.invoke({"messages": [{"role": "user", "content": text}]},
                        {"configurable": {"thread_id": thread}}, context=AdContext(user))


def call(name, args, cid=None):
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": cid or f"c-{name}"}])
