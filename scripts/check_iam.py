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
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

POLICY_ATTRIBUTES = {
    "aws_iam_policy": "policy",
    "aws_iam_role_policy": "policy",
    "aws_iam_user_policy": "policy",
    "aws_iam_group_policy": "policy",
    "aws_iam_role": "assume_role_policy",
    "aws_cloudwatch_log_resource_policy": "policy_document",
    "aws_ecr_repository_policy": "policy",
    "aws_sns_topic_policy": "policy",
    "aws_s3_bucket_policy": "policy",
    "aws_sqs_queue_policy": "policy",
}
ATTACHMENTS = {
    "aws_iam_role_policy_attachment",
    "aws_iam_user_policy_attachment",
    "aws_iam_group_policy_attachment",
    "aws_iam_policy_attachment",
}
AWS_MANAGED = re.compile(r"^arn:[^:]+:iam::aws:policy/")


@dataclass(frozen=True)
class Finding:
    address: str
    problem: str


@dataclass
class Scan:
    findings: list[Finding] = field(default_factory=list)
    policies: int = 0  # policy documents/values actually inspected
    unknown: int = 0  # policy values not yet known at plan time


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


def _is_dynamic(expressions: dict[str, Any], statements: Any) -> bool:
    if "dynamic" in expressions or "dynamic_statement" in expressions:
        return True
    if not isinstance(statements, list):
        return statements is not None  # e.g. {"references": [...]}: not a literal block list
    return any(
        not isinstance(s, dict) or "for_each" in s or "content" in s or "dynamic" in s
        for s in statements
    )


def _config_statement(address: str, statement: dict[str, Any], scan: Scan) -> None:
    if "not_actions" in statement:
        scan.findings.append(Finding(address, "uses NotAction"))
    if "actions" not in statement:
        if "not_actions" not in statement:
            scan.findings.append(Finding(address, "unverifiable: actions are not a constant list"))
        return
    actions = statement["actions"]
    constant = actions.get("constant_value") if isinstance(actions, dict) else None
    if not isinstance(constant, (list, str)):
        scan.findings.append(Finding(address, "unverifiable: actions are not a constant list"))
    elif bad := _bad_actions(_as_list(constant)):
        scan.findings.append(Finding(address, f"wildcard action(s): {', '.join(bad)}"))


def _scan_config(doc: dict[str, Any], scan: Scan) -> None:
    root = (doc.get("configuration") or {}).get("root_module") or {}
    for module in _modules(root):
        for res in module.get("resources") or []:
            if res.get("type") != "aws_iam_policy_document":
                continue
            scan.policies += 1
            expressions = res.get("expressions") or {}
            statements = expressions.get("statement")
            if _is_dynamic(expressions, statements):
                scan.findings.append(
                    Finding(res["address"], "unverifiable: dynamic statement block")
                )
                continue
            for statement in _as_list(statements):
                _config_statement(res["address"], statement, scan)


def _scan_policy_json(address: str, text: Any, scan: Scan) -> None:
    scan.policies += 1
    try:
        doc = json.loads(text) if isinstance(text, str) else text
    except json.JSONDecodeError:
        scan.findings.append(Finding(address, "unverifiable: policy is not valid JSON"))
        return
    for statement in _as_list((doc or {}).get("Statement")):
        if not isinstance(statement, dict):
            continue
        if "NotAction" in statement:
            scan.findings.append(Finding(address, "uses NotAction"))
        if bad := _bad_actions(_as_list(statement.get("Action"))):
            scan.findings.append(Finding(address, f"wildcard action(s): {', '.join(bad)}"))


def _scan_values(doc: dict[str, Any], scan: Scan) -> None:
    values = doc.get("planned_values") or doc.get("values") or {}
    for module in _modules(values.get("root_module") or {}):
        for res in module.get("resources") or []:
            kind, attrs, address = res.get("type"), res.get("values") or {}, res["address"]
            managed = [attrs.get("policy_arn")] if kind in ATTACHMENTS else []
            if kind == "aws_iam_role":
                managed += _as_list(attrs.get("managed_policy_arns"))
            for arn in managed:
                if AWS_MANAGED.match(str(arn or "")):
                    scan.findings.append(Finding(address, f"attaches AWS-managed {arn}"))
            if kind not in POLICY_ATTRIBUTES:
                continue
            primary = attrs.get(POLICY_ATTRIBUTES[kind])
            if primary:
                _scan_policy_json(address, primary, scan)
            else:
                scan.unknown += 1
            if kind == "aws_iam_role":
                for inline in _as_list(attrs.get("inline_policy")):
                    if inline.get("policy"):
                        _scan_policy_json(address, inline["policy"], scan)


def scan(doc: dict[str, Any]) -> Scan:
    result = Scan()
    _scan_config(doc, result)
    _scan_values(doc, result)
    return result


def findings(doc: dict[str, Any]) -> list[Finding]:
    return scan(doc).findings


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__)
        return 2
    result = scan(json.loads(Path(args[0]).read_text(encoding="utf-8")))
    for f in result.findings:
        print(f"FAIL  {f.address}: {f.problem}")
    if result.unknown:
        print(
            f"{result.unknown} policy value(s) resolve at apply; "
            "re-run on the state (terraform show -json) for a full check"
        )
    if not result.policies:
        print("IAM check: no IAM policies found (refusing to pass vacuously)")
        return 1
    problems = len(result.findings)
    print("IAM check: " + ("no wildcard actions" if not problems else f"{problems} problem(s)"))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
