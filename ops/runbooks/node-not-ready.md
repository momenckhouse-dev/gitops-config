# Runbook: Node NotReady

**Alert:** `NodeNotReady`

```bash
kubectl get nodes -o wide
kubectl describe node <node> | sed -n '/Conditions/,/Addresses/p'
kubectl get pods -A --field-selector spec.nodeName=<node>
aws ec2 describe-instance-status --instance-ids <id> --include-all-instances
```

- One node, instance status check failed → cordon + drain, let the managed node group replace it:
  `kubectl cordon <node> && kubectl drain <node> --ignore-daemonsets --delete-emptydir-data`
  then terminate the instance; the ASG launches a replacement in the same AZ.
- `MemoryPressure` / `DiskPressure` → find the top consumers (`platform-doctor` *Pod resource usage*),
  check for missing limits.
- Several nodes in one AZ → check the AWS Health Dashboard; workloads survive via
  `topologySpreadConstraints` across 3 AZs and PDBs.
- Capacity changes (instance type, node count) are Terraform changes in the `infra` repo, not console edits.
