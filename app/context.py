"""Who is talking. Set by the API from the request, never by the model."""
from dataclasses import dataclass

MODEL = "anthropic:claude-haiku-5-5"   # workshop rule: Haiku only, $2 spend limit


@dataclass
class AdContext:
    user_id: str = "demo-user"   # the business owner; every tool scopes its data to this


def thread_key(user_id: str, thread_id: str) -> str:
    """Checkpointer key. Includes user_id so two users sending the same thread_id never share a chat."""
    return f"{user_id}::{thread_id}"
