from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, BackgroundTasks
from openai import OpenAI
from pydantic import BaseModel

from .agent import build_graph
from .config import settings
from .vectorstore import VectorStore
from .tools.grafana import GrafanaSignals
from .tools.kubernetes import KubernetesSignals
from .tools.registry import SignalRegistry
import logging
from .logging import configure_logging

class AlertPayload(BaseModel):
    """The shape of an incoming alert. FastAPI validates requests against this.
    """
    title: str
    description: str = ""
    namespace: str | None = None
    
def create_app() -> FastAPI:
    """Application factory: build dependencies once, wire routes, return the app.

    Using a factory (rather than module-level globals) means the graph and its
    clients are constructed exactly once when the server starts, and tests can
    build their own app with fakes.
    """
    configure_logging()  
    logging.info("starting Troubleshooting Agent")
    app = FastAPI(title="Troubleshooting Agent")
    store = VectorStore()
    store.ensure_collection()
    client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)
    providers = [GrafanaSignals(), KubernetesSignals()]
    signals = SignalRegistry(providers)
    graph = build_graph(store, client, signals=signals)

    def run_agent(alert_text: str, alert_time: str, namespace: str) -> None:
        """Run the agent in a background thread. Logs to stdout."""
        try:
            final = graph.invoke({"alert": alert_text, "alert_time": alert_time, "namespace": namespace})
            result = final["result"]
            logging.info(
                "diagnosis complete: likely_cause=%r confidence=%.2f",
                result.likely_cause, result.confidence,
            )
            logging.debug("full diagnosis:\n%s", result.model_dump_json(indent=2))
        except Exception:
            logging.exception("agent run failed for alert: %s", alert_text.splitlines()[0])


    @app.post("/webhook/alert")
    async def receive_alert(payload: AlertPayload, background: BackgroundTasks):
        """Accept an alert, schedule the agent, and ack immediately.

        `background.add_task(...)` queues run_agent to execute AFTER the response
        is returned — so the alert source gets its 200 in milliseconds.
        """
        alert_text = f"{payload.title}\n\n{payload.description}".strip()
        # Stamp the receive time now — used as the end of the metrics query window.
        alert_time = datetime.now(timezone.utc).isoformat()
        background.add_task(run_agent, alert_text, alert_time, payload.namespace)
        return {"status": "accepted"}
    
    @app.get("/healthz")
    async def healthz():
        """Liveness/readiness probe target for Kubernetes."""
        return {"status": "ok"}
    return app
    