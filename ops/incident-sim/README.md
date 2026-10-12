# Incident exercise: defective release → detect → diagnose → roll back via Git → RCA

Run this in **staging**. It takes about 30 minutes and produces the evidence for an RCA like
[INC-2026-001](../incidents/INC-2026-001-secure-event-api-oom.md), which is the write-up of a real run.

## 0. Baseline

```bash
kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80 &
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090 &

python ops/platform-doctor/platform_doctor.py --prometheus-url http://localhost:9090   # expect OVERALL: OK
kubectl apply -f ops/incident-sim/loadgen-pod.yaml        # ~4 req/s from inside the cluster
kubectl -n secure-event-api-staging logs loadgen --tail 5  # status=201
```

Generate load from inside the cluster, not through a laptop port-forward: a port-forward pins one pod
and dies the first time that pod is OOMKilled. (`loadgen.py` is fine for steady-state load tests.)

Open Grafana → *Platform / Platform Overview*, namespace = `secure-event-api-staging`. Note the flat
memory line and write down the start time.

## 1. Ship the defective release (the GitOps way)

```bash
git checkout main && git pull
git checkout -b release/staging-event-cache
python ops/incident-sim/inject_defective_release.py --gitops-dir . --env staging
git add environments/ && git commit -m "staging: enable in-memory event cache"
git push -u origin HEAD && gh pr create --fill && gh pr merge --auto --squash
```

The script keeps the current image tag and turns on `FAULT_MODE=memory_leak`. If you pass `--tag`, it
must already exist in ECR, or you will be debugging `ImagePullBackOff` instead of an OOM.
The PR passes CI: the YAML is valid, and CI does not test runtime behaviour. That is part of the lesson.

## 2. Detect (play the on-call; do not look at the change yet)

Expect, within about 2–10 minutes of the Argo CD sync:

- Grafana: *memory vs limit* saw-tooths to 100 %, *Container restarts* steps up, *Running versions*
  shows `fault_mode=memory_leak`. The *Error ratio (5xx)* stays at 0: a killed pod returns no 500s.
- Alerts: `ContainerOOMKilled`, `SecureEventApiRestarting`, then `KubePodCrashLooping`,
  `SecureEventApiDegradedCapacity`, `SecureEventApiNoReadyReplicas` and `ArgoCDAppDegraded`.
- `kubectl -n secure-event-api-staging logs loadgen` shows `status=000` (connection failures).

## 3. Diagnose

```bash
python ops/platform-doctor/platform_doctor.py --prometheus-url http://localhost:9090 --output during.md
kubectl -n secure-event-api-staging describe pods -l app.kubernetes.io/name=secure-event-api | grep -A3 "Last State"
kubectl -n secure-event-api-staging get events --sort-by=.lastTimestamp | tail -20
kubectl -n argocd get application secure-event-api-staging -o jsonpath='{.status.history[-1].revision}'
```

Expected: `Last State: Terminated, Reason: OOMKilled, Exit Code: 137`, `BackOff` and `Unhealthy` events,
and the Argo CD revision matching the release commit.

Optional: try `kubectl -n secure-event-api-staging set env deployment/secure-event-api FAULT_MODE=none`
and watch Argo CD's selfHeal put `memory_leak` back. That is why the fix has to go through Git.

## 4. Roll back via Git (not kubectl)

Follow [rollback-via-git.md](../runbooks/rollback-via-git.md):

```bash
git checkout main && git pull
git checkout -b rollback/staging-event-cache
git revert --no-edit <merge-commit-of-step-1>
git push -u origin HEAD && gh pr create --fill && gh pr merge --auto --squash
```

## 5. Verify, clean up, write the RCA

```bash
python ops/platform-doctor/platform_doctor.py --prometheus-url http://localhost:9090 --output after.md
kubectl -n secure-event-api-staging logs loadgen > loadgen.log   # client-side impact numbers
kubectl delete -f ops/incident-sim/loadgen-pod.yaml
```

Copy [TEMPLATE.md](../incidents/TEMPLATE.md). Build the timeline from Argo CD history
(`.status.history`), alert `activeAt` times and the loadgen log.

## Offline rehearsal (no cluster)

```bash
python ops/platform-doctor/platform_doctor.py --fixture ops/platform-doctor/fixtures/incident
python ops/platform-doctor/platform_doctor.py --fixture ops/platform-doctor/fixtures/healthy
```
