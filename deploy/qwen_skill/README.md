# Anchor as a Qwen Application Center Custom Skill

This directory packages Anchor for registration as a Custom Skill on Qwen
Cloud's Application Center. Once registered, users can drive a full SRE
drift investigation directly from Qwen Chat:

> "Capture today as a healthy baseline called 'Q3 normal'"
> "Compare yesterday 22:00 to 23:00 against the latest anchor with --deep"
> "Mark drift abc12 as resolved — payment-svc deploy rollback"

## Files

| File | Purpose |
| ---- | ------- |
| `anchor-skill.yaml` | OpenAPI 3.0 spec — upload this to Qwen Application Center |
| `../alibaba-cloud.md` | How to provision the ECS host that runs the shim |
| `src/anchor/skill_server.py` | FastAPI shim implementing the spec |

## One-time registration

### 1. Run the shim on Alibaba Cloud ECS

```bash
# Inside the ECS instance, after `git clone` + python venv
pip install -e '.[skill]'

# Generate a long random token; rotate periodically
export ANCHOR_SKILL_TOKEN="$(openssl rand -hex 32)"
echo "$ANCHOR_SKILL_TOKEN"   # save this somewhere safe

# Bind to localhost behind nginx, or directly on :443 with TLS terminated
uvicorn anchor.skill_server:app --host 0.0.0.0 --port 8080
```

Put it behind a reverse proxy with TLS (Anchor's `deploy/alibaba-cloud.md`
section "TLS via Caddy" shows the 4-line config).

### 2. Patch the manifest

Edit `anchor-skill.yaml`:

- `servers[0].url` → `https://anchor.your-domain.com` (or the ECS public IP)

### 3. Upload to Qwen Application Center

1. Sign in at https://www.qwencloud.com → **Application Center** → **Custom Skills**
2. **Create skill** → pick "Import OpenAPI spec"
3. Upload `anchor-skill.yaml`
4. **Authentication** → Bearer token → paste the `$ANCHOR_SKILL_TOKEN` value
5. **Test** the `list_anchors` endpoint inline — should return `[]` for a fresh install
6. **Publish**

### 4. Use it from Qwen Chat

In a Qwen Chat session, attach the Anchor skill. The model can now invoke
`capture_anchor`, `compare`, `recall_similar`, `submit_feedback` etc. by
function call.

Example prompt:

> *Compare last Tuesday 14:00–15:00 UTC to my latest anchor, deep mode,
> focus="payment errors". If it recalls a past incident, summarize the
> confirmed reason and propose a remediation.*

The model decomposes that into:
```
compare(start="...T14:00Z", end="...T15:00Z", deep=true, focus="payment errors")
```
and the response (with `investigation.summary` + `evidence`) is rendered
back in chat.

## Production hardening checklist

- [ ] `ANCHOR_SKILL_TOKEN` is set to a 32+ char random value (never empty)
- [ ] Shim is **not** exposed on `0.0.0.0:8080` without TLS; use a proxy
- [ ] `SPLUNK_PASSWORD`, `QWEN_API_KEY` loaded from `.env` outside the repo
- [ ] Nightly `deploy/backup_kv_to_oss.py` cron is active
- [ ] ECS Security Group allows inbound 443 only from Qwen Cloud egress IPs
      (check Qwen docs for the current range)
- [ ] Rate limit on `/compare` and `/anchors POST` (each runs a Splunk job)
- [ ] `GET /healthz` is intentionally **unauthenticated** so load-balancer /
      reverse-proxy probes work; allow it through the proxy but rate-limit it
      and don't expose it externally if you can avoid it

## Why this matters for the hackathon

Qwen Cloud rewards "sophisticated use of QwenCloud APIs (e.g. custom skills,
MCP integrations)". Anchor ships both:

- **Custom Skill** — this OpenAPI spec, registerable in Application Center
- **MCP server** — `anchor-mcp` console script, drives any MCP client
  (Claude Desktop, Cursor) via stdio

The same backend powers both surfaces. The `anchor` CLI is just a third
client that happens to live in the same process.
