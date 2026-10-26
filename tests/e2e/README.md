# E2E fault-injection scenarios for the troubleshooting agent

On-demand fault scenarios for testing how well the deployed `pharos-sre-bot`
agent diagnoses real workload problems. Each scenario folder is self-contained:

```
tests/e2e/<scenario>/
  kustomization.yaml   # namespace tsa-<scenario> + resources
  workload.yaml        # the victim workload(s)
  chaos.yaml           # Chaos Mesh CR (only for chaos scenarios)
  scenario.yaml        # wait gate, alert payload, ground truth, expected tools
```

- **Chaos scenarios** (StressChaos / NetworkChaos): external disruptions —
  OOM, CPU saturation, network delay, network partition.
- **Workload-fault scenarios**: defects in the workload itself (crash loop via
  bad startup validation, bad image, missing ConfigMap, filling PVC) — no
  chaos type expresses these.
- **stale-alert**: no fixtures; regression case for alerts about pods that no
  longer exist.

## Prerequisites

- The agent is deployed and healthy: `kubectl -n monitoring get pods -l app.kubernetes.io/name=pharos-sre-bot`
- Chaos Mesh is installed **via Flux** (`gitops/apps/chaos-mesh` in lighthouse-ops):
  `kubectl -n chaos-mesh get pods` → controller, daemon, dashboard Running;
  `kubectl get crd podchaos.chaos-mesh.org`.

## Running

```bash
# from the repo root
uv run python scripts/e2e.py list                 # show scenarios
uv run python scripts/e2e.py oom                   # one scenario
uv run python scripts/e2e.py oom cpu-stress        # several, in order
uv run python scripts/e2e.py --all                 # everything + negative suite
uv run python scripts/e2e.py --all --skip-negatives
uv run python scripts/e2e.py cpu-stress --keep     # keep fixtures for manual inspection
```

The runner, per scenario: applies the fixtures → waits for the fault state →
posts an Alertmanager webhook to the agent → collects the diagnosis from the
agent logs → tears the namespace down. Scenarios run strictly **sequentially**
(the agent's `diagnosis complete` log line carries no alert id, so one alert in
flight at a time is the reliable correlation scheme). It also needs the shared
LLM endpoint, so keep runs sequential.

A report lands in `reports/e2e-<timestamp>.md` (+ `.json`). The runner scores
the **pipeline** (200, diagnosis arrived, steps ≤ max, no unhandled failure)
and records tools/chunks; **cause-match is judged by you** — compare
*ground truth* vs *agent likely_cause* in the details section.

## Manual run (no runner)

```bash
NS=tsa-oom
kubectl create ns $NS                        # first (kustomize applies D before N)
kubectl apply -k tests/e2e/oom/              # fault starts
kubectl -n $NS get pods -w                           # wait for OOMKilled
curl -s -X POST localhost:7070/webhook/alertmanager -H 'Content-Type: application/json' -d '{
  "status":"firing",
  "alerts":[{
    "status":"firing",
    "labels":{"alertname":"PodOOMKilling","severity":"warning","namespace":"'"$NS"'","pod":"<pod-name>"},
    "annotations":{"summary":"Pod is at risk of running out of memory"},
    "startsAt":"2026-01-01T00:00:00.000Z"
  }]}'
kubectl -n monitoring logs deploy/pharos-sre-bot -c agent -f --since=2m   # watch for "diagnosis complete"
kubectl delete -k tests/e2e/oom/                    # fault stops, cleanup
```

The runner uses a private port-forward (`--port`, default 17070); a manual
`kubectl port-forward svc/pharos-sre-bot 7070:7070` does not collide.

## Chaos Mesh dashboard

Enabled (HelmRelease values). ClusterIP only — no ingress by cluster design:

```bash
kubectl -n chaos-mesh port-forward svc/chaos-dashboard 3000:3000
# → http://localhost:3000 (verify actual svc name/port against chart 2.8.6)
```

Note: the dashboard has no built-in auth — it is a chaos admin UI, so keep it
port-forward-only (do not expose it via ngrok).

## Safety model

- Every scenario runs in its own `tsa-*` namespace. Pod-targeting Chaos CRs can
  only select pods **in their own namespace**, so no scenario can touch
  `monitoring`, `flux-system`, etc.
