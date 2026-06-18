# 6 · The deployment: Splunk + Qwen on Alibaba Cloud in three commands

*Part 6 — the final post — of the [Building Anchor](README.md) series.*

---

The first five posts were about how Anchor *works*. This one is about
how to put it on a server that someone other than you can talk to.

The hackathon target is **Alibaba Cloud**: ECS for compute, OSS for
durable memory backups, DashScope for the LLM calls Anchor already
makes. The walkthrough lives in
[`deploy/alibaba-cloud.md`](../../deploy/alibaba-cloud.md); this post
explains *why* each piece is shaped the way it is.

## The three-command path

Once the console-only prerequisites are done (more on those below),
the entire ECS install is:

```bash
ssh root@<ecs-public-ip>
curl -fsSL https://raw.githubusercontent.com/faketut/Anchor/main/deploy/setup_ecs.sh | bash
nano /opt/anchor/.env       # fill in SPLUNK_PASSWORD, QWEN_API_KEY, OSS_* creds
bash /opt/anchor/deploy/verify_setup.sh
```

That's the entire happy path. `setup_ecs.sh` is idempotent — safe to
re-run after editing `.env`. `verify_setup.sh` is a pre-flight
checker that exits non-zero on any failure, so it's usable as a
healthcheck.

## What `setup_ecs.sh` actually does

The script ([`deploy/setup_ecs.sh`](../../deploy/setup_ecs.sh))
consolidates seven steps:

1. **OS sanity check** — bail if not root, bail if not Ubuntu.
2. **`apt-get install`** — Docker, Compose v2, git, Python venv.
3. **`git clone || git pull`** — checks out Anchor to `/opt/anchor`.
4. **`docker compose up -d`** with the Alibaba overlay
   ([`deploy/docker-compose.alibaba.yml`](../../deploy/docker-compose.alibaba.yml))
   that:
   - sets `restart: unless-stopped` so Splunk survives ECS reboots
   - binds Splunk Web (8000) to localhost only — accessed via SSH
     tunnel, never the public internet
   - leaves the mgmt API (8089) exposed for the Anchor CLI but
     restricted at the security-group layer to your laptop's IP
   - removes HEC (8088) entirely (not needed for the demo flow)
   - caps Splunk at 2 vCPU / 4 GB so a runaway query doesn't OOM the box
5. **Wait-for-Splunk loop** — polls `https://localhost:8089/services/server/info`
   for up to 120 seconds before continuing. First-boot init is the
   slowest step; without this wait, the next command fails on a fresh
   install.
6. **KV Store schema install** — copies `splunk/collections.conf`
   into the container, chowns it, and restarts Splunk. This is the
   only step that requires `docker exec` choreography; everything
   else is host-side.
7. **Anchor venv + `pip install -e '.[alibaba]'`** — sets up the
   Python environment for the nightly OSS backup cron.

The script ends with a printed checklist of the *human* next steps:
edit `.env`, run the verifier, schedule the cron.

## What `verify_setup.sh` checks

The verifier ([`deploy/verify_setup.sh`](../../deploy/verify_setup.sh))
runs six independent checks and reports each as PASS / FAIL / SKIP:

| Check | What FAIL means |
|---|---|
| `.env` exists | You haven't filled in credentials yet |
| Required env vars set | `SPLUNK_PASSWORD` / `QWEN_API_KEY` placeholders still present |
| Splunk mgmt API reachable | Container not running, or security group blocking |
| All 3 KV collections present | `collections.conf` install failed; re-run `setup_ecs.sh` |
| OSS bucket reachable (optional) | AK pair wrong, or wrong endpoint/bucket |
| `anchor list` succeeds | End-to-end smoke test — the CLI ↔ KV path works |

It exits non-zero on any FAIL so you can chain it: e.g. `bash
deploy/verify_setup.sh && systemctl restart anchor-cron`.

The "optional" tag on OSS is deliberate. You can run Anchor without
the OSS backup; you just lose the durability guarantee. The verifier
says SKIP, not FAIL, when `OSS_*` env vars aren't set.

## What only humans can do

There are three things the script *can't* automate, because they
require the Alibaba Cloud console:

| Step | Why manual |
|---|---|
| Provision the ECS instance | Account-scoped action, billing implications |
| Open security-group ports 22 + 8089 to your laptop IP | Requires knowing your client IP — different for every dev |
| Create the OSS bucket + RAM user with `AliyunOSSFullAccess` | Account-scoped; RAM user creation needs human review |

The walkthrough in [`deploy/alibaba-cloud.md`](../../deploy/alibaba-cloud.md)
specifies exact values:

- `ecs.g7.large` (2 vCPU, 8 GB), Singapore or Hong Kong
- Ubuntu 24.04 LTS, 60 GB ESSD
- Security group: TCP 22 + TCP 8089 from your laptop's `/32`, that's it
- OSS bucket: private ACL, **versioning enabled** so backups are
  immutable

The Singapore / Hong Kong region choice matters: DashScope's
international endpoint has the lowest latency from those regions, and
keeps the ECS-to-Qwen call sub-100 ms.

## OSS as the durability layer

KV Store data lives on the ECS instance disk. ECS disks are durable
(triple-replicated), but the *blast radius* is one instance. If you
accidentally `docker compose down -v` (the `-v` removes volumes), your
anchors and drift history are gone.

[`deploy/backup_kv_to_oss.py`](../../deploy/backup_kv_to_oss.py)
solves that with a 60-line script:

