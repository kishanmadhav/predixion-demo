import json

from scripts.check_iam import findings


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
