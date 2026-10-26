#!/usr/bin/env python3
"""On-demand e2e fault-injection test runner for the troubleshooting agent.

Applies the scenario fixtures from tests/e2e/ (workloads + Chaos Mesh CRs),
waits for the fault state, fires the matching Alertmanager webhook at the
deployed agent, collects the diagnosis from the agent logs, scores the
pipeline, and writes a report to reports/.

Usage:
  uv run python scripts/e2e.py list
  uv run python scripts/e2e.py oom crashloop-podfailure
  uv run python scripts/e2e.py --all
  uv run python scripts/e2e.py --all --skip-negatives
  uv run python scripts/e2e.py cpu-stress --keep   # leave fixtures for manual inspection

Safety:
  - Every scenario runs in its own tsa-* namespace (chaos CRs can only target
    pods in their own namespace, so nothing outside tsa-* is reachable).
  - Stress targets carry CPU/memory limits; worst-case node impact is bounded.
  - Teardown is guaranteed, including on Ctrl-C.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = REPO / "tests" / "e2e"
REPORTS_DIR = REPO / "reports"
AGENT_NS = "monitoring"
AGENT_LABEL = "app.kubernetes.io/name=pharos-sre-bot"
AGENT_SERVICE = "pharos-sre-bot"
DEFAULT_PORT = 17070  # private port-forward port; do not collide with a manual 7070
TS_PREFIX = "tsa-"

DIAG_RE = re.compile(r"diagnosis complete: likely_cause=(.*) confidence=([\d.]+)")
TOOL_RE = re.compile(r"gather_signals: calling '([^']+)' \(step (\d+) of (\d+)\)")
CHUNK_RE = re.compile(r"retrieve: (\d+) chunks")
FAIL_RE = re.compile(r"agent run failed")


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class K8s:
    def __init__(self, context: str, insecure: bool):
        self.base = ["kubectl", f"--context={context}"]
        if insecure:
            self.base.append("--insecure-skip-tls-verify")

    def run(self, args: list[str], timeout: int = 120) -> tuple[int, str, str]:
        cmd = self.base + args + ["--request-timeout=30s"]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()

    def get_json(self, args: list[str]) -> object:
        rc, out, err = self.run(args + ["-o", "json"])
        if rc != 0:
            raise RuntimeError(f"kubectl get failed: {err}")
        return json.loads(out) if out else None


class PortForward:
    def __init__(self, k8s: K8s, port: int):
        self.k8s = k8s
        self.port = port
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.k8s.base
            + ["-n", AGENT_NS, "port-forward", f"svc/{AGENT_SERVICE}", f"{self.port}:7070"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"port-forward exited early: {self.proc.stderr.read()}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=2):
                    return
            except OSError:
                time.sleep(1)
        raise RuntimeError("port-forward did not come up within 30s")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def post(self, path: str, payload: dict | bytes) -> tuple[int, str]:
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()


@dataclass
class Scenario:
    name: str
    path: Path
    data: dict

    @property
    def ns(self) -> str:
        return TS_PREFIX + self.name

    @property
    def no_workload(self) -> bool:
        return bool(self.data.get("no_workload"))

    @property
    def has_chaos(self) -> bool:
        return (self.path / "chaos.yaml").exists()


def discover() -> list[Scenario]:
    out = []
    for d in sorted(SCENARIOS_DIR.iterdir()):
        meta = d / "scenario.yaml"
        if d.is_dir() and meta.exists():
            out.append(Scenario(d.name, d, yaml.safe_load(meta.read_text())))
    return out


def pod_states(k8s: K8s, ns: str, label: str) -> list[dict]:
    rc, out, _ = k8s.run(["-n", ns, "get", "pods", f"-l={label}", "-o", "json"])
    if rc != 0 or not out:
        return []
    return json.loads(out).get("items", [])


def _pod_state_summary(pods: list[dict]) -> str:
    if not pods:
        return "no pods"
    parts = []
    for p in pods:
        st = p["status"]
        cs = st.get("containerStatuses", [{}])[0]
        waiting = (cs.get("state", {}).get("waiting") or {}).get("reason")
        last = (cs.get("lastState", {}).get("terminated") or {}).get("reason")
        parts.append(
            f'{p["metadata"]["name"]}: phase={st.get("phase")}'
            + (f" waiting={waiting}" if waiting else "")
            + (f" lastState={last}" if last else "")
        )
    return "; ".join(parts)


def _wait_pod_state(k8s: K8s, ns: str, label: str, kind: str, timeout_s: int) -> list[dict]:
    """Poll until a pod matching `label` reaches the fault state `kind`."""
    deadline = time.time() + timeout_s
    pods: list[dict] = []
    while time.time() < deadline:
        pods = pod_states(k8s, ns, f"app={label}")
        for p in pods:
            st = p["status"]
            phase = st.get("phase", "")
            if kind == "crashloop" and phase == "CrashLoopBackOff":
                return pods
            if kind == "oomkilled":
                for cs in st.get("containerStatuses", []):
                    if (cs.get("lastState", {}).get("terminated") or {}).get("reason") == "OOMKilled":
                        return pods
            if kind == "imagepullbackoff":
                for cs in st.get("containerStatuses", []):
                    reason = (cs.get("state", {}).get("waiting") or {}).get("reason")
                    if reason in ("ErrImagePull", "ImagePullBackOff"):
                        return pods
            if kind == "containerconfigerror":
                reason = (
                    (st.get("containerStatuses", [{}])[0].get("state", {}).get("waiting") or {}).get("reason")
                    if st.get("containerStatuses")
                    else ""
                )
                if "CreateContainerConfigError" in (st.get("message") or "") or reason == "CreateContainerConfigError":
                    return pods
        time.sleep(5)
    return pods


def _wait_ready(k8s: K8s, ns: str, labels: list[str], timeout_s: int) -> tuple[bool, list[dict]]:
    """Poll until every label in `labels` has at least one ready pod."""
    deadline = time.time() + timeout_s
    all_pods: list[dict] = []
    while time.time() < deadline:
        all_pods = []
        for label in labels:
            all_pods += pod_states(k8s, ns, f"app={label}")
        ready = {
            p["metadata"]["labels"].get("app")
            for p in all_pods
            if p["status"].get("phase") == "Running"
            and all(c.get("ready") for c in p["status"].get("containerStatuses", [{}]))
        }
        if set(labels) <= ready:
            return True, all_pods
        time.sleep(5)
    return False, all_pods


def _wait_verify(k8s: K8s, sc: Scenario, cfg: dict) -> tuple[bool, str]:
    """Poll an exec-based check (e.g. `df -m /data`) until it passes."""
    pods = pod_states(k8s, sc.ns, "app=victim")
    if not pods:
        return False, "no victim pod for verify"
    pod = pods[0]["metadata"]["name"]
    deadline = time.time() + cfg["verify_timeout_s"]
    last = "?"
    while time.time() < deadline:
        rc, out, _ = k8s.run(["-n", sc.ns, "exec", pod, "--"] + cfg["verify_exec"].split())
        if rc == 0 and out:
            try:
                avail = int(out.strip().splitlines()[-1].split()[3])
                if avail <= cfg["verify_max_available_mib"]:
                    return True, f"{cfg['verify_exec']} available={avail}Mi"
                last = f"available={avail}Mi"
            except (IndexError, ValueError):
                last = f"unparseable: {out!r}"
        time.sleep(5)
    return False, f"verify never passed (last: {last})"


def run_gate(k8s: K8s, sc: Scenario) -> dict:
    cfg = sc.data.get("wait") or {"type": "none"}
    kind = cfg.get("type", "none")
    timeout = int(cfg.get("timeout_s", 180))
    if kind == "none":
        return {"ok": True, "state": "skipped (no workload)"}

    if kind == "ready":
        ok, pods = _wait_ready(k8s, sc.ns, list(cfg.get("pods", ["victim"])), timeout)
        if not ok:
            return {"ok": False, "state": _pod_state_summary(pods)}
        settle = int(cfg.get("settle_s", 0))
        if settle:
            time.sleep(settle)
        return {"ok": True, "state": _pod_state_summary(pods)}

    if kind == "timer":
        ok, pods = _wait_ready(k8s, sc.ns, [cfg.get("pod", "victim")], timeout)
        if not ok:
            return {"ok": False, "state": _pod_state_summary(pods)}
        if cfg.get("verify_exec"):
            ok2, info = _wait_verify(k8s, sc, cfg)
            return {"ok": ok2, "state": info}
        time.sleep(timeout)  # metric-settle window
        return {"ok": True, "state": f"pod ready; settled {timeout}s"}

    if kind in ("crashloop", "oomkilled", "imagepullbackoff", "containerconfigerror"):
        pods = _wait_pod_state(k8s, sc.ns, cfg.get("pod", "victim"), kind, timeout)
        state = _pod_state_summary(pods)
        ok = any(_fault_present(p, kind) for p in pods)
        return {"ok": ok, "state": state}

    return {"ok": False, "state": f"unknown wait type: {kind}"}


def _fault_present(p: dict, kind: str) -> bool:
    st = p["status"]
    if kind == "crashloop":
        return st.get("phase") == "CrashLoopBackOff"
    for cs in st.get("containerStatuses", []):
        if kind == "oomkilled" and (cs.get("lastState", {}).get("terminated") or {}).get("reason") == "OOMKilled":
            return True
        if kind == "imagepullbackoff":
            if (cs.get("state", {}).get("waiting") or {}).get("reason") in ("ErrImagePull", "ImagePullBackOff"):
                return True
    if kind == "containerconfigerror":
        return "CreateContainerConfigError" in (st.get("message") or "")
    return False


def find_agent_pod(k8s: K8s) -> str:
    rc, out, err = k8s.run(["-n", AGENT_NS, "get", "pods", f"-l={AGENT_LABEL}", "-o", "name"])
    if rc != 0 or not out:
        raise RuntimeError(f"no agent pod found: {err}")
    pod = out.splitlines()[0].split("/")[-1]
    rc, ready, err = k8s.run(
        ["-n", AGENT_NS, "get", "pod", pod, "-o", "jsonpath={.status.containerStatuses[*].ready}"]
    )
    if rc != 0 or "false" in ready:
        raise RuntimeError(f"agent pod {pod} not fully ready: {ready} {err}")
    return pod


def discover_alert_target(k8s: K8s, sc: Scenario) -> tuple[str | None, str | None]:
    """Find the (pod name, node) to put in the alert labels."""
    alert = sc.data.get("alert", {})
    if sc.no_workload or alert.get("pod"):
        return alert.get("pod"), None
    label = alert.get("pod_label") or (sc.data.get("wait") or {}).get("pod", "victim")
    pods = pod_states(k8s, sc.ns, f"app={label}")
    if not pods:
        return None, None
    return pods[0]["metadata"]["name"], pods[0]["spec"].get("nodeName")


def build_alert_payload(sc: Scenario, pod: str | None, node: str | None) -> dict:
    alert = sc.data["alert"]
    ns = alert.get("namespace") or sc.ns
    labels: dict[str, str] = {
        "alertname": alert["alertname"],
        "severity": alert.get("severity", ""),
        "namespace": ns,
    }
    if pod:
        labels["pod"] = pod
    if node:
        labels["node"] = node
    return {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": labels,
                "annotations": alert.get("annotations", {}),
                "startsAt": utc_iso(),
                "fingerprint": f"e2e-{sc.name}-{int(time.time())}",
            }
        ],
    }


def collect_diagnosis(k8s: K8s, agent_pod: str, since_iso: str, timeout_s: int) -> dict:
    deadline = time.time() + timeout_s
    lines: list[str] = []
    while time.time() < deadline:
        rc, out, _ = k8s.run(
            ["-n", AGENT_NS, "logs", agent_pod, "-c", "agent", f"--since-time={since_iso}"],
            timeout=60,
        )
        if rc == 0 and out:
            lines = out.splitlines()
        text = "\n".join(lines)
        m = DIAG_RE.search(text)
        if m:
            cause = m.group(1).strip()
            if cause.startswith("'") and cause.endswith("'"):
                cause = cause[1:-1]
            tools = [(t, int(s)) for t, s, _ in TOOL_RE.findall(text)]
            return {
                "ok": True,
                "failed": False,
                "timed_out": False,
                "likely_cause": cause,
                "confidence": float(m.group(2)),
                "tools": tools,
                "steps": max((s for _, s in tools), default=0),
                "chunks": [int(c) for c in CHUNK_RE.findall(text)],
                "log_tail": lines[-12:],
            }
        if FAIL_RE.search(text):
            err_lines = [l for l in lines if "ERROR" in l or "Traceback" in l or FAIL_RE.search(l)]
            return {
                "ok": True,
                "failed": True,
                "timed_out": False,
                "likely_cause": None,
                "confidence": None,
                "tools": [],
                "steps": 0,
                "chunks": [],
                "error": err_lines[-10:],
                "log_tail": lines[-12:],
            }
        time.sleep(5)
    return {
        "ok": True,
        "failed": False,
        "timed_out": True,
        "likely_cause": None,
        "confidence": None,
        "tools": [],
        "steps": 0,
        "chunks": [],
        "log_tail": lines[-12:],
    }


def teardown_ns(k8s: K8s, ns: str, timeout: int = 120) -> bool:
    k8s.run(["delete", "namespace", ns, "--wait=false", "--ignore-not-found"])
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc, _, _ = k8s.run(["get", "namespace", ns])
        if rc != 0:
            return True
        time.sleep(5)
    return False


def residue_check(k8s: K8s) -> dict:
    rc, out, _ = k8s.run(["get", "namespaces", "-o", "name"])
    left_ns = [l for l in out.splitlines() if TS_PREFIX in l] if rc == 0 else []
    rc2, out2, _ = k8s.run(["get", "podchaos,stresschaos,networkchaos", "-A", "-o", "name"])
    left_chaos = [l for l in out2.splitlines() if l] if rc2 == 0 else []
    return {"namespaces": left_ns, "chaos_crs": left_chaos}


def run_negatives(pf: PortForward) -> list[dict]:
    out = []

    def record(name, expect, status, body):
        ok = False
        if expect == "200-scheduled-0":
            ok = status == 200 and '"scheduled": 0' in body.replace(" ", "")
        elif expect == "404":
            ok = status == 404
        elif expect == "4xx":
            ok = 400 <= status < 500
        out.append({"name": name, "expect": expect, "status": status, "body": body[:200], "ok": ok})
        print(f"  [neg] {name}: {status} (expect {expect}) -> {'PASS' if ok else 'FAIL'}")

    # resolved alerts must be filtered by the adapter
    status, body = pf.post(
        "/webhook/alertmanager",
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "resolved",
                    "labels": {"alertname": "NegativeTest", "namespace": "tsa-negatives"},
                    "annotations": {},
                }
            ],
        },
    )
    record("resolved-filtered", "200-scheduled-0", status, body)

    # unknown source -> 404
    status, body = pf.post("/webhook/doesnotexist", {"alerts": []})
    record("unknown-source-404", "404", status, body)

    # valid JSON, wrong schema -> expect clean 4xx (500 would be a finding)
    status, body = pf.post("/webhook/alertmanager", {"alerts": "not-a-list"})
    record("bad-schema", "4xx", status, body)

    # malformed JSON body
    status, body = pf.post("/webhook/alertmanager", b"{not json")
    record("bad-json", "4xx", status, body)

    return out


def write_report(results: list[dict], residue: dict, meta: dict) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    jpath = REPORTS_DIR / f"e2e-{stamp}.json"
    mpath = REPORTS_DIR / f"e2e-{stamp}.md"

    neg = [r for r in results if r.get("kind") == "negative"]
    scs = [r for r in results if r.get("kind") == "scenario"]

    lines = [
        "# Pharos SRE Bot — e2e fault-injection report",
        "",
        f"- run: {meta['started_at']} (context {meta['context']}, agent pod `{meta['agent_pod']}`)",
        f"- scenarios: {len(scs)}, negatives: {len(neg)}",
        "",
        "## Negative suite",
        "",
        "| test | expect | got | verdict |",
        "|---|---|---|---|",
    ]
    for r in neg:
        lines.append(f"| {r['name']} | {r['expect']} | {r['status']} | {'PASS' if r['ok'] else '**FAIL**'} |")

    lines += [
        "",
        "## Scenarios",
        "",
        "| # | scenario | pipeline | steps | tools | confidence |",
        "|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(scs, 1):
        pipe = "FAIL" if (r["failed"] or r["timed_out"] or r.get("http") != 200) else "ok"
        tools = ",".join(t for t, _ in r["tools"]) or "-"
        conf = f"{r['confidence']:.2f}" if r["confidence"] is not None else "-"
        lines.append(f"| {i} | {r['name']} | {pipe} | {r['steps']} | {tools} | {conf} |")

    lines += ["", "## Details", ""]
    for i, r in enumerate(scs, 1):
        lines += [
            f"### {i}. {r['name']} — {r['title']}",
            "",
            f"- **ground truth**: {r['ground_truth']}",
            f"- **agent likely_cause**: {r['likely_cause']!r} (confidence {r['confidence']})",
            f"- runbook: {r.get('runbook') or '—'} | fault state at alert: {r['fault_state']}",
            f"- timeline: apply->gate {r['gate_s']:.0f}s, diagnosis {r['diag_s']:.0f}s, total {r['total_s']:.0f}s",
            f"- tools requested: {r['tools'] or 'none'}",
            f"- expected tools (soft): {r['expect_tools']}",
            f"- http={r.get('http')} scheduled={r.get('scheduled')} timed_out={r['timed_out']} failed={r['failed']}",
        ]
        if r.get("error"):
            lines.append("- **error lines**:")
            lines += [f"    {l}" for l in r["error"]]
        lines.append("")

    lines += [
        "## Residue check",
        "",
        f"- leftover tsa-* namespaces: {residue['namespaces'] or 'none'}",
        f"- leftover chaos CRs: {residue['chaos_crs'] or 'none'}",
        "",
        "## Judging cause-match",
        "",
        "The runner scores the pipeline only. For each scenario, compare *ground truth*",
        "with *agent likely_cause* above and mark ✅ (correct cause class) / ❌ / ~ (partially).",
        "",
    ]
    mpath.write_text("\n".join(lines))
    jpath.write_text(json.dumps({"meta": meta, "residue": residue, "results": results}, indent=2))
    return mpath


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenarios", nargs="*", help="scenario names (see `list`), or `negatives`")
    ap.add_argument("--all", action="store_true", help="run every scenario + the negative suite")
    ap.add_argument("--list", action="store_true", help="list available scenarios (also: `e2e.py list`)")
    ap.add_argument("--skip-negatives", action="store_true")
    ap.add_argument("--keep", action="store_true", help="do NOT tear down fixtures (manual debugging)")
    ap.add_argument("--context", default="lh-satellite")
    ap.add_argument("--secure", action="store_true", help="omit --insecure-skip-tls-verify")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="local port for the private port-forward")
    ap.add_argument("--diag-timeout", type=int, default=300, help="max seconds to wait for a diagnosis")
    args = ap.parse_args()

    k8s = K8s(args.context, insecure=not args.secure)
    all_sc = discover()
    by_name = {s.name: s for s in all_sc}

    if args.list or args.scenarios == ["list"]:
        print(f"scenarios in {SCENARIOS_DIR}:")
        for s in all_sc:
            flag = "chaos" if s.has_chaos else "workload"
            print(f"  {s.name:<22} [{flag:<8}] {s.data.get('title', '')}")
        print("  negatives              [builtin ] adapter/endpoint robustness checks (no LLM)")
        return 0

    if args.all:
        selected: list = list(all_sc)
        if not args.skip_negatives:
            selected.append("negatives")
    else:
        selected = []
        for n in args.scenarios:
            if n == "negatives":
                selected.append("negatives")
            elif n in by_name:
                selected.append(by_name[n])
            else:
                print(f"error: unknown scenario {n!r} (try: e2e.py list)", file=sys.stderr)
                return 2
        if not selected:
            print("error: no scenarios given (try: e2e.py --all or e2e.py list)", file=sys.stderr)
            return 2

    # ---- preflight ---------------------------------------------------------
    print(f"preflight: context={args.context} insecure={not args.secure}")
    agent_pod = find_agent_pod(k8s)
    print(f"  agent pod: {agent_pod} (all containers ready)")
    rc, nodes, _ = k8s.run(["get", "nodes", "-o", "name"])
    print(f"  nodes: {nodes.splitlines() or ['?']}")
    chaos_ok = k8s.run(["get", "crd", "podchaos.chaos-mesh.org"])[0] == 0
    if not chaos_ok:
        print("  WARNING: podchaos CRD not found — chaos scenarios will fail (is chaos-mesh installed via Flux?)")

    pf = PortForward(k8s, args.port)
    pf.start()
    print(f"  port-forward: localhost:{args.port} -> svc/{AGENT_SERVICE}")

    results: list[dict] = []
    live_ns: list[str] = []
    meta = {"started_at": utc_iso(), "context": args.context, "agent_pod": agent_pod}
    interrupted = False
    def _sigint(*_):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGINT, _sigint)

        for item in selected:
            if item == "negatives":
                print("\n== negative suite ==")
                results += [{**r, "kind": "negative"} for r in run_negatives(pf)]
                continue

            sc: Scenario = item
            print(f"\n== {sc.name} == {sc.data.get('title', '')}")
            if sc.has_chaos and not chaos_ok:
                results.append({
                    "kind": "scenario", "name": sc.name, "title": sc.data.get("title", ""),
                    "failed": True, "timed_out": False, "likely_cause": None, "confidence": None,
                    "tools": [], "steps": 0, "http": None, "scheduled": None, "fault_state": "skipped",
                    "gate_s": 0, "diag_s": 0, "total_s": 0, "ground_truth": sc.data.get("ground_truth", ""),
                    "expect_tools": sc.data.get("expect_tools"), "runbook": sc.data.get("runbook"),
                    "error": ["chaos-mesh CRDs missing"],
                })
                continue

            t0 = time.time()
            if not sc.no_workload:
                # create the ns first: kustomize applies resources alphabetically,
                # so the Deployment would land before the Namespace otherwise.
                k8s.run(["create", "namespace", sc.ns])  # idempotent enough; error ignored
                rc, _, err = k8s.run(["apply", "-k", str(sc.path)], timeout=180)
                if rc != 0:
                    print(f"  apply FAILED: {err}")
                    results.append({
                        "kind": "scenario", "name": sc.name, "title": sc.data.get("title", ""),
                        "failed": True, "timed_out": False, "likely_cause": None, "confidence": None,
                        "tools": [], "steps": 0, "http": None, "scheduled": None, "fault_state": "apply failed",
                        "gate_s": 0, "diag_s": 0, "total_s": time.time() - t0,
                        "ground_truth": sc.data.get("ground_truth", ""),
                        "expect_tools": sc.data.get("expect_tools"), "runbook": sc.data.get("runbook"),
                        "error": [err[-400:]],
                    })
                    continue
                live_ns.append(sc.ns)
                print(f"  applied {sc.path.name} -> ns {sc.ns}")

            gate = run_gate(k8s, sc)
            gate_s = time.time() - t0
            print(f"  gate: {'OK' if gate['ok'] else 'NOT REACHED'} ({gate['state']}) [{gate_s:.0f}s]")

            pod, node = discover_alert_target(k8s, sc)
            since = utc_iso()
            status, body = pf.post("/webhook/alertmanager", build_alert_payload(sc, pod, node))
            try:
                scheduled = json.loads(body).get("scheduled")
            except json.JSONDecodeError:
                scheduled = None
            print(f"  alert fired: http={status} scheduled={scheduled} pod={pod} node={node}")

            t_diag = time.time()
            diag = collect_diagnosis(k8s, agent_pod, since, args.diag_timeout)
            diag_s = time.time() - t_diag

            if sc.no_workload:
                fault_state = "no fixture (stale-alert regression)"
            elif not gate["ok"]:
                fault_state = f"GATE NOT REACHED: {gate['state']}"
            else:
                fault_state = gate["state"]

            if not args.keep and not sc.no_workload:
                torn = teardown_ns(k8s, sc.ns)
                live_ns.remove(sc.ns)
                print(f"  teardown: ns {sc.ns} {'gone' if torn else 'STILL PRESENT'}")

            results.append({
                "kind": "scenario", "name": sc.name, "title": sc.data.get("title", ""),
                "failed": diag["failed"], "timed_out": diag["timed_out"],
                "likely_cause": diag["likely_cause"], "confidence": diag["confidence"],
                "tools": diag["tools"], "steps": diag["steps"],
                "http": status, "scheduled": scheduled,
                "fault_state": fault_state, "gate_s": gate_s, "diag_s": diag_s,
                "total_s": time.time() - t0,
                "ground_truth": sc.data.get("ground_truth", ""),
                "expect_tools": sc.data.get("expect_tools"), "runbook": sc.data.get("runbook"),
                "error": diag.get("error"),
            })
            if diag["likely_cause"]:
                print(f"  diagnosis ({diag_s:.0f}s): {diag['likely_cause']!r} conf={diag['confidence']}")
            elif diag["failed"]:
                print(f"  agent run FAILED ({diag_s:.0f}s)")
            else:
                print(f"  diagnosis TIMED OUT after {diag_s:.0f}s")
    except KeyboardInterrupt:
        interrupted = True
        print("\ninterrupted — tearing down")
    finally:
        if not args.keep:
            for ns in list(live_ns):
                print(f"  final teardown: ns {ns}")
                teardown_ns(k8s, ns)
        pf.stop()

    residue = {} if interrupted else residue_check(k8s)
    report = write_report(results, residue, meta)
    print(f"\nreport: {report}")
    if interrupted:
        return 130
    failures = [
        r for r in results
        if r.get("kind") == "scenario" and (r["failed"] or r["timed_out"] or r.get("http") != 200)
    ] + [r for r in results if r.get("kind") == "negative" and not r["ok"]]
    if failures:
        print(f"{len(failures)} failing check(s) — see report")
        return 1
    print("all pipeline checks passed (judge cause-match from the report)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
