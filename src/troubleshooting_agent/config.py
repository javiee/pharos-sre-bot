from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):

    model_config = SettingsConfigDict(env_file=".env", extra = "ignore")
    log_format: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    log_level: str = "INFO"
    
    vector_db_url: str = "http://localhost:6333"
    vector_db_api_key: str | None = None
    collection_name: str = "runbooks"

    # LLM endpoint. Environment-specific — MUST be set per deployment (env var or
    # Secret). Left empty so a missing value fails loudly rather than silently
    # using a baked-in dev endpoint. See .env-dev for the local value.
    llm_base_url: str = ""
    llm_api_key: str = ""
    model: str = ""

    # Upper bound on the diagnose<->gather loop, mirroring values.yaml `agent.maxSteps`.
    max_steps: int = 6

    # Runbook sources. local_runbook_path defaults to the chart's ConfigMap mount
    # point; ingestion is a no-op when the path is absent. See .env-dev for the
    # local value.
    git_runbook_url: str | None = None
    notion_token: str | None = None
    local_runbook_path: str = "/runbooks"

    host: str = "0.0.0.0"
    port: int = 7070
    reload: bool = False

    grafana_mcp_url: str = "http://localhost:9000/mcp"
    # Prometheus datasource UID, auto-injected into query_prometheus calls.
    grafana_prometheus_uid: str = "prometheus"
    k8s_mcp_url: str = "http://localhost:9001/mcp"

settings = Settings()