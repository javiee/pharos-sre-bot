# AGENTS.md

PoC on-call troubleshooting agent (Python 3.12, **uv**, package `troubleshooting_agent`, entrypoint `tsa`).
Flow: webhook alert → RAG runbook chunks from Qdrant → LangGraph diagnose loop (at most one read-only Grafana/K8s MCP signal per turn) → structured `Diagnosis`. The diagnosis is **logged only** — the webhook acks immediately and runs the agent in a background task.

## Commands

```bash
cp .env.example .env     # only committed template; .env is read by BOTH docker compose and the agent
uv sync --dev            # deps + pytest/httpx (no test suite exists yet)
docker compose up -d     # Qdrant :6333, Grafana MCP :9000/mcp, K8s MCP :9001/mcp
uv run tsa ingest        # chunk runbooks from LOCAL_RUNBOOK_PATH into Qdrant
uv run tsa api --reload  # FastAPI on :7070 (reload is opt-in; default off)
```

- `tsa` has exactly two subcommands: `api`, `ingest`.
- Verify a run: `curl -s -X POST localhost:7070/webhook/test -H 'Content-Type: application/json' -d '{"title":"PodCrashLooping","description":"checkout-api restarting","namespace":"shop"}'` → `200 {"status":"accepted"}`; the diagnosis arrives in the server logs, not the response.
- No lint/typecheck config exists. CI (`.github/workflows/build.yml`) builds/pushes a GHCR image **only on `v*` tags**; the `test` job is commented out.
- `uv run python scripts/grafana-get-tools.py` / `scripts/k8-get-tools.py` dump the tool names + schemas a running MCP server advertises — the source of truth when curating the allowlists.

## Local dev gotchas

- `docker compose` mounts `./lh-satellite-kubeconfig` (gitignored) into the K8s MCP container — a real kubeconfig must exist at that path or the container fails.
- The `grafana-mcp` compose service hardcodes an ingress IP in `extra_hosts` plus `GRAFANA_URL` — edit both for your environment; `GRAFANA_SERVICE_ACCOUNT_TOKEN` comes from `.env`.
- `.env.example` values are partly stale (`GRAFANA_MCP_URL=http://localhost:8000/sse` vs the `:9000/mcp` used by compose and `config.py`; `LOCAL_RUNBOOK_PATH` points at another repo). Trust `config.py` defaults + `docker-compose.yml`, and verify against the running server with the scripts above.
- `tsa ingest` only works from local files. `GIT_RUNBOOK_URL` / `NOTION_TOKEN` are wired to broken stubs — setting either crashes ingestion. A missing `LOCAL_RUNBOOK_PATH` is NOT an error: the glob finds nothing and it reports success with 0 chunks.
- Both MCP endpoints speak streamable-HTTP on the `/mcp` path, not SSE (see the compose `command:` lines). Mixing up the transports caused a real "Session terminated" bug — see the comment in `scripts/k8-get-tools.py`.
- First `ingest`/`api` run downloads the fastembed model (BAAI/bge-small-en-v1.5).

## API surface (newer than the README describes)

- `POST /webhook/{source}` — the source is chosen by URL segment and mapped to an adapter registered in `api.py` (see `sources/base.py`: `SourceAdapter` protocol = `name` + `parse(payload) -> list[Alert]`). Only `alertmanager` exists → `POST /webhook/alertmanager`.
- `POST /webhook/test` — flat `{title, description, namespace}` for manual runs. This static route **must** stay declared before `/webhook/{source}` (NOTE in `api.py`).
- `GET /healthz` — k8s probe.
- Adding an alert source = new adapter + one line in the `adapters` dict; nothing else changes. Unknown sources get a 404.
- The README quickstart is stale: its curl hits `/webhook/alert` (404 — no such source) and it references a `.env-dev` file that does not exist. Trust this file over the README.

## Agent internals (`src/troubleshooting_agent/`)

- Graph (`agent.py`): `classify → retrieve → diagnose ↔ gather_signals → summarize`. `should_continue` loops only while `needs_more` AND `steps < MAX_STEPS` (default 6). `classify` is a PoC no-op: `category` is just the alert text used as the RAG query.
- The LLM is a local llama-swap/Qwen endpoint via the OpenAI-compatible API. **Strict `json_schema` response format hangs on this stack** — use `{"type": "json_object"}` + field descriptions in the prompt + client-side `model_validate_json`, and always pass `extra_body={"chat_template_kwargs": {"enable_thinking": False}}`. `LLM_BASE_URL` / `LLM_API_KEY` / `MODEL` ship empty and must be set.
- `gather_signals` auto-fixes `query_prometheus` args (`query`→`expr`, 10-minute range window anchored at `alert_time`); the Grafana provider injects `datasourceUid` when missing. Don't make the LLM emit these.
- MCP wrappers are sync-over-async: every `list_tools`/`call_tool` opens a fresh `ClientSession` via `asyncio.run`.
- Embedding contract: fastembed `BAAI/bge-small-en-v1.5`, 384-dim, COSINE — the Qdrant collection must match; changing the model means recreating the collection. Point IDs are `uuid5(source\0text)`, so re-ingesting overwrites instead of duplicating.
- Logging: custom `TRACE` level (5) below DEBUG, exposed as `logger.trace()`. `LOG_LEVEL=DEBUG` = tool calls/args/results; `TRACE` = full prompts + raw LLM responses. Noisy third-party loggers (httpx, mcp, openai, …) are held at WARNING except under TRACE.

## Read-only safety model (do not weaken)

- `tools/grafana.py` / `tools/kubernetes.py` hard-code `ALLOWED_*_TOOLS` frozensets. The Grafana MCP server advertises ~56 tools including mutating ones; only allowlisted names are shown to the LLM **and** `call_tool` raises `PermissionError` on anything else.
- One signal per diagnose turn; the loop is bounded by `MAX_STEPS`; the K8s MCP runs with `--read-only --disable-destructive`.
- Before adding a tool to an allowlist, confirm it cannot mutate state.

## Deployment

- `Dockerfile`: multi-stage uv build, non-root `sre` user, entrypoint `tsa`, default cmd `api`, port 7070.
- `chart/pharos-sre-bot/`: sidecars toggled in `values.yaml` (Qdrant on + PVC, Grafana MCP off, K8s MCP off). Derived URLs (`VECTOR_DB_URL`, `GRAFANA_MCP_URL`, `K8S_MCP_URL`, …) are computed by the chart from the sidecar toggles — don't set them in `agent.env`. ClusterIP only, **no ingress by design** (alert sources reach the webhook in-cluster). The chart creates no Secret — wire `LLM_API_KEY` / `GRAFANA_SERVICE_ACCOUNT_TOKEN` via `extraEnv`/`envFrom`. `kubernetesMcp.rbac.scope`: `cluster` vs per-`namespace` read-only RBAC. Optional `runbooks` ConfigMap and `agent.ingestOnStart` (off by default).
