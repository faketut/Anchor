"""Splunk client: SPL execution + KV Store CRUD.

Thin wrapper over splunk-sdk. Centralizes connection setup so the rest of the
codebase never imports splunk-sdk directly.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from typing import Any

import splunklib.client as splunk_client
import splunklib.results as splunk_results

from .config import CONFIG

# ---- Connection ------------------------------------------------------------

_svc: splunk_client.Service | None = None


def connect() -> splunk_client.Service:
    """Return an authenticated Splunk Service handle (cached per process).

    Re-authenticating on every KV operation is wasteful and trips Splunk's
    session-creation rate limits over slow links. The session is reused for
    the life of the process; tests can call :func:`reset_connection` to
    force a fresh handle.
    """
    global _svc
    if _svc is None:
        _svc = splunk_client.connect(
            host=CONFIG.splunk_host,
            port=CONFIG.splunk_port,
            username=CONFIG.splunk_username,
            password=CONFIG.splunk_password,
            scheme=CONFIG.splunk_scheme,
            verify=CONFIG.splunk_verify_ssl,
            app=CONFIG.anchor_app,
            owner=CONFIG.anchor_owner,
        )
    return _svc


def reset_connection() -> None:
    """Drop the cached Splunk session. Mainly for tests / config reloads."""
    global _svc
    _svc = None


# ---- SPL search ------------------------------------------------------------


def run_search(
    spl: str,
    earliest: str | datetime,
    latest: str | datetime,
    *,
    timeout: int = 120,
    max_count: int = 50_000,
) -> list[dict[str, Any]]:
    """Run a one-shot SPL search and return rows as dicts.

    `earliest`/`latest` may be ISO strings, datetime, or Splunk relative time
    (e.g. "-24h@h", "now").
    """
    svc = connect()
    if isinstance(earliest, datetime):
        earliest = earliest.isoformat()
    if isinstance(latest, datetime):
        latest = latest.isoformat()

    kwargs = {
        "earliest_time": earliest,
        "latest_time": latest,
        "exec_mode": "blocking",
        "count": max_count,
    }
    # Ensure SPL starts with `search` or `|`
    body = spl.strip()
    if not body.startswith("|") and not body.lower().startswith("search "):
        body = f"search {body}"

    job = svc.jobs.create(body, **kwargs)
    # blocking exec_mode means job is already done, but be defensive
    waited = 0
    while not job.is_done() and waited < timeout:
        time.sleep(0.5)
        waited += 0.5
    if not job.is_done():
        job.cancel()
        raise TimeoutError(f"SPL exceeded {timeout}s: {body[:120]}")

    rows: list[dict[str, Any]] = []
    try:
        reader = splunk_results.JSONResultsReader(
            job.results(output_mode="json", count=max_count)
        )
        for item in reader:
            if isinstance(item, dict):
                rows.append(item)
    finally:
        job.cancel()
    return rows


# ---- KV Store --------------------------------------------------------------


COLLECTIONS = {
    "anchors": {
        "field.name": "string",
        "field.created_at": "string",
        "field.version": "number",
    },
    "drift_history": {
        "field.anchor_id": "string",
        "field.timestamp": "string",
        "field.outcome": "string",
    },
    "signal_weights": {
        "field.signal_name": "string",
        "field.weight": "number",
        "field.last_updated": "string",
        "field.last_used_at": "string",
        "field.total_appearances": "number",
        "field.confirmed_count": "number",
        "field.false_positive_count": "number",
    },
}


def ensure_collections() -> None:
    """Create KV Store collections if missing. Idempotent."""
    svc = connect()
    existing = {c.name for c in svc.kvstore}
    for name, fields in COLLECTIONS.items():
        if name in existing:
            continue
        svc.kvstore.create(name, fields=fields)


def _collection(name: str):
    svc = connect()
    return svc.kvstore[name].data


def kv_insert(collection: str, doc: dict) -> str:
    """Insert and return _key."""
    result = _collection(collection).insert(json.dumps(doc, default=str))
    # SDK returns {"_key": "..."}
    if isinstance(result, dict) and result.get("_key"):
        return result["_key"]
    print(
        f"anchor: kv_insert into {collection!r} returned no _key (got {result!r})",
        file=sys.stderr,
    )
    return ""


def kv_update(collection: str, key: str, doc: dict) -> None:
    _collection(collection).update(key, json.dumps(doc, default=str))


def kv_get(collection: str, key: str) -> dict | None:
    try:
        return _collection(collection).query_by_id(key)
    except Exception:
        return None


def kv_query(collection: str, query: dict | None = None) -> list[dict]:
    params: dict[str, Any] = {}
    if query is not None:
        params["query"] = json.dumps(query)
    return list(_collection(collection).query(**params))


def kv_delete(collection: str, key: str) -> None:
    _collection(collection).delete_by_id(key)


# Convenience: iterate, useful for filters that KV query syntax doesn't support
def kv_all(collection: str) -> list[dict]:
    return list(_collection(collection).query())
