#!/usr/bin/env python3
"""platform-doctor: one-shot health report for the EKS GitOps platform.

Checks nodes, pods, deployments, recent Warning events, Argo CD applications,
Crossplane SecureService claims, firing Prometheus alerts and pod resource usage.

Data comes from kubectl (live cluster) or from a directory of JSON snapshots
(--fixture), which is how the tool is tested and how incident evidence is replayed.

Exit codes: 0 healthy, 1 warnings (only with --fail-on warn), 2 critical findings.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess  # noqa: S404 - kubectl is the intended data source
import sys
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

OK, WARN, CRIT, SKIP = "OK", "WARN", "CRIT", "SKIP"
SEVERITY = {OK: 0, SKIP: 0, WARN: 1, CRIT: 2}

BAD_WAITING_REASONS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerConfigError",
    "CreateContainerError",
    "InvalidImageName",
}
IGNORED_ALERTS = {"Watchdog", "InfoInhibitor"}


@dataclass
class Finding:
    check: str
    status: str
    summary: str
    details: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- parsing


def parse_cpu(quantity: str) -> float:
    """Kubernetes CPU quantity -> cores."""
    q = str(quantity).strip()
    for suffix, factor in (("n", 1e-9), ("u", 1e-6), ("m", 1e-3)):
        if q.endswith(suffix):
            return float(q[: -len(suffix)]) * factor
    return float(q)


_MEMORY_SUFFIXES = (
    ("Ki", 1024),
    ("Mi", 1024**2),
    ("Gi", 1024**3),
    ("Ti", 1024**4),
    ("k", 1000),
    ("K", 1000),
    ("M", 1000**2),
    ("G", 1000**3),
    ("T", 1000**4),
)


def parse_memory(quantity: str) -> int:
    """Kubernetes memory quantity -> bytes."""
    q = str(quantity).strip()
    for suffix, factor in _MEMORY_SUFFIXES:
        if q.endswith(suffix):
            return int(float(q[: -len(suffix)]) * factor)
    return int(float(q))


def human_bytes(n: float) -> str:
    for unit in ("B", "Ki", "Mi", "Gi"):
        if abs(n) < 1024 or unit == "Gi":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}Gi"


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _conditions(obj: dict) -> dict[str, dict]:
    return {c.get("type"): c for c in obj.get("status", {}).get("conditions", []) or []}


def _ref(obj: dict) -> str:
    meta = obj.get("metadata", {})
    ns = meta.get("namespace")
    return f"{ns}/{meta.get('name')}" if ns else meta.get("name", "?")


# --------------------------------------------------------------------------- sources


class KubectlSource:
    def __init__(self, context: str | None, prometheus_url: str | None, argocd_namespace: str):
        if not shutil.which("kubectl"):
            raise SystemExit("kubectl not found on PATH (use --fixture for offline mode)")
        self.context = context
        self.prometheus_url = prometheus_url.rstrip("/") if prometheus_url else None
        self.argocd_namespace = argocd_namespace
        self.errors: dict[str, str] = {}

    def _kubectl(self, *args: str) -> str | None:
        cmd = ["kubectl", *args]
        if self.context:
            cmd += ["--context", self.context]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)  # noqa: S603
        if proc.returncode != 0:
            self.errors[" ".join(args[:2])] = proc.stderr.strip().splitlines()[-1] if proc.stderr else "error"
            return None
        return proc.stdout

    def _get(self, resource: str, namespace: str | None = None) -> dict | None:
        scope = ["-n", namespace] if namespace else ["-A"]
        out = self._kubectl("get", resource, *scope, "-o", "json")
        return json.loads(out) if out else None

    def now(self) -> datetime:
        return datetime.now(UTC)

    def nodes(self):
        out = self._kubectl("get", "nodes", "-o", "json")
        return json.loads(out) if out else None

    def pods(self):
        return self._get("pods")

    def deployments(self):
        return self._get("deployments")

    def events(self):
        return self._get("events")

    def applications(self):
        return self._get("applications.argoproj.io", self.argocd_namespace)

    def secureservices(self):
        return self._get("secureservices.platform.example.io")

    def alerts(self):
        if not self.prometheus_url:
            return None
        with urllib.request.urlopen(f"{self.prometheus_url}/api/v1/alerts", timeout=15) as resp:  # noqa: S310
            return json.load(resp)

    def pod_usage(self):
        out = self._kubectl("top", "pods", "-A", "--no-headers")
        if not out:
            return None
        usage = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                usage.append({"namespace": parts[0], "pod": parts[1], "cpu": parts[2], "memory": parts[3]})
        return usage


class FixtureSource:
    """Reads <dir>/<name>.json; a missing file means 'not available' (check is skipped)."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.errors: dict[str, str] = {}
        meta = self._load("meta") or {}
        self._now = parse_time(meta.get("captured_at")) or datetime.now(UTC)
        self.context = meta.get("context", f"fixture:{directory.name}")

    def _load(self, name: str):
        path = self.directory / f"{name}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def now(self) -> datetime:
        return self._now

    def __getattr__(self, name: str):
        if name in {
            "nodes",
            "pods",
            "deployments",
            "events",
            "applications",
            "secureservices",
            "alerts",
            "pod_usage",
        }:
            return lambda: self._load(name)
        raise AttributeError(name)


