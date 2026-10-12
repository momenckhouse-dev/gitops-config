import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import platform_doctor as pd

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "quantity, expected",
    [("100m", 0.1), ("2", 2.0), ("250000000n", 0.25), ("1500u", 0.0015)],
)
def test_parse_cpu(quantity, expected):
    assert pd.parse_cpu(quantity) == pytest.approx(expected)


@pytest.mark.parametrize(
    "quantity, expected",
    [("256Mi", 256 * 1024**2), ("1Gi", 1024**3), ("512Ki", 512 * 1024), ("1M", 1_000_000), ("1024", 1024)],
)
def test_parse_memory(quantity, expected):
    assert pd.parse_memory(quantity) == expected


def test_healthy_fixture_is_ok():
    report = pd.run_checks(pd.FixtureSource(FIXTURES / "healthy"), events_window=30, restart_threshold=3)
    statuses = {f["check"]: f["status"] for f in report["findings"]}
    assert report["overall"] == pd.OK, statuses
    assert statuses["Argo CD applications"] == pd.OK


def test_incident_fixture_is_critical_and_explains_why():
    report = pd.run_checks(pd.FixtureSource(FIXTURES / "incident"), events_window=30, restart_threshold=3)
    by_check = {f["check"]: f for f in report["findings"]}
    assert report["overall"] == pd.CRIT
    assert by_check["Pods"]["status"] == pd.CRIT
    assert any("CrashLoopBackOff" in d and "OOMKilled" in d for d in by_check["Pods"]["details"])
    assert by_check["Argo CD applications"]["status"] == pd.CRIT
    assert any("secure-event-api-production: Synced/Degraded" in d for d in by_check["Argo CD applications"]["details"])
    assert by_check["Deployments"]["status"] == pd.WARN
    assert by_check["Prometheus alerts"]["status"] == pd.CRIT
    assert by_check["Pod resource usage"]["status"] == pd.WARN
    assert by_check["Warning events"]["details"][0].startswith("x23")


def test_events_outside_window_are_ignored():
    now = datetime(2026, 10, 8, 14, 0, tzinfo=UTC)
    events = {
        "items": [
            {
                "metadata": {"namespace": "a", "creationTimestamp": "2026-10-08T12:00:00Z"},
                "involvedObject": {"kind": "Pod", "name": "old"},
                "type": "Warning",
                "reason": "BackOff",
                "message": "old",
                "lastTimestamp": "2026-10-08T12:00:00Z",
            }
        ]
    }
    assert pd.check_events(events, now, window_minutes=30).status == pd.OK


def test_pending_pod_is_warning():
    pods = {
        "items": [
            {
                "metadata": {"namespace": "a", "name": "p"},
                "status": {
                    "phase": "Pending",
                    "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}],
                },
            }
        ]
    }
    finding = pd.check_pods(pods)
    assert finding.status == pd.WARN
    assert "Unschedulable" in finding.details[0]


def test_stuck_rollout_is_critical():
    deps = {
        "items": [
            {
                "metadata": {"namespace": "a", "name": "d"},
                "spec": {"replicas": 2},
                "status": {
                    "availableReplicas": 2,
                    "updatedReplicas": 1,
                    "conditions": [{"type": "Progressing", "reason": "ProgressDeadlineExceeded"}],
                },
            }
        ]
    }
    assert pd.check_deployments(deps).status == pd.CRIT


def test_missing_data_is_skipped_not_failed():
    assert pd.check_alerts(None).status == pd.SKIP
    assert pd.check_crossplane(None).status == pd.SKIP


def test_cli_exit_codes_and_json_output(capsys):
    assert pd.main(["--fixture", str(FIXTURES / "healthy"), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["overall"] == "OK"
    assert pd.main(["--fixture", str(FIXTURES / "incident")]) == 2


def test_markdown_report_has_summary_table(capsys):
    pd.main(["--fixture", str(FIXTURES / "incident"), "--format", "markdown"])
    out = capsys.readouterr().out
    assert out.startswith("# Platform health report - **CRIT**")
    assert "| CRIT | Pods |" in out
