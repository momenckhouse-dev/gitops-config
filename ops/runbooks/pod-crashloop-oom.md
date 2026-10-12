# Runbook: Pods crash-looping / OOMKilled

**Alerts:** `SecureEventApiRestarting`, `ContainerOOMKilled`, `PodMemoryNearLimit`, `KubePodCrashLooping`

## Triage (5 min)

```bash
NS=secure-event-api-production
kubectl -n $NS get pods -o wide
kubectl -n $NS get events --sort-by=.lastTimestamp | tail -20
kubectl -n $NS describe pod <pod> | sed -n '/Last State/,/Ready/p'
kubectl get events -A --field-selector reason=OOMKilling     # node-level kernel OOM kills
kubectl -n $NS logs <pod> --previous --tail=50
```

Or in one go: `python ops/platform-doctor/platform_doctor.py --events-window 30`.

| What you see | Meaning | Next step |
|---|---|---|
| `Last State: Terminated, Reason: OOMKilled, Exit Code: 137` | Container exceeded its memory **limit** | Check memory graph: steady climb = leak, step = undersized limit |
| `CrashLoopBackOff` + exit code 1 | App crashed on start | `logs --previous` for the stack trace |
| `Readiness probe failed: connection refused` right after restarts | Pod is restarting, not a network issue | Follow the restart cause |
| `FailedScheduling: Insufficient memory` | Nodes are full | Check cluster memory, node group scaling |

## Decide

1. **Did this start with a release?** Grafana *Running versions* panel, or
   `git log -3 -- environments/<env>/`. If yes, and the previous version was healthy:
   **roll back via Git** → [rollback-via-git.md](rollback-via-git.md). Diagnose afterwards.
2. **No release, memory climbing over hours:** the leak predates the latest deploy, or traffic changed.
   Raising the limit is a temporary mitigation, and it also goes through Git (production overlay
   `patches/deployment-resources.yaml`) with a PR.
3. **Node-level memory pressure:** see `node-not-ready.md`.

## Don't

- `kubectl delete pod` in a loop. It hides the signal and changes nothing.
- `kubectl set resources` / `kubectl edit`. Argo CD reverts it within seconds (selfHeal).