# --------------------------------------------------------------------------- checks


def check_nodes(data) -> Finding:
    if data is None:
        return Finding("Nodes", SKIP, "node list unavailable")
    items = data.get("items", [])
    not_ready, pressure = [], []
    for node in items:
        conds = _conditions(node)
        if conds.get("Ready", {}).get("status") != "True":
            not_ready.append(f"{_ref(node)}: NotReady ({conds.get('Ready', {}).get('reason', 'unknown')})")
        for kind in ("MemoryPressure", "DiskPressure", "PIDPressure"):
            if conds.get(kind, {}).get("status") == "True":
                pressure.append(f"{_ref(node)}: {kind}")
    status = CRIT if not_ready else WARN if pressure else OK
    ready = len(items) - len(not_ready)
    return Finding("Nodes", status, f"{ready}/{len(items)} nodes Ready", not_ready + pressure)


def check_pods(data, restart_threshold: int = 3) -> Finding:
    if data is None:
        return Finding("Pods", SKIP, "pod list unavailable")
    items = [p for p in data.get("items", []) if p.get("status", {}).get("phase") != "Succeeded"]
    details, status, unhealthy = [], OK, set()
    for pod in items:
        ref = _ref(pod)
        phase = pod.get("status", {}).get("phase")
        if phase == "Pending":
            sched = _conditions(pod).get("PodScheduled", {})
            reason = sched.get("reason") or "waiting"
            details.append(f"{ref}: Pending ({reason}) {sched.get('message', '')}".rstrip())
            status = max(status, WARN, key=SEVERITY.get)
            unhealthy.add(ref)
        elif phase in ("Failed", "Unknown"):
            details.append(f"{ref}: phase {phase}")
            status = max(status, WARN, key=SEVERITY.get)
            unhealthy.add(ref)
        for cs in pod.get("status", {}).get("containerStatuses", []) or []:
            restarts = cs.get("restartCount", 0)
            waiting = (cs.get("state", {}).get("waiting") or {}).get("reason")
            last = (cs.get("lastState", {}).get("terminated") or {}).get("reason")
            suffix = f"restarts={restarts}" + (f", last terminated: {last}" if last else "")
            if waiting in BAD_WAITING_REASONS:
                details.append(f"{ref} [{cs['name']}]: {waiting} ({suffix})")
                status = CRIT
                unhealthy.add(ref)
            elif last == "OOMKilled" or restarts >= restart_threshold:
                details.append(f"{ref} [{cs['name']}]: unstable ({suffix})")
                status = max(status, WARN, key=SEVERITY.get)
                unhealthy.add(ref)
    healthy = len(items) - len(unhealthy)
    return Finding("Pods", status, f"{healthy}/{len(items)} pods healthy", details)


def check_deployments(data) -> Finding:
    if data is None:
        return Finding("Deployments", SKIP, "deployment list unavailable")
    items = data.get("items", [])
    details, status = [], OK
    for dep in items:
        desired = dep.get("spec", {}).get("replicas", 1)
        st = dep.get("status", {})
        available = st.get("availableReplicas", 0) or 0
        updated = st.get("updatedReplicas", 0) or 0
        progressing = _conditions(dep).get("Progressing", {})
        line = f"{_ref(dep)}: {available}/{desired} available, {updated}/{desired} updated"
        if progressing.get("reason") == "ProgressDeadlineExceeded":
            details.append(f"{line} - rollout stuck (ProgressDeadlineExceeded)")
            status = CRIT
        elif desired > 0 and available == 0:
            details.append(f"{line} - no available replicas")
            status = CRIT
        elif available < desired:
            details.append(f"{line} - degraded")
            status = max(status, WARN, key=SEVERITY.get)
        elif updated < desired:
            details.append(f"{line} - rollout in progress")
            status = max(status, WARN, key=SEVERITY.get)
    ok = len(items) - len(details)
    return Finding("Deployments", status, f"{ok}/{len(items)} deployments fully available", details)