- All stress targets carry CPU/memory limits (worst case: the victim's cgroup
  absorbs the stress; node impact is bounded).
- NetworkChaos scenarios use no `externalTargets` (the one knob that reaches
  outside the namespace).
- Teardown deletes the namespace (workload + chaos CRs + PVC). A Ctrl-C also
  triggers teardown; the runner finishes with a residue sweep
  (`tsa-*` namespaces + `podchaos/stresschaos/networkchaos -A`).

## Scenario table

| scenario | fault | mechanism | wait gate | runbook |
|---|---|---|---|---|
| crashloop | CrashLoopBackOff | workload fault: app exits non-zero at startup | phase=CrashLoopBackOff | crashloopbackoff |
| oom | OOMKilled | `StressChaos` memory 36M (32Mi-limit victim) | lastState=OOMKilled | crashloopbackoff (OOM) |
| cpu-stress | CPU saturation / throttling | `StressChaos` CPU (250m-limit victim) | ready + 150s settle | high-latency-webserver (CPU) |
| network-delay | slow downstream dependency | `NetworkChaos` delay 800ms api→backend | both ready + 60s settle | high-latency-webserver (dependency) |
| network-partition | unreachable dependency | `NetworkChaos` partition (backend) | **api** ready + 45s settle | none (signal-only) |
| image-pullbackoff | ImagePullBackOff | workload fault: nonexistent image | waiting=ImagePullBackOff | none (RAG gap) |
| missing-configmap | CreateContainerConfigError | workload fault: missing ConfigMap | waiting=CreateContainerConfigError | crashloopbackoff (dependency) |
| pvc-filling | PVC nearly full | workload fault: 20Mi PVC + 1Mi/s filler | df available ≤ 3Mi | persistentvolume-disk-full |
| stale-alert | (regression) nonexistent pod | none | — | crashloopbackoff |

## Gotchas (learned the hard way, verified on the live cluster)

- **`network-delay` needs the headless Service.** Chaos Mesh builds the chaos
  ipset from the target's **pod IP**, but traffic to a ClusterIP is DNATed in
  the **node** netns — the source pod's egress is never addressed to the pod
  IP, so the ipset match (and thus the delay) can never fire. `clusterIP: None`
  makes DNS resolve straight to the pod IP. (Without it the CR reports
  `AllInjected=True` and everything is in place inside the pod netns — the
  iptables counters just never move. Very convincing false positive.)
- **`network-partition` deliberately gates on the api only.** The partition
  drops ALL egress from the backend, which also breaks the backend's own
  readiness probes (SYN-ACKs never leave) → the backend flips `0/1` while
  staying `Running` with no restarts. That is expected and part of the
  diagnosis signal. Its Service is a **regular ClusterIP** on purpose: it
  doesn't need pod-IP matching, and the name stays resolvable when the
  backend is NotReady (the failure then looks like a classic unreachable
  dependency, not a DNS error).
- **`crashloop` is a workload fault, not PodChaos.** `PodChaos pod-failure`
  is one-shot (injectedCount=1, process gets SIGKILL'd and the container
  exits 0) — it cannot hold a crash loop. The fixture instead runs an app
  that validates its config at startup and exits non-zero.
- **Chaos Mesh 2.8.6 schema on this cluster:** CRDs serve `v1alpha1`;
  `duration` needs a unit (`"600s"`); `StressChaos` has **no** `spec.action`
  and **requires** `spec.mode: all`; `NetworkChaos` targets go in `spec.target`
  (a full PodSelector: `mode` + `selector`), not `destination`.
- **k3s daemon prerequisites** (done in lighthouse-ops, without them *every*
  network/stress injection silently no-ops or fails): the chaos-daemon must
  run with `--runtime containerd` + the k3s socket symlink, and the daemon's
  `PATH` must be prepended with `/var/run/chaos-shim` (shims exec
  `iptables-nft` — the image hardcodes legacy iptables, which cannot work on
  the nftables-backend kernel). See `gitops/apps/chaos-mesh/` in
  lighthouse-ops (`deploy.yaml` + `chaos-daemon-node-fix.yaml`).
- **Manual `kubectl apply -f chaos.yaml` without `-n`** lands in the
  kubeconfig's default namespace (`hermes-agent` here), not the fixture's —
  always apply via `-k` (the kustomization pins the namespace) or with `-n`.
