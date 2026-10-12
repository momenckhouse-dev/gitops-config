# Runbook: secure-event-api high error rate / latency

**Alerts:** `SecureEventApiHighErrorRate` (>5 % 5xx for 5m), `SecureEventApiHighLatency` (p95 > 500 ms for 10m),
`SecureEventApiArchiveFailures`

## Triage

1. Grafana → *Platform Overview* → `namespace` = affected env.
   - **Requests/s by status**: which codes? `500` = app bug, `503` = archive (S3) unavailable,
     drops in total RPS with connection errors = pods restarting.
   - **Running versions**: more than one row, or a new commit, means a rollout correlates.
   - **Memory vs limit / Container restarts**: restarts make errors → follow
     [pod-crashloop-oom.md](pod-crashloop-oom.md).
2. Raw numbers if Grafana is down:

   ```promql
   secure_event_api:http_error_ratio:rate5m
   sum by (status) (rate(http_requests_total{job="secure-event-api",namespace="secure-event-api-production"}[5m]))
   ```

3. `503`s with `events_archive_failures_total` rising → the S3 archive:

   ```bash
   kubectl -n secure-event-api-production get secureservice events-archive
   kubectl -n secure-event-api-production logs deploy/secure-event-api | grep "archive failed" | tail
   ```

   `AccessDenied` = IRSA role/boundary problem (check the ServiceAccount annotation matches
   `status.roleArn` of the claim). `NoSuchBucket` = the claim is not Ready.

## Mitigate

- Correlates with a release → [rollback-via-git.md](rollback-via-git.md).
- Archive-only failure → the API returns 503 so clients retry; no data loss. Fix the claim/role, no rollback.
