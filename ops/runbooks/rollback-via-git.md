# Runbook: Roll back a release via Git

**Use when:** a release in staging or production is causing harm and the previous release was healthy.
**Rule:** never `kubectl edit/set image/rollout undo` an Argo CD-managed workload. Argo CD would undo
your change (selfHeal), and the Git history would no longer describe what is running.

## 1. Identify the bad commit (2 min)

```bash
cd gitops-config && git pull
git log --oneline -5 -- environments/production/
# c7d21f0 production: promote secure-event-api 9e41c07a2b55   <- bad
# a41b9e2 production: promote secure-event-api 3f9c2a1b7d4e   <- last good
```

Confirm what Argo CD is actually running:

```bash
argocd app get secure-event-api-production -o json | jq '.status.sync.revision, .status.summary.images'
```

## 2. Revert (1 min)

```bash
git checkout -b rollback/inc-$(date +%Y%m%d-%H%M)
git revert --no-edit c7d21f0
git push -u origin HEAD
gh pr create --title "ROLLBACK production: revert c7d21f0 (INC-xxxx)" --body "Reverts the release causing INC-xxxx." --label rollback
```

The `validate` workflow renders the overlay and shows the diff: it must show only the image tag
(and any config from the bad release) going back.

## 3. Approve and merge (≤5 min)

- A CODEOWNER (`platform-leads`) approves. During an incident the on-call lead is the approver;
  the incident channel is the place to ask for one.
- Squash-merge. **Do not** bypass branch protection; if you must, an org admin uses
  "merge without waiting for requirements" and it is recorded in the RCA.

## 4. Let Argo CD reconcile (1–3 min)

```bash
argocd app get secure-event-api-production --refresh     # don't wait for the 3-min poll
argocd app wait secure-event-api-production --health --timeout 300
kubectl -n secure-event-api-production rollout status deploy/secure-event-api
```

## 5. Verify recovery

- Grafana → *Platform Overview*: error ratio < 1 %, restarts flat, **Running versions** shows only the old build.
- `python ops/platform-doctor/platform_doctor.py --prometheus-url http://localhost:9090` → `OVERALL: OK`.
- Alerts resolve in Alertmanager.

## 6. Follow-up

- Block re-promotion of the bad tag: the fix ships as a **new** image tag (ECR tags are immutable).
- Open the RCA from `ops/incidents/TEMPLATE` within 2 business days.
