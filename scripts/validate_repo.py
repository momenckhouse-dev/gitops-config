#!/usr/bin/env python3
"""Static checks that need nothing but Python + PyYAML (run before pushing).

- every YAML/JSON file parses (Helm templates excluded)
- every kustomization's resources/patches/generator files exist
- every Argo CD Application path in the config repo exists
- every GitHub workflow declares top-level permissions and runs-on for each job
- every Python file compiles
"""

from __future__ import annotations

import json
import py_compile
import sys
from pathlib import Path

import yaml

# Workspace folder holding app-source/, gitops-config/ and infra/ side by side.
ROOT = Path(__file__).resolve().parents[2]
GITOPS = ROOT / "gitops-config"
GITOPS_REPO_URL = "https://github.com/momenckhouse-dev/gitops-config.git"
SKIP_DIRS = {".venv", ".git", "node_modules", "__pycache__", ".terraform"}

errors: list[str] = []
checked: dict[str, int] = {}


def rel(p: Path) -> str:
    return str(p.relative_to(ROOT)).replace("\\", "/")


def files(pattern: str):
    for p in ROOT.rglob(pattern):
        if not SKIP_DIRS.intersection(p.parts):
            yield p


def count(kind: str) -> None:
    checked[kind] = checked.get(kind, 0) + 1


def load_yaml_docs(path: Path) -> list:
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d is not None]


def check_yaml_and_json() -> None:
    for path in list(files("*.yaml")) + list(files("*.yml")):
        if "templates" in path.parts and "charts" in path.parts:
            continue
        try:
            load_yaml_docs(path)
            count("yaml")
        except yaml.YAMLError as exc:
            errors.append(f"{rel(path)}: invalid YAML: {exc}")
    for path in files("*.json"):
        try:
            json.loads(path.read_text(encoding="utf-8"))
            count("json")
        except json.JSONDecodeError as exc:
            errors.append(f"{rel(path)}: invalid JSON: {exc}")


def check_kustomizations() -> None:
    for path in files("kustomization.yaml"):
        base = path.parent
        k = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        refs = list(k.get("resources", []))
        refs += [p["path"] for p in k.get("patches", []) if isinstance(p, dict) and "path" in p]
        for gen in k.get("configMapGenerator", []) + k.get("secretGenerator", []):
            refs += [f.split("=", 1)[-1] for f in gen.get("files", [])]
        for chart in k.get("helmCharts", []):
            home = base / k.get("helmGlobals", {}).get("chartHome", "charts")
            if not chart.get("repo") and not (home / chart["name"] / "Chart.yaml").exists():
                errors.append(f"{rel(path)}: local chart {chart['name']} not found under {home}")
            if chart.get("valuesFile"):
                refs.append(chart["valuesFile"])
        for ref in refs:
            if "://" not in ref and not (base / ref).exists():
                errors.append(f"{rel(path)}: missing reference {ref}")
        count("kustomization")


def check_argocd_apps() -> None:
    for path in (GITOPS / "argocd").rglob("*.yaml"):
        for doc in load_yaml_docs(path):
            if doc.get("kind") != "Application":
                continue
            spec = doc["spec"]
            for src in spec.get("sources", [spec.get("source")]):
                if src and src.get("repoURL") == GITOPS_REPO_URL and src.get("path"):
                    if not (GITOPS / src["path"]).is_dir():
                        errors.append(f"{rel(path)}: {doc['metadata']['name']} path {src['path']} does not exist")
                for vf in (src or {}).get("helm", {}).get("valueFiles", []):
                    if vf.startswith("$values/") and not (GITOPS / vf.removeprefix("$values/")).exists():
                        errors.append(f"{rel(path)}: values file {vf} does not exist")
            count("argocd-application")


def check_workflows() -> None:
    for path in files("*.yml"):
        if ".github" not in path.parts or "workflows" not in path.parts:
            continue
        wf = yaml.safe_load(path.read_text(encoding="utf-8"))
        if "permissions" not in wf:
            errors.append(f"{rel(path)}: no top-level permissions block (defaults to broad token)")
        for name, job in wf.get("jobs", {}).items():
            if "runs-on" not in job:
                errors.append(f"{rel(path)}: job {name} has no runs-on")
            for step in job.get("steps", []):
                uses = step.get("uses", "")
                if uses and "@" not in uses:
                    errors.append(f"{rel(path)}: job {name} uses unpinned action {uses}")
        count("workflow")


def check_python() -> None:
    for path in files("*.py"):
        try:
            py_compile.compile(str(path), doraise=True, cfile=None)
            count("python")
        except py_compile.PyCompileError as exc:
            errors.append(f"{rel(path)}: {exc.msg}")


def main() -> int:
    for check in (check_yaml_and_json, check_kustomizations, check_argocd_apps, check_workflows, check_python):
        check()
    summary = ", ".join(f"{n} {k}" for k, n in sorted(checked.items()))
    if errors:
        print(f"FAILED ({len(errors)} problems; checked {summary})")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"OK: checked {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
