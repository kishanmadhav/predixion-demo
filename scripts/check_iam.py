"""Fail if any IAM policy Terraform manages allows a wildcard action.

Checks, from `terraform show -json` output (a saved plan or the state):
* every `data.aws_iam_policy_document` statement in the configuration (known before
  apply, so this runs in CI on a plan);
* every known policy JSON in planned/state values (inline role policies, managed
  policies, resource policies);
* that no AWS-managed policy is attached (we cannot vouch for its actions).

  terraform -chdir=infra plan -out=tfplan
  terraform -chdir=infra show -json tfplan > plan.json
  uv run python scripts/check_iam.py plan.json
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

POLICY_ATTRIBUTES = {
    "aws_iam_policy": "policy",
    "aws_iam_role_policy": "policy",
    "aws_iam_user_policy": "policy",
    "aws_iam_group_policy": "policy",
    "aws_cloudwatch_log_resource_policy": "policy_document",
    "aws_ecr_repository_policy": "policy",
    "aws_sns_topic_policy": "policy",
}
ATTACHMENTS = {
    "aws_iam_role_policy_attachment",
    "aws_iam_user_policy_attachment",
    "aws_iam_group_policy_attachment",
    "aws_iam_policy_attachment",
}


@dataclass(frozen=True)
class Finding:
    address: str
    problem: str


def _modules(module: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield module
    for call in (module.get("module_calls") or {}).values():
        yield from _modules(call.get("module") or {})
    for child in module.get("child_modules") or []:
        yield from _modules(child)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _bad_actions(actions: list[Any]) -> list[str]:
    return [a for a in actions if isinstance(a, str) and "*" in a]


def _config_findings(doc: dict[str, Any]) -> Iterator[Finding]:
    root = (doc.get("configuration") or {}).get("root_module") or {}
    for module in _modules(root):
        for res in module.get("resources") or []:
            if res.get("type") != "aws_iam_policy_document":
                continue
            for statement in _as_list((res.get("expressions") or {}).get("statement")):
                if "not_actions" in statement:
                    yield Finding(res["address"], "uses NotAction")
                actions = _as_list((statement.get("actions") or {}).get("constant_value"))
                if bad := _bad_actions(actions):
                    yield Finding(res["address"], f"wildcard action(s): {', '.join(bad)}")


def _value_findings(doc: dict[str, Any]) -> Iterator[Finding]:
    values = doc.get("planned_values") or doc.get("values") or {}
    for module in _modules(values.get("root_module") or {}):
        for res in module.get("resources") or []:
            kind, attrs = res.get("type"), res.get("values") or {}
            if kind in ATTACHMENTS and str(attrs.get("policy_arn", "")).startswith(
                "arn:aws:iam::aws:policy/"
            ):
                yield Finding(res["address"], f"attaches AWS-managed {attrs['policy_arn']}")
            text = (
                attrs.get(POLICY_ATTRIBUTES.get(kind, ""), None)
                if kind in POLICY_ATTRIBUTES
                else None
            )
            if not text:
                continue
            for statement in _as_list(json.loads(text).get("Statement")):
                if "NotAction" in statement:
                    yield Finding(res["address"], "uses NotAction")
                if bad := _bad_actions(_as_list(statement.get("Action"))):
                    yield Finding(res["address"], f"wildcard action(s): {', '.join(bad)}")


def findings(doc: dict[str, Any]) -> list[Finding]:
    return list(_config_findings(doc)) + list(_value_findings(doc))


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__)
        return 2
    problems = findings(json.loads(Path(args[0]).read_text(encoding="utf-8")))
    for f in problems:
        print(f"FAIL  {f.address}: {f.problem}")
    print(
        "IAM check: " + ("no wildcard actions" if not problems else f"{len(problems)} problem(s)")
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
