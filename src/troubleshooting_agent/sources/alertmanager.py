from __future__ import annotations

from pydantic import BaseModel, Field

from .base import Alert


class _AlertManagerAlert(BaseModel):
    """One alert object inside an Alertmanager webhook payload."""
    status: str = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str | None = None
    fingerprint: str = ""


class AlertmanagerWebhook(BaseModel):
    """The Alertmanager webhook envelope; carries a batch of alerts."""
    status: str = "firing"
    alerts: list[_AlertManagerAlert] = Field(default_factory=list)


def _context_block(labels: dict, annotations: dict) -> str:
    """Render an alert's annotations and labels into a text block for the LLM."""
    lines = [f"{k}: {v}" for k, v in annotations.items()]   # all annotations
    lines += [f"{k}={v}" for k, v in labels.items()]        # all labels
    return "\n".join(lines)


class AlertManagerAdapter:
    """Maps an Alertmanager webhook payload into Alert(s)."""
    name = "alertmanager"

    def parse(self, payload: dict) -> list[Alert]:
        hook = AlertmanagerWebhook.model_validate(payload)
        out: list[Alert] = []
        for a in hook.alerts:
            if a.status != "firing":          # belt-and-suspenders w/ send_resolved:false
                continue
            labels, ann = a.labels, a.annotations
            title = labels.get("alertname", "unknown alert")
            sev = labels.get("severity", "")
            header = title + (f" (severity: {sev})" if sev else "")
            out.append(Alert(
                alert_text=(header + "\n\n" + _context_block(labels, ann)).strip(),
                namespace=labels.get("namespace"),
                pod=labels.get("pod"),
                node=labels.get("node"),          # best-effort; not all alerts carry it
                alert_time=a.startsAt,            # better metrics-window anchor than "now"
                dedup_id=a.fingerprint,
                severity=sev,
                source=self.name,
                labels=labels,
                annotations=ann,
            ))
        return out
