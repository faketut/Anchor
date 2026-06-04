"""Synthetic e-commerce log generator for the Anchor demo.

Writes two log files to ./examples/data/:
  - healthy.log  : 7 days of clean traffic (use as anchor window)
  - drifted.log  : 1 day with a new PaymentGatewayTimeout error template
                   and a latency spike on the checkout service

Ingest via Splunk Web (Add Data → Upload), or via HEC, into index=main.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).parent / "data"
OUT.mkdir(parents=True, exist_ok=True)

SERVICES = ["frontend", "checkout-svc", "payment-svc", "inventory-svc", "auth-svc"]
HOSTS = [f"host-{i:02d}" for i in range(1, 6)]
TEMPLATES_HEALTHY = [
    ('INFO', '{svc} request processed status=200 latency_ms={lat}'),
    ('INFO', '{svc} cache hit key=user:{uid}'),
    ('INFO', '{svc} db query ok rows={rows} latency_ms={lat}'),
    ('WARN', '{svc} retrying upstream attempt={n}'),
    ('INFO', '{svc} healthcheck ok'),
]
TEMPLATES_DRIFT = [
    ('ERROR', '{svc} PaymentGatewayTimeout after {lat}ms gateway=stripe'),
    ('ERROR', '{svc} request failed status=503 latency_ms={lat}'),
]


def _line(ts: datetime, svc: str, host: str, level: str, msg: str) -> str:
    return json.dumps(
        {
            "time": ts.isoformat(),
            "host": host,
            "sourcetype": svc,
            "level": level,
            "message": msg,
            "latency_ms": int(msg.split("latency_ms=")[-1].split()[0]) if "latency_ms=" in msg else None,
        }
    )


def write_healthy(start: datetime, days: int = 7) -> Path:
    path = OUT / "healthy.log"
    rng = random.Random(42)
    with path.open("w") as f:
        for day in range(days):
            base = start + timedelta(days=day)
            # 1000 events/hour, normal latency around 80ms
            for hour in range(24):
                for _ in range(1000):
                    ts = base + timedelta(hours=hour, seconds=rng.randint(0, 3599))
                    svc = rng.choice(SERVICES)
                    host = rng.choice(HOSTS)
                    level, tmpl = rng.choices(TEMPLATES_HEALTHY, weights=[70, 15, 10, 4, 1])[0]
                    msg = tmpl.format(
                        svc=svc, lat=rng.randint(40, 150), uid=rng.randint(1, 999),
                        rows=rng.randint(1, 50), n=rng.randint(1, 3),
                    )
                    f.write(_line(ts, svc, host, level, msg) + "\n")
    return path


def write_drifted(start: datetime, hours: int = 24) -> Path:
    path = OUT / "drifted.log"
    rng = random.Random(7)
    with path.open("w") as f:
        for hour in range(hours):
            base = start + timedelta(hours=hour)
            # Normal traffic
            for _ in range(900):
                ts = base + timedelta(seconds=rng.randint(0, 3599))
                svc = rng.choice(SERVICES)
                host = rng.choice(HOSTS)
                level, tmpl = rng.choices(TEMPLATES_HEALTHY, weights=[70, 15, 10, 4, 1])[0]
                # Inject latency spike on checkout-svc
                lat_max = 1500 if svc == "checkout-svc" else 150
                lat_min = 800 if svc == "checkout-svc" else 40
                msg = tmpl.format(
                    svc=svc, lat=rng.randint(lat_min, lat_max), uid=rng.randint(1, 999),
                    rows=rng.randint(1, 50), n=rng.randint(1, 3),
                )
                f.write(_line(ts, svc, host, level, msg) + "\n")
            # Inject the new error template ~400/hr on payment-svc
            for _ in range(400):
                ts = base + timedelta(seconds=rng.randint(0, 3599))
                level, tmpl = rng.choice(TEMPLATES_DRIFT)
                msg = tmpl.format(svc="payment-svc", lat=rng.randint(2000, 6000))
                f.write(_line(ts, "payment-svc", rng.choice(HOSTS), level, msg) + "\n")
    return path


if __name__ == "__main__":
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    healthy_start = now - timedelta(days=14)
    drifted_start = now - timedelta(days=1)
    h = write_healthy(healthy_start, days=7)
    d = write_drifted(drifted_start, hours=24)
    print(f"Wrote {h} and {d}")
    print(f"Healthy window: {healthy_start.isoformat()} → {(healthy_start + timedelta(days=7)).isoformat()}")
    print(f"Drifted window: {drifted_start.isoformat()} → {(drifted_start + timedelta(hours=24)).isoformat()}")
