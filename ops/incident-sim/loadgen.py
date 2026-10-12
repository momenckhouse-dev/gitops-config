#!/usr/bin/env python3
"""Steady POST /v1/events load so the incident shows up in metrics (stdlib only).

kubectl -n secure-event-api-staging port-forward svc/secure-event-api 8080:80
python loadgen.py --url http://localhost:8080 --rps 20 --duration 900
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
import urllib.error
import urllib.request
from collections import Counter

SEVERITIES = ["low", "medium", "high", "critical"]
SOURCES = ["fw-gw-01", "fw-gw-02", "ips-edge-eu", "endpoint-agent"]


def send(url: str, results: Counter, lock: threading.Lock) -> None:
    body = json.dumps(
        {
            "source": random.choice(SOURCES),  # noqa: S311
            "severity": random.choice(SEVERITIES),  # noqa: S311
            "message": "synthetic event from loadgen",
            "attributes": {"loadgen": True},
        }
    ).encode()
    req = urllib.request.Request(  # noqa: S310
        f"{url}/v1/events", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            code = str(resp.status)
    except urllib.error.HTTPError as exc:
        code = str(exc.code)
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        code = "conn-error"
    with lock:
        results[code] += 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument("--rps", type=float, default=10)
    parser.add_argument("--duration", type=int, default=60, help="seconds")
    parser.add_argument("--report-every", type=int, default=5)
    args = parser.parse_args(argv)

    results: Counter = Counter()
    lock = threading.Lock()
    interval = 1 / args.rps
    end = time.monotonic() + args.duration
    next_report = time.monotonic() + args.report_every
    threads: list[threading.Thread] = []

    while time.monotonic() < end:
        t = threading.Thread(target=send, args=(args.url, results, lock), daemon=True)
        t.start()
        threads.append(t)
        if time.monotonic() >= next_report:
            with lock:
                print(time.strftime("%H:%M:%S"), dict(sorted(results.items())), flush=True)
            next_report += args.report_every
        time.sleep(interval)

    for t in threads:
        t.join(timeout=6)
    print("final", dict(sorted(results.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
