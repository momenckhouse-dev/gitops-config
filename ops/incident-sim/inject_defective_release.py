#!/usr/bin/env python3
"""Stage the INC-2026-001 defective release in a gitops-config checkout.

Release 9e41c07a2b55 shipped an unbounded in-memory de-duplication cache. To reproduce
it without building a broken image, this script pins the new tag *and* turns on the
app's memory_leak fault mode in the target overlay, so each request retains ~1 MiB.

It only edits files. You commit and open the PR yourself, exactly like a real release,
and the rollback is `git revert` of that commit - never kubectl.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

PATCH_FILE = "patches/fault-injection.yaml"
PATCH = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: secure-event-api
spec:
  template:
    spec:
      containers:
        - name: secure-event-api
          env:
            - name: FAULT_MODE
              value: {fault}
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gitops-dir", type=Path, required=True, help="path to a gitops-config checkout")
    parser.add_argument("--env", default="staging", choices=["staging", "production"])
    parser.add_argument("--tag", default="9e41c07a2b55", help="image tag of the 'defective' release")
    parser.add_argument("--fault", default="memory_leak", choices=["memory_leak", "errors", "latency"])
    args = parser.parse_args(argv)

    overlay = args.gitops_dir / "environments" / args.env
    kustomization_path = overlay / "kustomization.yaml"
    if not kustomization_path.exists():
        print(f"not found: {kustomization_path}", file=sys.stderr)
        return 1

    kustomization = yaml.safe_load(kustomization_path.read_text(encoding="utf-8"))
    for image in kustomization.get("images", []):
        if image.get("name") == "secure-event-api":
            previous = image.get("newTag")
            image["newTag"] = args.tag
            break
    else:
        print("no secure-event-api image entry in the overlay", file=sys.stderr)
        return 1

    patches = kustomization.setdefault("patches", [])
    if {"path": PATCH_FILE} not in patches:
        patches.append({"path": PATCH_FILE})

    (overlay / PATCH_FILE).write_text(PATCH.format(fault=args.fault), encoding="utf-8")
    kustomization_path.write_text(yaml.safe_dump(kustomization, sort_keys=False), encoding="utf-8")

    print(f"{args.env}: secure-event-api {previous} -> {args.tag} (FAULT_MODE={args.fault})")
    print("Next, from the gitops-config checkout:")
    print(f"  git checkout -b release/{args.env}-{args.tag}")
    print("  git add environments/")
    print(f'  git commit -m "{args.env}: deploy secure-event-api {args.tag}"')
    print("  git push -u origin HEAD   # open the PR, get it approved, merge")
    return 0


if __name__ == "__main__":
    sys.exit(main())
