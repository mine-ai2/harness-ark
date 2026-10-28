#!/usr/bin/env python3
"""Structural checks on the talos-deploy workflows (mine-capstone#1085).

Run by talos-test and locally: python3 deploy/tests/check-talos-deploy.py
(needs PyYAML). Guards the properties the deploy depends on that actionlint
cannot see: prod waits for dev, secrets reach the reusable workflow, one
leg never cancels another, and every matrix entry is a production
environment name (a typo would make GitHub auto-create an UNPROTECTED
environment on the next run).
"""

import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
REUSABLE = "./.github/workflows/talos-deploy-env.yml"
PROD_ENV = re.compile(r"production(-[a-z0-9]([a-z0-9-]*[a-z0-9])?)?")

failures = []


def check(ok, message):
    if not ok:
        failures.append(message)


def load(name):
    doc = yaml.safe_load((WORKFLOWS / name).read_text())
    # PyYAML (YAML 1.1) parses a bare `on:` key as boolean True.
    doc["on"] = doc.pop(True, doc.get("on"))
    return doc


caller = load("talos-deploy.yml")
callee = load("talos-deploy-env.yml")

check(
    caller["on"] == {"push": {"branches": ["main"]}, "workflow_dispatch": None},
    f"talos-deploy triggers changed: {caller['on']!r}",
)
jobs = caller.get("jobs", {})
check(set(jobs) == {"deploy-dev", "deploy-prod"}, f"talos-deploy jobs: {sorted(jobs)}")
dev, prod = jobs.get("deploy-dev", {}), jobs.get("deploy-prod", {})

check(dev.get("uses") == REUSABLE, "deploy-dev must call talos-deploy-env.yml")
check(dev.get("with") == {"environment": "development"}, "deploy-dev must deploy `development`")
check(dev.get("secrets") == "inherit", "deploy-dev must pass `secrets: inherit`")

check(prod.get("uses") == REUSABLE, "deploy-prod must call talos-deploy-env.yml")
check(prod.get("needs") in ("deploy-dev", ["deploy-dev"]), "deploy-prod must `needs: deploy-dev`")
check(prod.get("secrets") == "inherit", "deploy-prod must pass `secrets: inherit`")
check(
    prod.get("with") == {"environment": "${{ matrix.environment }}"},
    "deploy-prod must pass `environment: ${{ matrix.environment }}`",
)
strategy = prod.get("strategy", {})
check(strategy.get("fail-fast") is False, "deploy-prod must set `fail-fast: false`")
envs = strategy.get("matrix", {}).get("environment", [])
check(isinstance(envs, list) and "production" in envs, "the matrix must include `production`")
for env in envs if isinstance(envs, list) else []:
    check(
        isinstance(env, str) and PROD_ENV.fullmatch(env),
        f"matrix entry {env!r} is not production or production-<slug>",
    )
check(len(envs) == len(set(envs)), "duplicate matrix entries")

check(list(callee["on"]) == ["workflow_call"], "talos-deploy-env.yml must be workflow_call only")
inputs = callee["on"].get("workflow_call", {}).get("inputs", {})
check(
    inputs.get("environment", {}).get("required") is True
    and inputs.get("environment", {}).get("type") == "string",
    "talos-deploy-env.yml needs a required string input `environment`",
)
deploy = callee.get("jobs", {}).get("deploy", {})
check(deploy.get("environment") == "${{ inputs.environment }}", "callee must declare `environment: ${{ inputs.environment }}`")
check(
    deploy.get("concurrency") == {"group": "deploy-${{ inputs.environment }}", "cancel-in-progress": False},
    "callee concurrency must be deploy-${{ inputs.environment }}, cancel-in-progress: false",
)

if failures:
    for message in failures:
        print(f"FAIL: {message}", file=sys.stderr)
    sys.exit(1)
print("check-talos-deploy.py: all checks passed")