```python
def dump_kv() -> dict:
    """Snapshot all three collections to a single JSON dict."""
    svc = connect()
    return {
        "anchors":        list(svc.kvstore["anchors"].data.query()),
        "drift_history":  list(svc.kvstore["drift_history"].data.query()),
        "signal_weights": list(svc.kvstore["signal_weights"].data.query()),
        "snapshot_at":    datetime.now(timezone.utc).isoformat(),
    }

def upload_to_oss(payload: dict) -> str:
    auth   = oss2.Auth(os.environ["OSS_ACCESS_KEY_ID"],
                       os.environ["OSS_ACCESS_KEY_SECRET"])
    bucket = oss2.Bucket(auth, os.environ["OSS_ENDPOINT"],
                                os.environ["OSS_BUCKET"])
    key = f"anchor-memory/{datetime.now(timezone.utc).isoformat()}.json"
    bucket.put_object(key, json.dumps(payload).encode("utf-8"),
                      headers={"x-oss-server-side-encryption": "AES256"})
    return key
```

The wiring: `oss2.Auth` → `oss2.Bucket` → `put_object` with
server-side AES256 encryption. That's the *Alibaba Cloud API usage*
the hackathon rules want as proof — a single file that imports the
Alibaba SDK, authenticates with RAM credentials, and pushes data into
the platform.

Scheduled via cron:

```cron
0 3 * * * cd /opt/anchor \
  && set -a; . ./.env; set +a \
  && .venv/bin/python deploy/backup_kv_to_oss.py >> /var/log/anchor-backup.log 2>&1
```

With bucket versioning enabled, every nightly run is preserved.
Restore is `oss-util cp oss://... ./` plus a small replay script that
reads the JSON and writes each row back via `kv_insert`. The repo
doesn't ship the restore script because nobody needs it for the demo;
it's ~30 lines of Python when someone does.

## The three Qwen Cloud surfaces, on the same backend

The ECS install gets you the CLI. The MCP server and Qwen Custom Skill
share the *same* Splunk backend — they're just different transports:

| Surface | How to bring it up |
|---|---|
| **CLI** | already installed by `setup_ecs.sh` |
| **MCP server (stdio)** | `pip install -e '.[mcp]' && anchor-mcp` — plug into Claude Desktop or Cursor |
| **Custom Skill (HTTP)** | `pip install -e '.[skill]' && uvicorn anchor.skill_server:app` — register [`deploy/qwen_skill/anchor-skill.yaml`](../../deploy/qwen_skill/anchor-skill.yaml) in Qwen Cloud → Application Center |

That's by design. The "application" is the SPL + KV layer
([posts 2](02-fingerprint.md) and [3](03-diff-and-weights.md)); the
surfaces are interchangeable. Adding a Slack bot, a Discord bot, or a
PagerDuty webhook is the same pattern: thin transport, call into
`agent.compare()`, render the `CompareResult`.

## What's deliberately not in the demo deploy

A short list of things that would be on the checklist for a real
production deploy but were intentionally cut for the hackathon:

| Skipped | Why |
|---|---|
| TLS on the mgmt API (Caddy / Let's Encrypt) | `SPLUNK_VERIFY_SSL=false` is fine for a 30-second judge demo. Documented in `alibaba-cloud.md` as a real-deploy requirement. |
| systemd unit for the cron | Crontab is fine for a once-a-day backup. Adding systemd adds a unit file with zero behavioral change. |
| RAM policy scoped to the single bucket | `AliyunOSSFullAccess` is broader than necessary. Real deploy should scope to `oss:PutObject` on `anchor-memory/*`. |
| Multi-AZ Splunk replication | Single ECS instance is fine for a demo. Splunk SHC is several weeks of work. |
| Skill-server behind a reverse proxy | The skill server has bearer-token auth via `secrets.compare_digest`, which is the right primitive — but it's running on `0.0.0.0:8000`. For real use, front it with Caddy + TLS. |

The principle: ship the simplest thing that demonstrates the
capability. Document the production gaps honestly.

## Validating the proof

The hackathon submission asks for two things:

1. **A URL to a code file demonstrating Alibaba Cloud API usage.** That
   file is [`deploy/backup_kv_to_oss.py`](../../deploy/backup_kv_to_oss.py).
   60 lines, imports `oss2`, authenticates with RAM AK, uploads with
   server-side encryption. Direct mapping from the rules to the code.
2. **Evidence the backend runs on Alibaba Cloud.** The 30-second
   demo video walks: Alibaba Cloud console showing the ECS instance →
   SSH into it → `docker ps` showing Splunk → `curl
   https://localhost:8089/services/server/info` returning a 200 →
   OSS console showing the `anchor-memory/*.json` objects → laptop
   side `anchor compare` against the ECS public IP.

That second list is the checklist at the bottom of
[`deploy/alibaba-cloud.md`](../../deploy/alibaba-cloud.md).

---

## Wrapping the series

Six posts in:

- [**Post 1**](01-why-memoryagent.md) — the on-call problem and the
  three-memory framing.
- [**Post 2**](02-fingerprint.md) — five SPL queries → one KV row.
- [**Post 3**](03-diff-and-weights.md) — the diff engine and decay
  toward 1.0.
- [**Post 4**](04-narrator-llm-at-edge.md) — the LLM only narrates.
- [**Post 5**](05-planner-react-loop.md) — the optional ReAct planner.
- [**Post 6**](06-deploy-alibaba-cloud.md) — the deploy you're reading.

The common thread across all six: *most of the agent's value lives in
the deterministic core; the LLM is an edge component*. That's why
ranking, recall, decay, and the planner's tool restrictions all
matter more than the prompt itself.

If you'd build this differently — or you've shipped something similar
and want to compare notes — open an issue on
[`faketut/Anchor`](https://github.com/faketut/Anchor/issues).

*Previous: [← The planner — function-calling for SRE drill-down](05-planner-react-loop.md)*
