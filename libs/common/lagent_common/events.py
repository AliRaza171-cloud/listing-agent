"""Event envelope + contract check against contracts/events.json."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from functools import lru_cache

from lagent_common.correlation import correlation_id_var


class ContractError(ValueError):
    pass


@lru_cache
def _contracts() -> dict:
    path = os.environ.get("EVENTS_CONTRACT_PATH", "/app/contracts/events.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data.pop("$comment", None)
    return data


def make_envelope(event_type: str, data: dict, *, producer: str, correlation_id: str | None = None) -> dict:
    spec = _contracts().get(event_type)
    if spec is None:
        raise ContractError(f"Unknown event type '{event_type}' — add it to contracts/events.json first.")
    missing = [f for f in spec["required"] if f not in data]
    if missing:
        raise ContractError(f"{event_type} is missing required field(s): {', '.join(missing)}")
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "version": spec["version"],
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "correlation_id": correlation_id or correlation_id_var.get() or str(uuid.uuid4()),
        "producer": producer,
        "data": data,
    }
