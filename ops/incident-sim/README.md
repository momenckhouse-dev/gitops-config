# Incident exercise: defective release → detect → diagnose → roll back via Git → RCA

Run this in **staging** first. It takes about 30 minutes and produces the evidence for an RCA like
[INC-2026-001](../incidents/INC-2026-001-secure-event-api-oom.md).

## 0. Baseline

```bash
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80 &
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090 &
kubectl -n secure-event-api-staging port-forward svc/secure-event-api 8080:80 &

python ops/platform-doctor/platform_doctor.py --prometheus-url http://localhost:9090 \
  --format markdown --output baseline.md          # expect OVERALL: OK
python ops/incident-sim/loadgen.py --url http://localhost:8080 --rps 20 --duration 1800 &
```

Open Grafana → *Platform / Platform Overview*, namespace = `secure-event-api-staging`. Note the flat
memory line and ~0 % error ratio. Write down the start time.

## 1. Ship the defective release (the GitOps way)

```bash
cd gitops-config
git checkout -b release/staging-9e41c07a2b55
python ops/incident-sim/inject_defective_release.py --gitops-dir . --env staging
git add environments/ && git commit -m "staging: deploy secure-event-api 9e41c07a2b55"
git push -u origin HEAD && gh pr create --fill && gh pr merge --squash
```

Argo CD syncs within 3 minutes (or `argocd app get secure-event-api-staging --refresh`).

## 2. Detect (do not look at the change yet; play the on-call)

Within ~5–10 minutes:

- Grafana: *memory vs limit* climbs linearly to 100 %, *Container restarts* steps up, the
  *Error ratio* jumps and the *Running versions* table shows `9e41c07a2b55` with `fault_mode=memory_leak`.
- Alerts: `PodMemoryNearLimit` → `SecureEventApiRestarting` → `SecureEventApiHighErrorRate` → `ArgoCDAppDegraded`.
- `loadgen` output shows `500`/`conn-error` counts appearing.

## 3. Diagnose with Kubernetes events

```bash
python ops/platform-doctor/platform_doctor.py --prometheus-url http://localhost:9090 --output during.md
kubectl -n secure-event-api-staging get events --sort-by=.lastTimestamp | tail -20
kubectl -n secure-event-api-staging describe pod -l app.kubernetes.io/name=secure-event-api | grep -A3 "Last State"
kubectl get events -A --field-selector reason=OOMKilling
```

Expected: `BackOff`, `Unhealthy` (readiness refused while restarting), `Last State: OOMKilled (137)`, node `OOMKilling`.

## 4. Roll back via Git (not kubectl)

Follow [rollback-via-git.md](../runbooks/rollback-via-git.md):

```bash
git checkout main && git pull
git checkout -b rollback/staging-9e41c07a2b55
git revert --no-edit <merge-commit-of-step-1>
git push -u origin HEAD && gh pr create --fill --label rollback && gh pr merge --squash
argocd app wait secure-event-api-staging --health --timeout 300
```

Try a `kubectl set image` on the deployment before the revert if you want to see selfHeal undo
manual drift within seconds; that demonstrates why rollback has to go through Git.

## 5. Verify and write the RCA

```bash
python ops/platform-doctor/platform_doctor.py --prometheus-url http://localhost:9090 --output after.md   # OK
```

Copy [TEMPLATE.md](../incidents/TEMPLATE.md), fill the timeline from Argo CD history
(`argocd app history`), Alertmanager and your notes, and attach `during.md`.

## Offline rehearsal (no cluster)

```bash
python ops/platform-doctor/platform_doctor.py --fixture ops/platform-doctor/fixtures/incident
python ops/platform-doctor/platform_doctor.py --fixture ops/platform-doctor/fixtures/healthy
```
