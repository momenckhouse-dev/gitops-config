# RCA: INC-2026-001, secure-event-api OOM crash loop in staging after enabling an in-memory cache

> **Planned exercise, real data.** The defect was injected on purpose with
> [`ops/incident-sim`](../incident-sim/README.md) (the app's `memory_leak` fault mode stands in for an
> unbounded cache). Everything below was measured on the live EKS cluster on 2026-10-12: alert times
> from Prometheus, deploy times from Argo CD history, and client impact from the load generator's log.

| | |
|---|---|
| **Severity** | SEV-3: staging only, synthetic traffic, production unaffected |
| **Status** | Resolved |
| **Date** | 2026-10-12 |
| **Duration** | 23 min client impact (17:41–18:04 UTC); 21 min from first alert to recovery |
| **Detected by** | Prometheus alerts `SecureEventApiRestarting` (critical) and `ContainerOOMKilled`, 2.5 min after rollout |
| **Resolved by** | `git revert` of the release commit (PR #6), reconciled by Argo CD |
| **Incident lead** | Momen Khouse (platform on-call) |

## Summary

PR #5, "staging: enable in-memory event cache", turned on a cache that kept about 1 MiB per request and
never released it. After Argo CD synced it, both staging pods filled their 256 MiB memory limit within
about two minutes, were OOMKilled (exit code 137), and entered a crash loop. For 23 minutes, 10.2 % of
client requests failed, peaking at 100 % in the minutes when both pods were down at once. The release
was rolled back by reverting its commit in Git; no `kubectl` change was used to fix the workload.

## Impact

| Measure | Value | Source |
|---|---|---|
| Client requests in the impact window | 4,127 | loadgen log, 17:41–18:04 |
| Failed requests | 419 (10.2 %), all connection failures (`status=000`) | loadgen log |
| Worst minutes | 17:52, 17:55, 17:56: 100 % failed | loadgen log |
| Server-reported 5xx ratio | **0 % throughout** | Prometheus `http_requests_total` |
| Restarts | 6 per pod in 18 minutes | platform-doctor |
| Data loss | None for accepted events: the app writes to S3 before it answers `201`. The 419 failed events were never acknowledged; real senders must retry (the load generator does not). | app design, S3 |
| Production | Not affected: the change only touched the staging overlay | Git diff |

## Timeline (UTC)

| Time | Event |
|---|---|
| 17:30:15 | PR #4 merged (monitoring config, unrelated; ruled out because it touched no workload) |
| 17:34:20 | Load generator started: 100 % `201` (baseline) |
| 17:35:13 | **PR #5 merged**: `FAULT_MODE=memory_leak` in the staging overlay; CI passed |
| 17:41:23 | **Argo CD synced `b296105`**; new ReplicaSet `68b4ccc4dd` rolls out. First client failures |
| 17:43:46 | 🔔 `SecureEventApiRestarting` firing (critical) |
| 17:43:53 | 🔔 `ContainerOOMKilled` firing for both pods |
| 17:45:30 | First moment with fewer ready replicas than desired; pods start flapping |
| 17:47 | On-call starts investigating: Grafana *memory vs limit* shows a saw-tooth to 100 % |
| ~17:53 | Diagnosis: `describe` shows `OOMKilled`, exit 137, `FAULT_MODE=memory_leak`; Argo CD history ties onset to `b296105` |
| 17:55:30–17:58:00 | Full outage: 0 of 2 replicas ready |
| 17:59:58 | `platform-doctor` snapshot: OVERALL CRIT (below) |
| ~18:01 | **PR #6 merged**: `git revert` of `b296105` |
| 18:04:43 | **Argo CD synced the revert** (`cea5aa5`); healthy ReplicaSet `789c8f6f6f` back |
| 18:04 | Last failed request; 100 % `201` from 18:05 |

## Detection

- **Worked:** restart and OOM alerts fired 2.5 minutes after the rollout. Grafana's *memory vs limit*
  panel made the leak obvious at a glance.
- **Did not work:** `SecureEventApiHighErrorRate` never fired. An OOMKilled pod does not answer with a
  500; it disappears and the client's connection fails. The server-side 5xx ratio stayed at 0 % while
  clients saw 10 % failures. The dashboard's *Error ratio* panel also showed "No data" instead of 0,
  which looks like a broken panel at the moment you need to trust it.
- **Flapping defeats `for:`.** Pods cycled OOM → restart → briefly Ready → OOM, so the ready-replica
  count kept bouncing between 0, 1 and 2. A rule like "below desired replicas `for: 5m`" would never
  have fired, because every brief recovery resets the timer.

## Diagnosis evidence

```text
$ kubectl -n secure-event-api-staging describe pods -l app.kubernetes.io/name=secure-event-api
    Reason:       CrashLoopBackOff
    Last State:   Terminated
      Reason:     OOMKilled
      Exit Code:  137
    Limits:   memory: 256Mi
    Requests: memory: 128Mi
      FAULT_MODE:  memory_leak
```

```text
platform-doctor  |  2026-10-12T17:59:58Z  |  OVERALL: CRIT
[OK  ] Nodes                  3/3 nodes Ready
[CRIT] Pods                   42/44 pods healthy
         - secure-event-api-staging/secure-event-api-68b4ccc4dd-7nsmz: CrashLoopBackOff (restarts=6, last terminated: OOMKilled)
         - secure-event-api-staging/secure-event-api-68b4ccc4dd-bqgdj: CrashLoopBackOff (restarts=6, last terminated: OOMKilled)
[CRIT] Deployments            19/20 deployments fully available
         - secure-event-api-staging/secure-event-api: 0/2 available, 2/2 updated
[CRIT] Argo CD applications   9/10 Synced and Healthy
         - secure-event-api-staging: Synced/Degraded rev=b296105
[OK  ] SecureService claims   2/2 archives ready
[CRIT] Prometheus alerts      3 firing (SecureEventApiRestarting, ContainerOOMKilled x2)
[SKIP] Pod resource usage     metrics unavailable (metrics-server not installed)
[WARN] Warning events         1560 warnings in the last 30m
         - x753 per namespace  HorizontalPodAutoscaler FailedGetResourceMetric (no metrics-server)
         - x27 / x25           BackOff: Back-off restarting failed container
         - x2                  Unhealthy: Readiness probe failed: connection refused
```

## Root cause

### Five whys

1. **Why did requests fail?** Both pods were repeatedly OOMKilled and in CrashLoopBackOff, so for
   long stretches there was no ready replica behind the Service.
2. **Why were they OOMKilled?** Each request kept about 1 MiB in process memory. At about 2 requests per
   second per pod, the 256 MiB limit was reached in roughly two minutes.
3. **Why did memory grow?** The release enabled an in-memory cache with no size bound or eviction.
4. **Why did it reach staging?** CI checks structure (YAML, Kustomize render, schemas), not runtime
   behaviour. Nothing exercised the new build under load before or after the sync.
5. **Why did recovery need a human?** Argo CD marks the app Degraded but does not roll back, and there
   is no post-deploy check that would gate or revert a bad release.

### Contributing factors

- The error-rate SLO alert measures only server-reported errors, so it was blind to this failure mode.
- Argo CD polls Git every ~3 minutes: 6 minutes from merge to deploy for the release, and about 3.5
  minutes for the revert. That added latency to both the impact and the fix.
- metrics-server is not installed: the HPA could not act (753 `FailedGetResourceMetric` warnings per
  namespace in 30 minutes), and that noise buried the useful `BackOff` events. Scaling would not have
  fixed a leak, but the noise slowed reading the events.
- Staging runs 2 replicas, both on the same defective build, so there was no healthy capacity left.

## What went well

- Alerts fired 2.5 minutes after the rollout, and the dashboard showed the leak's signature immediately.
- Argo CD's deploy history pointed straight at the responsible commit.
- The fix was a reviewed, auditable `git revert`; recovery came about 3.5 minutes after the merge.
- Archive-before-acknowledge meant no accepted event was lost.
- GitOps limited the blast radius to staging; production was never touched.

## Action items

| # | Action | Owner | Priority | Status |
|---|---|---|---|---|
| 1 | Alert on capacity, not only server errors: `SecureEventApiNoReadyReplicas` (0 ready for 1m) and `SecureEventApiDegradedCapacity` (10-minute average under 75 % of desired, which survives flapping). Replayed against this incident's data, they would have fired at 17:56:30 and 17:53:30. | Momen | High | Done in this PR |
| 2 | Fix the *Error ratio (5xx)* panel to show 0 instead of "No data" (`or vector(0)`) | Momen | Medium | Done in this PR |
| 3 | Add a post-sync smoke and soak check in staging (an Argo CD PostSync Job: short load, then assert no restarts and flat memory) and require it before production promotion | Momen | High | Open |
| 4 | Install metrics-server so the HPA, `kubectl top` and platform-doctor's resource check work, and the warning-event noise goes away | Momen | Medium | Open |
| 5 | Add a GitHub webhook to Argo CD to cut merge-to-deploy time from minutes to seconds (faster rollouts and faster rollbacks) | Momen | Medium | Open |
| 6 | Evaluate Argo Rollouts with Prometheus analysis for automatic rollback on restart or OOM signals | Momen | Low | Open |
| 7 | Exercise tooling: the inject script defaulted to an image tag that does not exist in ECR (it would have caused ImagePullBackOff instead of an OOM); it now keeps the current tag. Load now runs inside the cluster, because a port-forward dies with the first OOMKilled pod. | Momen | Low | Done in this PR |
