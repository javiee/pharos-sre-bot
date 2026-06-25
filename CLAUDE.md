# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A PoC on-call troubleshooting agent (Python package `troubleshooting_agent`, deployed as `pharos-sre-bot`). The flow is: **webhook receives an alert → RAG retrieves runbook snippets from Qdrant → a LangGraph agent loop diagnoses, optionally pulling live Grafana/Kubernetes signals via MCP → emits a structured `Diagnosis`**. The diagnosis is currently logged, not returned to the caller — the webhook acks immediately and runs the agent in a background task.

## Commands

Dependency/runtime management is via **uv** (not pip/poetry). Python 3.12+.

```bash
uv sync                       # install deps (add --dev for pytest/httpx)
docker compose up -d          # start local Qdrant + Grafana MCP + Kubernetes MCP
uv run tsa ingest             # ingest runbooks into Qdrant (reads LOCAL_RUNBOOK_PATH etc.)
uv run tsa api                # run the FastAPI server (uvicorn, reload=True) on :7070
```

The `tsa` console-script entry point is `troubleshooting_agent.cli:main` and has exactly two subcommands: `api` and `ingest`.

Config comes from a `.env` file at repo root (see `.env.example`) loaded by pydantic-settings; every setting in `config.py` maps 1:1 to an UPPER_SNAKE env var.

**Tests:** there are none yet. `pytest`/`httpx` are declared as dev deps and the CI test job exists but is fully commented out in `.github/workflows/build.yml`. Run a single test with `uv run pytest path::test_name` once a suite exists.

Helper scripts in `scripts/` (`grafana-get-tools.py`, `k8-get-tools.py`) dump the tool list a running MCP server advertises — useful when curating the allowlists.

## Architecture

The package lives in `src/troubleshooting_agent/`. Key modules and how they fit:

- **`api.py`** — application factory `create_app()`. Builds dependencies *once* at startup (VectorStore, OpenAI client, the two signal providers, the compiled graph), then wires routes. `POST /webhook/alert` validates against `AlertPayload`, stamps the receive time, schedules `run_agent` as a background task, and returns `{"status": "accepted"}`. `GET /healthz` is the k8s probe target.

- **`agent.py`** — the LangGraph state machine, the heart of the system. Nodes: `classify → retrieve → diagnose → (gather_signals → diagnose)* → summarize`. State is `AgentState` (a `total=False` TypedDict). The **diagnose↔gather_signals loop** is the core control flow: `diagnose` asks the LLM to emit a `SignalRequest` JSON (`tool`/`arguments`/`reason`); `should_continue` loops back to fetch that one signal only if `needs_more` is set AND `steps < settings.max_steps`, otherwise goes to `summarize`. `summarize` produces the final `Diagnosis` model.

- **LLM interaction quirks (important):** the target LLM is a local llama-swap/Qwen endpoint, not Anthropic/OpenAI hosted. Strict `json_schema` response format **hangs** on this stack, so `summarize` uses `{"type": "json_object"}` + field descriptions in the prompt + client-side `model_validate_json`. All calls pass `extra_body={"chat_template_kwargs": {"enable_thinking": False}}`. The default model id is in `config.py` (`MODEL`).

- **`vectorstore.py`** — the only module that imports `qdrant_client`. `ensure_collection` is idempotent; `upsert` uses a deterministic `uuid5(source\x00text)` id so re-ingesting overwrites rather than duplicates. The collection dimension (384, COSINE) **must** match the embedding model.

- **`embeddings.py`** — wraps fastembed `BAAI/bge-small-en-v1.5` (384-dim). `EMBED_MODEL`/`EMBED_DIM` are a data contract with the Qdrant collection — changing the model means recreating the collection.

- **`ingest.py`** — chunks markdown on paragraph breaks (~1200 chars) and upserts. Local-file ingestion works; `ingest_git`/`ingest_notion` are stubs.

- **`tools/`** — the signal layer. `base.py` defines the `SignalProvider` Protocol (structural: `list_tools()` + `call_tool()`) and `ToolInfo`. `grafana.py` and `kubernetes.py` are sync wrappers over MCP servers reached over streamable-HTTP (each call opens a fresh `ClientSession` via `asyncio.run`). `registry.py` (`SignalRegistry`) merges providers and routes a tool name to its owner.

- **Read-only safety model (do not weaken):** each provider hard-codes an allowlist (`ALLOWED_GRAFANA_TOOLS`, `ALLOWED_K8S_TOOLS`) of read-only tool names. The Grafana MCP server advertises ~56 tools including mutating ones; only allowlisted names are shown to the LLM *and* `call_tool` raises `PermissionError` on anything else. This is defense-in-depth on top of read-only service accounts / `--read-only` MCP flags. When adding a tool, confirm it cannot mutate state before adding it to the frozenset.

- **`logging.py`** — defines a custom **TRACE** level (5, below DEBUG) exposed as `logger.trace(...)`. DEBUG = tool calls/args/results; TRACE = full prompts, raw LLM responses, whole AgentState, per-chunk text, plus the noisy MCP/httpx/openai loggers (held at WARNING otherwise). Set verbosity with `LOG_LEVEL` (e.g. `LOG_LEVEL=TRACE`).

## Deployment

- **`Dockerfile`** — multi-stage uv build; runtime image runs as non-root `sre`, entrypoint `tsa`, default cmd `api`, exposes 7070.
- **`chart/pharos-sre-bot/`** — Helm chart. Agent + optional sidecars (Qdrant, Grafana MCP, Kubernetes MCP) toggled in `values.yaml`. `agent.env` keys map 1:1 to the pydantic Settings env vars; derived URLs (VECTOR_DB_URL, GRAFANA_MCP_URL, K8S_MCP_URL) are computed by the chart from the sidecar toggles. **No ingress by design** — alert sources reach the webhook in-cluster (ClusterIP). The chart creates no Secret; wire `LLM_API_KEY` / `GRAFANA_SERVICE_ACCOUNT_TOKEN` via `extraEnv`/`envFrom`. `kubernetesMcp.rbac.scope` chooses cluster-wide vs per-namespace read-only RBAC.
- Runbooks ship two ways: ingested from a path/git into Qdrant, or mounted via the chart's `runbooks/` ConfigMap.

## CI

`.github/workflows/build.yml` builds and pushes a multi-stage Docker image to GHCR **only on `v*` tags**. Note `build-and-push` declares `needs: [test]` but the `test` job is commented out — uncommenting tests requires restoring that job too.
