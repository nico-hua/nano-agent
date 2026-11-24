"""Factories for durable memory test data."""

from nanobot.memory.models import MemoryChanges
from nanobot.providers import HumanMessage


def addition(text, offset=0):
    return {"op": "add", "kind": "decision", "scope": "project", "text": text, "message_offset": offset}


def seed(store, text, *, replace_id=None):
    event = store.append_event("chat", (HumanMessage(text),))
    change = addition(text)
    if replace_id is not None:
        change.update(op="replace", target_id=replace_id)
    store.apply(event, MemoryChanges.model_validate({"operations": [change]}))
