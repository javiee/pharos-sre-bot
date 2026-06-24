# Runbook: High Latency on Webserver

## Symptom

A webserver / API is responding slowly. Request latency (p95 / p99) is elevated above
its normal baseline, and users or upstream services report timeouts or sluggish
responses. Error rates may also rise as requests start timing out.

## Likely causes

- The service is CPU-throttled or out of CPU headroom under load.
- A slow downstream dependency (database, cache, external API) is blocking request handlers.
- Traffic spike: request rate increased beyond what the current replicas can serve.
- Too few replicas / no autoscaling, so each pod is saturated.
- Garbage collection, connection-pool exhaustion, or lock contention inside the app.

## Diagnosis steps

1. In Grafana, look at the service's latency panel and confirm which quantile rose and when:
   - p95 latency: `histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{service="<svc>"}[5m])) by (le))`
   - Compare against the same metric a day/week earlier to confirm it is abnormal.
2. Check request rate to see if this is a traffic spike:
   - `sum(rate(http_requests_total{service="<svc>"}[5m]))`
3. Check error rate alongside latency:
   - `sum(rate(http_requests_total{service="<svc>",status=~"5.."}[5m]))`
4. Check resource saturation of the pods:
   - CPU: `rate(container_cpu_usage_seconds_total{pod=~"<svc>-.*"}[5m])` vs the CPU limit
   - CPU throttling: `rate(container_cpu_cfs_throttled_periods_total{pod=~"<svc>-.*"}[5m])`
5. If latency is high but CPU is low, suspect a slow dependency — check the downstream
   service's latency dashboard or DB query times.

## Remediation

- Saturation / traffic spike: scale up replicas (or enable/raise HPA), or raise CPU limits if throttled.
- Slow dependency: address the downstream (slow query, cold cache, dependency incident) rather than the webserver itself.
- Connection-pool / GC issues: tune pool size or memory settings and redeploy.
- Shed load temporarily (rate limit) if the spike is abusive or unexpected.

## Escalation

If latency stays high after scaling and no single dependency stands out, escalate to the
owning service team with the time window, the affected quantile, and links to the
latency, request-rate, and CPU Grafana panels.