def check_events(data, now: datetime, window_minutes: int = 30, top: int = 10) -> Finding:
    if data is None:
        return Finding("Warning events", SKIP, "event list unavailable")
    since = now - timedelta(minutes=window_minutes)
    grouped: dict[tuple, dict] = {}
    for ev in data.get("items", []):
        if ev.get("type") != "Warning":
            continue
        ts = parse_time(ev.get("lastTimestamp") or ev.get("eventTime") or ev["metadata"].get("creationTimestamp"))
        if ts is None or ts < since:
            continue
        obj = ev.get("involvedObject", {})
        namespace = ev["metadata"].get("namespace") or "(cluster)"
        key = (namespace, f"{obj.get('kind')}/{obj.get('name')}", ev.get("reason"))
        entry = grouped.setdefault(key, {"count": 0, "message": ""})
        entry["count"] += ev.get("count", 1) or 1
        entry["message"] = (ev.get("message") or "").strip().splitlines()[0][:140]
    ranked = sorted(grouped.items(), key=lambda kv: kv[1]["count"], reverse=True)
    details = [f"x{v['count']:<4} {ns}/{obj} {reason}: {v['message']}" for (ns, obj, reason), v in ranked[:top]]
    total = sum(v["count"] for v in grouped.values())
    status = WARN if grouped else OK
    return Finding("Warning events", status, f"{total} warnings in the last {window_minutes}m", details)


def check_argocd(data) -> Finding:
    if data is None:
        return Finding("Argo CD applications", SKIP, "applications.argoproj.io unavailable")
    items = data.get("items", [])
    details, status, good = [], OK, 0
    for app in items:
        st = app.get("status", {})
        sync = st.get("sync", {}).get("status", "Unknown")
        health = st.get("health", {}).get("status", "Unknown")
        rev = (st.get("sync", {}).get("revision") or "")[:7]
        images = ", ".join(i.split("/")[-1] for i in st.get("summary", {}).get("images", []) or [])
        line = f"{app['metadata']['name']}: {sync}/{health} rev={rev or '-'}" + (f" images={images}" if images else "")
        if health in ("Degraded", "Missing"):
            status = CRIT
            details.append(line)
        elif sync != "Synced" or health != "Healthy":
            status = max(status, WARN, key=SEVERITY.get)
            details.append(line)
        else:
            good += 1
    return Finding("Argo CD applications", status, f"{good}/{len(items)} Synced and Healthy", details)


def check_crossplane(data) -> Finding:
    if data is None:
        return Finding("SecureService claims", SKIP, "secureservices.platform.example.io unavailable")
    items = data.get("items", [])
    details, status = [], OK
    for claim in items:
        conds = _conditions(claim)
        ready = conds.get("Ready", {}).get("status", "Unknown")
        synced = conds.get("Synced", {}).get("status", "Unknown")
        bucket = claim.get("status", {}).get("bucketName", "-")
        if synced == "False":
            status = CRIT
            details.append(f"{_ref(claim)}: Synced=False ({conds['Synced'].get('message', '')})")
        elif ready != "True":
            status = max(status, WARN, key=SEVERITY.get)
            details.append(f"{_ref(claim)}: Ready={ready} bucket={bucket}")
    ready_count = len(items) - len(details)
    return Finding("SecureService claims", status, f"{ready_count}/{len(items)} archives ready", details)


def check_alerts(data) -> Finding:
    if data is None:
        return Finding("Prometheus alerts", SKIP, "no --prometheus-url given")
    alerts = [
        a
        for a in data.get("data", {}).get("alerts", [])
        if a.get("state") == "firing" and a.get("labels", {}).get("alertname") not in IGNORED_ALERTS
    ]
    status, details = OK, []
    for a in sorted(alerts, key=lambda a: a["labels"].get("severity") != "critical"):
        labels = a.get("labels", {})
        sev = labels.get("severity", "none")
        status = max(status, CRIT if sev == "critical" else WARN, key=SEVERITY.get)
        where = labels.get("namespace", "")
        summary = a.get("annotations", {}).get("summary", "")
        details.append(f"[{sev}] {labels.get('alertname')} {where}: {summary}".rstrip(": "))
    return Finding("Prometheus alerts", status, f"{len(alerts)} firing", details)


