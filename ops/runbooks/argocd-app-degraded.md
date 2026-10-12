# Runbook: Argo CD application OutOfSync / Degraded / sync failing

**Alerts:** `ArgoCDAppDegraded`, `ArgoCDAppOutOfSync`, `ArgoCDSyncFailing`

```bash
argocd app get <app>                       # tree with health per resource
argocd app diff <app>                      # what differs between Git and the cluster
argocd app history <app>                   # which Git revisions were deployed when
```

| State | Usual cause | Action |
|---|---|---|
| **Synced / Degraded** | Git was applied fine but workloads are unhealthy (crash loop, stuck rollout) | Treat as an app incident → [pod-crashloop-oom.md](pod-crashloop-oom.md); roll back via Git if release-related |
| **OutOfSync / Healthy**, self-heals repeatedly | Someone/something edits live objects (manual kubectl, a controller mutating fields) | `argocd app diff`; find the actor in the audit log; add `ignoreDifferences` only for legitimate controllers (e.g. HPA-managed `replicas`) |
| **OutOfSync**, sync **failed** | Manifest invalid, CRD missing, AppProject denies the kind/namespace | `argocd app get <app> --show-operation`; fix in Git, never in the cluster |
| **Progressing** > 10 min | Rollout blocked (image pull, quota, PDB) or SecureService claim not Ready | Check the resource marked Progressing in the tree |
| **Missing** | Namespace/resources deleted out of band | Argo CD recreates them on next sync; investigate who deleted them |

Production auto-syncs with `selfHeal: true` but `prune: false`, so resources removed from Git are
reported as OutOfSync instead of being deleted. Prune deliberately:
`argocd app sync secure-event-api-production --prune` after review.
