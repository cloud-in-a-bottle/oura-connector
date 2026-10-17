"""Suppress Oura workouts that another health-data provider also recorded.

The same session often lands in several providers (e.g. a run tracked by both
the ring and an Apple Watch). When the owner turns this on, each sync fetches
workouts from the other providers of the health-data service and marks any of
ours that overlap one in time as suppressed: the row is kept, but the service
API stops returning it. Turning the option off un-hides everything at once.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from health_data_service.client import SERVICE_URL

from . import db

log = logging.getLogger(__name__)

# Must match the [[services.v2.consumes]] shortname in cloudinabottle.toml.
SHORTNAME = "health"

# A regular sync re-checks this much history, since other providers may ingest
# a workout well after it happened (e.g. an Apple Health export arriving late).
LOOKBACK_DAYS = 30

# The spec has no paging, so fetch in windows with a limit no window will hit.
FETCH_WINDOW_DAYS = 365
FETCH_LIMIT = 100_000

# Fetch a little before the range so a workout that starts just before it but
# overlaps one of ours inside it still counts.
FETCH_MARGIN = timedelta(days=1)


async def is_enabled() -> bool:
    return await db.get_config("dedupe_workouts") == "1"


class DedupeError(Exception):
    pass


def _to_ms(ts: str) -> int:
    return int(datetime.fromisoformat(ts).timestamp() * 1000)


async def _other_providers(client: httpx.AsyncClient) -> list[dict]:
    resp = await client.get("/api/services/v2/providers", params={"service": SERVICE_URL})
    resp.raise_for_status()
    # The router returns a bare list; older mocks/clients expect {"providers": [...]}.
    body = resp.json()
    providers = body.get("providers", []) if isinstance(body, dict) else body
    own_id = os.environ.get("OPENHOST_APP_ID")
    return [
        p for p in providers
        if p.get("status") == "running" and p.get("app_id") != own_id
    ]


async def _provider_workouts(
    client: httpx.AsyncClient, provider: dict, start: datetime, end: datetime
) -> list[tuple[int, int, str]]:
    """(start_ms, end_ms, "<app_name>:<workout id>") for each of a provider's workouts."""
    name = provider.get("app_name") or provider["app_id"]
    out = []
    cur = start
    while cur < end:
        chunk_end = min(cur + timedelta(days=FETCH_WINDOW_DAYS), end)
        resp = await client.get(
            f"/api/services/v2/call/{SHORTNAME}/v1/workouts",
            params={"start": cur.isoformat(), "end": chunk_end.isoformat(), "limit": FETCH_LIMIT},
            headers={"X-OpenHost-Provider": provider["app_id"]},
        )
        if resp.status_code == 403:
            raise DedupeError(f"{name} denied access to its workouts. Grant this app access in Cloud in a Bottle settings.")
        resp.raise_for_status()
        for w in resp.json().get("data", []):
            if w.get("start") and w.get("end"):
                out.append((_to_ms(w["start"]), _to_ms(w["end"]), f"{name}:{w.get('id', '')}"))
        cur = chunk_end
    return out


async def refresh(start: datetime, end: datetime) -> None:
    """Recompute suppression for our workouts starting in [start, end].

    Never raises: a failure is logged and stored in `dedupe_error` for the UI,
    and existing suppressions are left as they were, so one unreachable
    provider can't make hidden duplicates reappear.
    """
    if not await is_enabled():
        return
    router = os.environ.get("OPENHOST_ROUTER_URL")
    token = os.environ.get("OPENHOST_APP_TOKEN")
    if not router or not token:
        await db.set_config("dedupe_error", "Can't reach other providers: not running on Cloud in a Bottle.")
        return

    try:
        others: list[tuple[int, int, str]] = []
        async with httpx.AsyncClient(
            base_url=router.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=60
        ) as client:
            for provider in await _other_providers(client):
                others += await _provider_workouts(client, provider, start - FETCH_MARGIN, end)
    except Exception as e:
        msg = str(e) if isinstance(e, DedupeError) else f"Couldn't fetch workouts from other providers: {e}"
        log.warning("Workout dedupe skipped: %s", msg)
        await db.set_config("dedupe_error", msg)
        return

    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    async with db.connect() as conn:
        rows = await (await conn.execute(
            "SELECT id, start_unix, end_unix FROM workouts WHERE start_unix >= ? AND start_unix <= ?",
            (start_ms, end_ms),
        )).fetchall()
        updates = []
        for wid, w_start, w_end in rows:
            match = next((ref for o_start, o_end, ref in others if o_start < w_end and o_end > w_start), None)
            updates.append((match, wid))
        await conn.executemany("UPDATE workouts SET suppressed_by = ? WHERE id = ?", updates)
        await conn.commit()

    suppressed = sum(1 for m, _ in updates if m)
    log.info("Workout dedupe: %d of %d workouts overlap another provider", suppressed, len(updates))
    await db.set_config("dedupe_error", "")


async def refresh_recent() -> None:
    now = datetime.now(timezone.utc)
    await refresh(now - timedelta(days=LOOKBACK_DAYS), now + timedelta(days=1))


async def refresh_all() -> None:
    async with db.connect() as conn:
        row = await (await conn.execute("SELECT MIN(start_unix) FROM workouts")).fetchone()
    if row[0] is None:
        return
    first = datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc)
    await refresh(first, datetime.now(timezone.utc) + timedelta(days=1))
