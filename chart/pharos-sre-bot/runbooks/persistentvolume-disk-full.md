# Runbook: PersistentVolume Almost Full / Disk Pressure

## Symptom

A pod's PersistentVolumeClaim (PVC) is running out of space, or a node reports
`DiskPressure`. Applications start failing writes with "no space left on device",
databases go read-only, log writes fail, or pods get evicted from a node under disk
pressure. `kubectl describe node` shows a `DiskPressure` condition set to `True`.

## Likely causes

- A volume filled up with data growth, large logs, or temp files that are never cleaned.
- A log file or cache grows unbounded inside the container's writable layer or an emptyDir.
- The PVC was provisioned too small for the workload's real data footprint.
- Snapshots, WAL files, or backups accumulating on the same volume.
- Node ephemeral storage (not a PVC) exhausted by image layers or container logs.

## Diagnosis steps

1. Identify the affected PVC and pod: `kubectl get pvc -n <ns>` and `kubectl describe pod <pod> -n <ns>`.
2. Check node conditions for disk pressure: `kubectl describe node <node>` → `Conditions` section.
3. In Grafana, check volume free space and usage trend for the PVC:
   - Free bytes: `kubelet_volume_stats_available_bytes{persistentvolumeclaim="<pvc>"}`
   - Used fraction: `1 - (kubelet_volume_stats_available_bytes / kubelet_volume_stats_capacity_bytes)`
   - A steadily rising used fraction approaching 1.0 confirms the volume is filling.
4. For node ephemeral storage, check `node_filesystem_avail_bytes` per node and mountpoint.
5. Look at the growth rate to estimate time-to-full and whether it is a slow leak or a sudden spike.

## Remediation

- Immediate: free space — rotate/delete old logs, clear temp/cache files, prune old snapshots or backups.
- Expand the volume: increase the PVC size if the StorageClass allows volume expansion (`allowVolumeExpansion: true`), then let the filesystem grow.
- Fix the root cause: add log rotation, cap cache size, or move large data off the hot volume.
- Node disk pressure: prune unused images (`kubelet` image GC) and rotate container logs; evicted pods will reschedule once pressure clears.

## Escalation

If the volume cannot be expanded and space cannot be safely reclaimed, escalate to the
owning service team and the storage/platform team with the PVC name, namespace, node,
current free bytes, and the usage-trend Grafana panel.
