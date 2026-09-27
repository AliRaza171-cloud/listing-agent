"""Redis Streams event bus.

Publishing:   await bus.publish("listing.requested", {...})
Consuming:    bus.subscribe("listing.generated", handler); await bus.run(stop)

Delivery guarantees (contracts/README.md §3.2):
- one consumer group per service -> every interested service gets each event;
  several copies of the same service share the work
- at-least-once: a handler that raises leaves the message pending; after
  `claim_idle_ms` it is re-claimed and retried; after `max_deliveries` it goes
  to `events:dead-letter` and is acknowledged so the stream keeps moving
- handlers must be idempotent (see lagent_common.db.mark_processed)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from typing import Awaitable, Callable

import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from lagent_common.correlation import correlation_id_var
from lagent_common.events import make_envelope

log = logging.getLogger("lagent.bus")

Handler = Callable[[dict], Awaitable[None]]
DEAD_LETTER_STREAM = "events:dead-letter"


def stream_key(event_type: str) -> str:
    return f"events:{event_type}"


class EventBus:
    def __init__(
        self,
        redis_url: str,
        service: str,
        *,
        consumer: str | None = None,
        maxlen: int = 100_000,
        max_deliveries: int = 5,
        claim_idle_ms: int = 60_000,
        block_ms: int = 5_000,
    ):
        # The socket must wait LONGER than XREADGROUP's BLOCK, or the client gives up
        # mid-wait with "Timeout reading from redis". Set explicitly so it doesn't depend
        # on the redis-py version's defaults.
        self.redis = redis.from_url(
            redis_url,
            decode_responses=True,
            socket_timeout=block_ms / 1000 + 10,
            socket_connect_timeout=5,
            health_check_interval=30,
        )
        self.service = service  # also the consumer-group name
        self.consumer = consumer or f"{service}-{socket.gethostname()}-{os.getpid()}"
        self.maxlen = maxlen
        self.max_deliveries = max_deliveries
        self.claim_idle_ms = claim_idle_ms
        self.block_ms = block_ms
        self._handlers: dict[str, Handler] = {}

    # ---------------- publishing ----------------

    async def publish(self, event_type: str, data: dict, correlation_id: str | None = None) -> str:
        envelope = make_envelope(event_type, data, producer=self.service, correlation_id=correlation_id)
        await self.redis.xadd(
            stream_key(event_type), {"event": json.dumps(envelope)}, maxlen=self.maxlen, approximate=True
        )
        log.info("published %s id=%s corr=%s", event_type, envelope["id"], envelope["correlation_id"])
        return envelope["id"]

    # ---------------- consuming ----------------

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._handlers[stream_key(event_type)] = handler

    async def _ensure_groups(self) -> None:
        for key in self._handlers:
            try:
                # "$" = only events published after the group is first created, so a
                # newly deployed service doesn't replay (e.g. re-email) old history.
                await self.redis.xgroup_create(key, self.service, id="$", mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

    async def _handle(self, key: str, msg_id: str, fields: dict) -> None:
        handler = self._handlers[key]
        try:
            envelope = json.loads(fields["event"])
        except (KeyError, json.JSONDecodeError) as exc:
            await self._dead_letter(key, msg_id, fields, f"unreadable event: {exc}")
            return
        token = correlation_id_var.set(envelope.get("correlation_id") or envelope.get("id"))
        try:
            await handler(envelope)
        except Exception:  # noqa: BLE001 — any failure means "retry later"
            log.exception("handler failed for %s %s (will retry)", key, msg_id)
            return  # left pending -> re-claimed after claim_idle_ms
        finally:
            correlation_id_var.reset(token)
        await self.redis.xack(key, self.service, msg_id)

    async def _dead_letter(self, key: str, msg_id: str, fields: dict, reason: str) -> None:
        await self.redis.xadd(
            DEAD_LETTER_STREAM,
            {"stream": key, "group": self.service, "message_id": msg_id,
             "event": fields.get("event", ""), "reason": reason, "at": str(int(time.time()))},
            maxlen=self.maxlen, approximate=True,
        )
        await self.redis.xack(key, self.service, msg_id)
        log.error("dead-lettered %s %s: %s", key, msg_id, reason)

    async def _reclaim(self) -> None:
        """Retry messages another consumer (or this one) failed to finish."""
        for key in self._handlers:
            result = await self.redis.xautoclaim(
                key, self.service, self.consumer, min_idle_time=self.claim_idle_ms, start_id="0-0", count=20
            )
            claimed = result[1] if result else []
            for msg_id, fields in claimed:
                if not fields:  # entry was trimmed from the stream
                    await self.redis.xack(key, self.service, msg_id)
                    continue
                info = await self.redis.xpending_range(key, self.service, min=msg_id, max=msg_id, count=1)
                deliveries = info[0]["times_delivered"] if info else 1
                if deliveries > self.max_deliveries:
                    await self._dead_letter(key, msg_id, fields, f"failed {deliveries - 1} times")
                else:
                    await self._handle(key, msg_id, fields)

    async def run(self, stop: asyncio.Event) -> None:
        if not self._handlers:
            return
        await self._ensure_groups()
        streams = {key: ">" for key in self._handlers}
        last_reclaim = 0.0
        log.info("%s consuming %s", self.consumer, ", ".join(self._handlers))
        while not stop.is_set():
            try:
                if time.monotonic() - last_reclaim > self.claim_idle_ms / 2000:
                    await self._reclaim()
                    last_reclaim = time.monotonic()
                batches = await self.redis.xreadgroup(
                    self.service, self.consumer, streams, count=10, block=self.block_ms
                )
                for key, messages in batches or []:
                    for msg_id, fields in messages:
                        await self._handle(key, msg_id, fields)
            except asyncio.CancelledError:
                break
            except (RedisTimeoutError, RedisConnectionError) as exc:
                # Redis restarting / network blip: one line, not a traceback, then retry.
                log.warning("redis unavailable (%s); retrying in 2s", exc)
                await asyncio.sleep(2)
            except Exception:  # noqa: BLE001 — anything unexpected: full traceback, back off, continue
                log.exception("event loop error; retrying in 2s")
                await asyncio.sleep(2)

    async def close(self) -> None:
        await self.redis.aclose()
