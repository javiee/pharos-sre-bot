from __future__ import annotations
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Alert:
    """Source-agnostic alert the agent pipeline consumes."""
    alert_text: str                # human-readable; fed to RAG + diagnosis
    namespace: str | None = None   # k8s namespace -> targets k8s tools + PromQL label
    pod: str | None = None         # affected pod -> targets pods_log / pods_get
    node: str | None = None        # affected node -> targets node tools
    alert_time: str | None = None  # ISO8601; anchors the Prometheus query window
    dedup_id: str = ""             # source-native id/fingerprint (future dedup)
    severity: str = ""
    source: str = ""               # "alertmanager" | "opsgenie" | ...
    labels: dict = field(default_factory=dict)       # full passthrough
    annotations: dict = field(default_factory=dict)  # full passthrough


class SourceAdapter(Protocol):
    """Maps a raw webhook payload from one source into Alert(s)."""
    name: str
    def parse(self, payload: dict) -> list[Alert]: ...