def check_resources(usage, pods, top: int = 5, threshold: float = 0.9) -> Finding:
    if usage is None:
        return Finding("Pod resource usage", SKIP, "metrics unavailable (metrics-server / kubectl top)")
    limits: dict[str, int] = {}
    for pod in (pods or {}).get("items", []):
        containers = pod.get("spec", {}).get("containers", [])
        mems = [c.get("resources", {}).get("limits", {}).get("memory") for c in containers]
        if mems and all(mems):
            limits[_ref(pod)] = sum(parse_memory(m) for m in mems)
    rows = []
    for u in usage:
        ref = f"{u['namespace']}/{u['pod']}"
        mem = parse_memory(u["memory"])
        limit = limits.get(ref)
        rows.append((ref, parse_cpu(u["cpu"]), mem, limit, mem / limit if limit else None))
    hot = [r for r in rows if r[4] is not None and r[4] >= threshold]
    rows.sort(key=lambda r: (r[4] or 0, r[2]), reverse=True)
    details = [
        f"{ref}: cpu={cpu:.3f} cores mem={human_bytes(mem)}"
        + (f" ({ratio:.0%} of {human_bytes(limit)} limit)" if limit else "")
        for ref, cpu, mem, limit, ratio in rows[:top]
    ]
    total_cpu = sum(r[1] for r in rows)
    total_mem = sum(r[2] for r in rows)
    summary = (
        f"{len(rows)} pods, {total_cpu:.2f} cores, {human_bytes(total_mem)} total; "
        f"{len(hot)} above {threshold:.0%} of memory limit"
    )
    return Finding("Pod resource usage", WARN if hot else OK, summary, details)


# --------------------------------------------------------------------------- report


def run_checks(source, events_window: int, restart_threshold: int) -> dict:
    pods = source.pods()
    findings = [
        check_nodes(source.nodes()),
        check_pods(pods, restart_threshold),
        check_deployments(source.deployments()),
        check_argocd(source.applications()),
        check_crossplane(source.secureservices()),
        check_alerts(source.alerts()),
        check_resources(source.pod_usage(), pods),
        check_events(source.events(), source.now(), events_window),
    ]
    overall = max((f.status for f in findings), key=SEVERITY.get)
    return {
        "generated_at": source.now().isoformat(),
        "context": getattr(source, "context", None) or "current-context",
        "overall": overall,
        "findings": [asdict(f) for f in findings],
        "source_errors": source.errors,
    }


def render_text(report: dict) -> str:
    lines = [
        "=" * 78,
        f" platform-doctor  |  {report['context']}  |  {report['generated_at']}",
        f" OVERALL: {report['overall']}",
        "=" * 78,
    ]
    for f in report["findings"]:
        lines.append(f"[{f['status']:<4}] {f['check']:<22} {f['summary']}")
        lines.extend(f"         - {d}" for d in f["details"])
    if report["source_errors"]:
        lines.append("")
        lines.extend(f"note: {k}: {v}" for k, v in report["source_errors"].items())
    return "\n".join(lines)


def render_markdown(report: dict) -> str:
    out = [
        f"# Platform health report - **{report['overall']}**",
        "",
        f"Context `{report['context']}`, generated {report['generated_at']}.",
        "",
        "| Status | Check | Summary |",
        "|---|---|---|",
    ]
    out += [f"| {f['status']} | {f['check']} | {f['summary']} |" for f in report["findings"]]
    for f in report["findings"]:
        if f["details"]:
            out += ["", f"## {f['check']}", ""] + [f"- `{d}`" for d in f["details"]]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="platform-doctor", description=__doc__.splitlines()[0])
    parser.add_argument("--context", help="kubeconfig context (default: current)")
    parser.add_argument("--fixture", type=Path, help="read JSON snapshots from this directory instead of kubectl")
    parser.add_argument("--prometheus-url", help="e.g. http://localhost:9090 (kubectl port-forward)")
    parser.add_argument("--argocd-namespace", default="argocd")
    parser.add_argument("--events-window", type=int, default=30, help="minutes of Warning events to include")
    parser.add_argument("--restart-threshold", type=int, default=3)
    parser.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    parser.add_argument("--output", type=Path, help="write the report to a file as well")
    parser.add_argument("--fail-on", choices=["warn", "crit"], default="crit")
    args = parser.parse_args(argv)

    if args.fixture:
        source = FixtureSource(args.fixture)
    else:
        source = KubectlSource(args.context, args.prometheus_url, args.argocd_namespace)

    report = run_checks(source, args.events_window, args.restart_threshold)
    rendered = {"text": render_text, "markdown": render_markdown, "json": lambda r: json.dumps(r, indent=2)}[
        args.format
    ](report)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")

    if report["overall"] == CRIT:
        return 2
    if report["overall"] == WARN and args.fail_on == "warn":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
