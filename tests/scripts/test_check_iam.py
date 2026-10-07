import json
from pathlib import Path

from scripts.check_iam import findings, main


def plan(config_resources: list[dict], value_resources: list[dict] | None = None) -> dict:
    return {
        "configuration": {"root_module": {"resources": config_resources}},
        "planned_values": {"root_module": {"resources": value_resources or []}},
    }


def doc_resource(name: str, statements: list[dict]) -> dict:
    return {
        "address": f"data.aws_iam_policy_document.{name}",
        "mode": "data",
        "type": "aws_iam_policy_document",
        "expressions": {
            "statement": [{k: {"constant_value": v} for k, v in s.items()} for s in statements]
        },
    }


def test_explicit_actions_pass() -> None:
    doc = plan(
        [
            doc_resource(
                "task", [{"effect": "Allow", "actions": ["dynamodb:GetItem"], "resources": ["*"]}]
            )
        ]
    )
    assert findings(doc) == []


def test_wildcard_action_in_a_policy_document_fails() -> None:
    doc = plan([doc_resource("bad", [{"actions": ["dynamodb:*"]}])])
    [f] = findings(doc)
    assert f.address == "data.aws_iam_policy_document.bad" and "dynamodb:*" in f.problem


def test_not_actions_fails() -> None:
    doc = plan([doc_resource("bad", [{"not_actions": ["iam:PassRole"]}])])
    assert "NotAction" in findings(doc)[0].problem


def test_known_inline_policy_json_is_checked_too() -> None:
    policy = json.dumps({"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]})
    doc = plan(
        [],
        [
            {
                "address": "aws_iam_role_policy.x",
                "type": "aws_iam_role_policy",
                "values": {"policy": policy},
            }
        ],
    )
    assert findings(doc)[0].address == "aws_iam_role_policy.x"


def test_aws_managed_policy_attachments_fail() -> None:
    doc = plan(
        [],
        [
            {
                "address": "aws_iam_role_policy_attachment.a",
                "type": "aws_iam_role_policy_attachment",
                "values": {"policy_arn": "arn:aws:iam::aws:policy/AmazonS3FullAccess"},
            }
        ],
    )
    assert "AWS-managed" in findings(doc)[0].problem


def test_child_modules_are_scanned() -> None:
    doc = {
        "configuration": {
            "root_module": {
                "module_calls": {
                    "m": {
                        "module": {"resources": [doc_resource("bad", [{"actions": ["s3:Get*"]}])]}
                    }
                }
            }
        }
    }
    assert len(findings(doc)) == 1


def policy_value(kind: str, text: dict, attr: str = "policy") -> dict:
    return {"address": f"{kind}.x", "type": kind, "values": {attr: json.dumps(text)}}


def test_state_shape_with_child_modules_is_scanned() -> None:
    bad = policy_value("aws_iam_role_policy", {"Statement": [{"Action": "s3:*"}]})
    state = {"values": {"root_module": {"child_modules": [{"resources": [bad]}]}}}
    assert len(findings(state)) == 1


def test_action_as_single_string_and_statement_as_object() -> None:
    doc = plan([], [policy_value("aws_iam_policy", {"Statement": {"Action": "iam:*"}})])
    assert "iam:*" in findings(doc)[0].problem


def test_non_constant_actions_are_unverifiable() -> None:
    res = doc_resource("v", [])
    res["expressions"]["statement"] = [{"actions": {"references": ["var.actions"]}}]
    [f] = findings(plan([res]))
    assert f.problem == "unverifiable: actions are not a constant list"


def test_dynamic_statement_block_is_unverifiable() -> None:
    res = doc_resource("d", [])
    res["expressions"] = {"dynamic": {"statement": {"for_each": {}}}}
    [f] = findings(plan([res]))
    assert f.problem == "unverifiable: dynamic statement block"


def test_role_assume_policy_inline_policy_and_managed_arns_are_checked() -> None:
    role = {
        "address": "aws_iam_role.r",
        "type": "aws_iam_role",
        "values": {
            "assume_role_policy": json.dumps({"Statement": [{"Action": "sts:*"}]}),
            "inline_policy": [{"policy": json.dumps({"Statement": [{"Action": "ec2:*"}]})}],
            "managed_policy_arns": ["arn:aws-cn:iam::aws:policy/ReadOnlyAccess"],
        },
    }
    problems = [f.problem for f in findings(plan([], [role]))]
    assert len(problems) == 3 and any("AWS-managed" in p for p in problems)


def test_bucket_and_queue_policies_are_checked() -> None:
    bad = {"Statement": [{"Action": "s3:*"}]}
    doc = plan(
        [],
        [policy_value("aws_s3_bucket_policy", bad), policy_value("aws_sqs_queue_policy", bad)],
    )
    assert len(findings(doc)) == 2


def write(tmp_path: Path, doc: dict) -> str:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def test_main_vacuous_input_fails(tmp_path: Path) -> None:
    assert main([write(tmp_path, plan([]))]) == 1


def test_main_clean_input_passes_and_notes_unknown_values(tmp_path: Path, capsys) -> None:
    ok = doc_resource("t", [{"actions": ["dynamodb:GetItem"]}])
    unknown = {"address": "aws_iam_role_policy.u", "type": "aws_iam_role_policy", "values": {}}
    assert main([write(tmp_path, plan([ok], [unknown]))]) == 0
    assert "1 policy value(s) resolve at apply" in capsys.readouterr().out


def test_main_bad_usage_exits_2() -> None:
    assert main([]) == 2
