# RCA: INC-2026-001, secure-event-api OOM crash loop after release 9e41c07a2b55

> **Simulated exercise.** This incident was staged with `ops/incident-sim` to practise detection,
> diagnosis and Git-based rollback. Times, metrics and evidence come from the exercise run;
> the "defect" is the app's `memory_leak` fault mode standing in for a real code regression.

| | |
|---|---|
| **Severity** | SEV-2 (partial loss of ingestion, no data loss) |
| **Status** | Resolved |
| **Date** | 2026-10-08 |
| **Duration** | 31 min customer impact (14:09–14:40 UTC); 21 min from first alert to recovery |
| **Detected by** | Prometheus alert `SecureEventApiHighErrorRate` → Alertmanager page |
| **Resolved by** | `git revert` of the release commit in `gitops-config`, reconciled by Argo CD |
| **Incident lead** | on-call platform engineer |

## Summary

Release `9e41c07a2b55` of secure-event-api added an in-memory de-duplication cache with no size bound.
Under production traffic each pod's memory grew by ~1 MiB per request until it hit the 384 MiB limit
and was OOM-killed. All three pods entered a crash loop, and at peak 31 % of event submissions failed.
The release was rolled back by reverting its commit in the GitOps repository. No kubectl changes were
made to the workload. Clients retried failed submissions, so no events were lost.

## Impact

- Peak 5xx/connection-error ratio: **31.4 %** of `POST /v1/events` (14:17–14:31).
- p95 latency for successful requests rose from 18 ms to 640 ms (fewer pods serving).
- Staging ran the same build but at ~2 % of production traffic, so its pods had not yet reached the limit.
- No events lost: the API returns an error before acknowledging, and senders retry with back-off.

## Timeline (UTC)

| Time | Event |
|---|---|
| 13:58 | PR *production: promote secure-event-api 9e41c07a2b55* approved by CODEOWNER and merged (`c7d21f0`). |
| 14:03 | Argo CD syncs `secure-event-api-production`; rolling update starts (maxUnavailable 0). |
| 14:04 | Rollout completes. All 3 new pods pass readiness. Argo CD reports **Synced/Healthy**. |
| 14:09 | First `OOMKilled` (container exit 137). Memory panel shows linear climb on all new pods. |
| 14:12 | `PodMemoryNearLimit` (warning) fires. |
| 14:14 | `SecureEventApiHighErrorRate` (critical) fires → on-call paged. `SecureEventApiRestarting` fires. |
| 14:16 | On-call acknowledges, opens Grafana *Platform Overview*: error ratio 22 % and rising; *Running versions* shows only `9e41c07a2b55`; restarts correlate with the 14:03 sync annotation. |
| 14:19 | `ArgoCDAppDegraded` fires (Synced/**Degraded**). |
| 14:22 | `platform-doctor` run (report attached below): CrashLoopBackOff + last state OOMKilled on all new pods, `OOMKilling` kernel events, 97 % of memory limit on the surviving pod. Decision: roll back first, diagnose later. |
| 14:24 | `git revert c7d21f0` → PR #214 *ROLLBACK production*. `validate` shows the only diff is image tag + `FAULT_MODE`. |
| 14:27 | PR approved by platform lead, merged (`e5a90b3`). |
| 14:28 | `argocd app get --refresh`; Argo CD syncs, rolling update to `3f9c2a1b7d4e` begins. |
| 14:31 | Rollout complete, Argo CD **Synced/Healthy**. Error ratio falling. |
| 14:40 | Error ratio < 0.1 % for 5 min; all alerts resolved. Incident closed as mitigated. |

**Time to detect:** 5 min after first OOM (alert `for: 5m`). **Time to mitigate:** 17 min from page.

## Detection: what worked

- The SLO alert (`secure_event_api:http_error_ratio:rate5m > 5 %`) paged within 5 minutes of impact.
- The Grafana *Running versions* panel plus the Argo CD sync annotation made the release correlation obvious in under a minute.
- Kubernetes events gave the mechanism directly: `BackOff`, `Unhealthy` (readiness refused during restart)
  and node-level `OOMKilling` naming the gunicorn process.

## Diagnosis evidence

```text
[CRIT] Pods                   8/11 pods healthy
  - secure-event-api-production/secure-event-api-7f8e6d5c4-a1b2c [secure-event-api]: CrashLoopBackOff (restarts=5, last terminated: OOMKilled)
  - secure-event-api-production/secure-event-api-7f8e6d5c4-d3e4f [secure-event-api]: CrashLoopBackOff (restarts=4, last terminated: OOMKilled)
[CRIT] Argo CD applications   9/10 Synced and Healthy
  - secure-event-api-production: Synced/Degraded rev=c7d21f0 images=secure-event-api:9e41c07a2b55
[WARN] Pod resource usage
  - secure-event-api-production/secure-event-api-7f8e6d5c4-g5h6i: cpu=0.310 cores mem=371.0Mi (97% of 384.0Mi limit)
[WARN] Warning events         59 warnings in the last 30m
  - x23 BackOff: Back-off restarting failed container secure-event-api ...
  - x6  OOMKilling: Memory cgroup out of memory: Killed process 4121 (gunicorn) ... anon-rss:392116kB
```

(Reproduce: `python ops/platform-doctor/platform_doctor.py --fixture ops/platform-doctor/fixtures/incident`.)

Local reproduction after the incident: the defective build grew from 60 MB to 350 MB RSS over
289 requests; the previous build stayed flat at 60 MB under identical load.

## Root cause

The release introduced a cache keyed by event ID with no eviction. Every accepted event retained
roughly 1 MiB, so memory grew linearly with traffic and reached the 384 MiB limit within minutes.

### Five whys

1. **Why did requests fail?** All pods were repeatedly OOM-killed and restarting.
2. **Why were they OOM-killed?** Memory grew without bound with request volume.
3. **Why did memory grow?** The new de-duplication cache had no maximum size or TTL.
4. **Why wasn't it caught before production?** Staging traffic is ~2 % of production; at that rate the
   pods would need ~6 hours to reach the limit, and the release sat in staging for 40 minutes.
5. **Why did the rollout succeed?** Readiness checks only prove a pod *starts* healthy. Nothing
   compared new-version behaviour over time before shifting 100 % of traffic.

### Contributing factors

- No load/soak test in CI that watches memory.
- Promotion is all-at-once. There is no canary step with automated analysis.
- `PodMemoryNearLimit` is a warning and did not page. It fired 2 minutes before customer impact.

## What went well

- Rollback was a reviewed `git revert`; the Git history matches exactly what ran and when.
- Immutable ECR tags meant "previous good version" was unambiguous.
- `maxUnavailable: 0` and the PDB kept one pod serving most of the time (31 % failures, not 100 %).

## Action items

| # | Action | Owner | Priority | Due |
|---|---|---|---|---|
| 1 | Bound the de-dup cache (LRU, 10k entries, 15-min TTL) and add a unit test for the bound | app team | P1 | 2026-10-10 |
| 2 | Add a 10-minute soak test in CI (`loadgen.py` against the built image) that fails if RSS grows > 50 MB | platform | P1 | 2026-10-17 |
| 3 | Adopt Argo Rollouts canary for production (10 % → 50 % → 100 %) with a Prometheus analysis on error ratio and memory slope | platform | P2 | 2026-11-07 |
| 4 | Require ≥ 2 h staging soak before `promote-to-production` can run (check in the workflow) | platform | P2 | 2026-10-24 |
| 5 | Page on `PodMemoryNearLimit` when restarts also increase (combine into one critical alert) | platform | P3 | 2026-10-17 |
