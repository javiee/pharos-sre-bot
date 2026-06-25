# Runbook: Pod in CrashLoopBackOff

## Symptom

A pod repeatedly starts, crashes, and restarts. `kubectl get pod` shows status
`CrashLoopBackOff` and a rising `RESTARTS` count. Kubernetes backs off (waits longer)
between each restart attempt, so the pod never becomes Ready.

## Likely causes

- The application exits with a non-zero code on startup (bad config, missing env var, failed migration).
- A missing or unreachable dependency at boot (database, secret, config map, downstream service).
- The container is OOMKilled — it exceeds its memory limit and is killed, then restarts.
- A failing liveness probe restarts the container before it finishes starting up.
- A bad image or wrong entrypoint/command.

## Diagnosis steps

1. Confirm the state and restart count: `kubectl get pod <pod> -n <ns>`.
2. Read the crash reason: `kubectl describe pod <pod> -n <ns>` and look at the
   `Last State` (e.g. `Error`, `OOMKilled`) and the `Events` section.
3. Read the logs of the previous (crashed) container: `kubectl logs <pod> -n <ns> --previous`.
4. In Grafana, check the container restart and memory panels for the workload:
   - Restarts: `kube_pod_container_status_restarts_total{pod="<pod>"}`
   - Memory vs limit: `container_memory_working_set_bytes{pod="<pod>"}` against
     `kube_pod_container_resource_limits{pod="<pod>",resource="memory"}`
   - If memory tracks up to the limit right before each restart, it is an OOM loop.
5. Check whether a liveness probe is firing too early (probe events appear in `describe`).

## Remediation

- OOM: raise the memory limit, or fix the leak / reduce memory usage.
- Bad config / missing dependency: fix the env var, secret, or config map and redeploy.
- Probe too aggressive: increase `initialDelaySeconds` / `failureThreshold` on the liveness probe.
- Bad image or command: roll back to the last known-good image or correct the entrypoint.

## Escalation

If the cause is not clear from logs, events, and Grafana within a few restart cycles,
escalate to the owning service team with the pod name, namespace, and the `--previous`
logs attached.
