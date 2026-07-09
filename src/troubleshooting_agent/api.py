from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, BackgroundTasks, Request, HTTPException
from openai import OpenAI
from pydantic import BaseModel

from .agent import build_graph
from .config import settings
from .vectorstore import VectorStore
from .tools.grafana import GrafanaSignals
from .tools.kubernetes import KubernetesSignals
from .tools.registry import SignalRegistry
from .sources.base import Alert
from .sources.alertmanager import AlertManagerAdapter
import logging
from .logging import configure_logging


class AlertPayload(BaseModel):
    """Flat payload for the manual test endpoint (POST /webhook/test)."""
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

    # Alert sources the webhook understands, keyed by their URL path segment.
    # Add future adapters (Opsgenie, incident.io, ...) here — nothing else changes.
    adapters = {a.name: a for a in (AlertManagerAdapter(),)}

    def run_agent(alert: Alert) -> None:
        """Run the agent for one normalized alert in a background task. Logs the diagnosis."""
        try:
            final = graph.invoke({
                "alert": alert.alert_text,
                "alert_time": alert.alert_time or datetime.now(timezone.utc).isoformat(),
                "namespace": alert.namespace,
                "pod": alert.pod,
                "node": alert.node,
            })
            result = final["result"]
            logging.info(
                "diagnosis complete: likely_cause=%r confidence=%.2f",
                result.likely_cause, result.confidence,
            )
            logging.debug("full diagnosis:\n%s", result.model_dump_json(indent=2))
        except Exception:
            logging.exception("agent run failed for alert: %s", alert.alert_text.splitlines()[0])

    # NOTE: this static route MUST be declared before "/webhook/{source}" below,
    # otherwise the path-param route would swallow "/webhook/test" (source="test").
    @app.post("/webhook/test")
    async def receive_test(payload: AlertPayload, background: BackgroundTasks):
        """Manual test endpoint: post a flat alert and run the agent once."""
        alert = Alert(
            alert_text=f"{payload.title}\n\n{payload.description}".strip(),
            namespace=payload.namespace,
            source="test",
        )
        background.add_task(run_agent, alert)
        return {"status": "accepted", "scheduled": 1}

    @app.post("/webhook/{source}")
    async def receive_alert(source: str, request: Request, background: BackgroundTasks):
        """Accept a webhook from an alert source, schedule the agent per firing alert, ack.

        The source is chosen by the URL path (e.g. /webhook/alertmanager). Its adapter
        parses the raw payload into normalized Alert(s); each is diagnosed in the
        background so the caller gets its 200 in milliseconds.
        """
        adapter = adapters.get(source)
        if adapter is None:
            raise HTTPException(status_code=404, detail=f"unknown alert source {source!r}")
        alerts = adapter.parse(await request.json())
        for alert in alerts:
            background.add_task(run_agent, alert)
        logging.info("webhook[%s]: scheduled %d firing alert(s)", source, len(alerts))
        return {"status": "accepted", "scheduled": len(alerts)}

    @app.get("/healthz")
    async def healthz():
        """Liveness/readiness probe target for Kubernetes."""
        return {"status": "ok"}

    return app
