#!/usr/bin/env python3
"""Back up Anchor's KV Store collections to Alibaba Cloud OSS.

Reads anchors / drift_history / signal_weights from Splunk KV via Anchor's
own client, bundles them into a single timestamped JSON blob, and uploads
to OSS. Designed to run on the same ECS host as Splunk (e.g. as a cron).

This file is intentionally simple — it's also the "proof of Alibaba Cloud
services usage" artifact for the Qwen Cloud hackathon submission.

Usage:
    pip install -e '.[alibaba]'
    export OSS_ACCESS_KEY_ID=...
    export OSS_ACCESS_KEY_SECRET=...
    export OSS_ENDPOINT=oss-ap-southeast-1.aliyuncs.com
    export OSS_BUCKET=anchor-memory-backups
    python deploy/backup_kv_to_oss.py

Add to ECS crontab for daily backups:
    0 3 * * * cd /opt/anchor && /opt/anchor/.venv/bin/python deploy/backup_kv_to_oss.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from anchor.splunk_client import ensure_collections, kv_all

COLLECTIONS = ("anchors", "drift_history", "signal_weights")


def dump_kv() -> dict:
    """Snapshot every Anchor collection. Pure read — never mutates."""
    ensure_collections()
    return {
        "schema_version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "collections": {name: kv_all(name) for name in COLLECTIONS},
    }


def upload_to_oss(payload: dict, *, bucket: str, endpoint: str, ak_id: str, ak_secret: str) -> str:
    """Upload payload as JSON to OSS. Returns the object key.

    Snapshots can contain sample log lines and signal names, so request
    server-side encryption (AES256) on every upload.
    """
    import oss2  # type: ignore[import-not-found]

    auth = oss2.Auth(ak_id, ak_secret)
    oss_bucket = oss2.Bucket(auth, endpoint, bucket)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"anchor-memory/{ts}.json"
    body = json.dumps(payload, default=str, indent=2).encode("utf-8")
    oss_bucket.put_object(
        key,
        body,
        headers={
            "Content-Type": "application/json",
            "x-oss-server-side-encryption": "AES256",
        },
    )
    return key


def main() -> int:
    required = ("OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET", "OSS_ENDPOINT", "OSS_BUCKET")
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        print(f"Missing env vars: {', '.join(missing)}", file=sys.stderr)
        return 2

    try:
        payload = dump_kv()
    except Exception as e:
        print(f"Failed to snapshot KV from Splunk: {e!r}", file=sys.stderr)
        return 3
    counts = {name: len(items) for name, items in payload["collections"].items()}
    print(f"Snapshot: {counts}")

    try:
        key = upload_to_oss(
            payload,
            bucket=os.environ["OSS_BUCKET"],
            endpoint=os.environ["OSS_ENDPOINT"],
            ak_id=os.environ["OSS_ACCESS_KEY_ID"],
            ak_secret=os.environ["OSS_ACCESS_KEY_SECRET"],
        )
    except Exception as e:
        print(f"Failed to upload snapshot to OSS: {e!r}", file=sys.stderr)
        return 4
    print(f"Uploaded oss://{os.environ['OSS_BUCKET']}/{key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
