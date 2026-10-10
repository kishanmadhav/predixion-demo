from pathlib import Path

import boto3
import pytest
from moto import mock_aws

import scripts.teardown_check as td
from scripts.teardown_check import main, run_checks


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch):
    for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        yield boto3.Session(region_name="ap-south-1")


def test_an_empty_account_is_clean(aws) -> None:
    assert all(not found for found in run_checks(aws).values())


def test_leftovers_are_reported(aws) -> None:
    aws.client("ec2").create_volume(AvailabilityZone="ap-south-1a", Size=10)
    aws.client("dynamodb").create_table(
        TableName="collectionsinference-state",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
    )
    results = run_checks(aws)
    assert len(results["EBS volumes"]) == 1
    assert results["DynamoDB tables"] == ["collectionsinference-state"]


def test_main_writes_a_log_and_exits_nonzero_when_not_clean(
    aws, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("scripts.teardown_check.make_session", lambda profile, region: aws)
    aws.client("ec2").create_volume(AvailabilityZone="ap-south-1a", Size=10)
    assert main(["--out-dir", str(tmp_path)]) == 1
    [log] = list(tmp_path.glob("teardown-check-*.log"))
    assert "NOT CLEAN" in log.read_text()


def test_snapshot_owned_by_the_account_is_reported(aws) -> None:
    ec2 = aws.client("ec2")
    vol = ec2.create_volume(AvailabilityZone="ap-south-1a", Size=1)["VolumeId"]
    snap = ec2.create_snapshot(VolumeId=vol)["SnapshotId"]
    assert run_checks(aws)["EBS snapshots"] == [snap]


def test_main_clean_account_returns_zero_and_logs_scope(aws, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("scripts.teardown_check.make_session", lambda profile, region: aws)
    assert main(["--out-dir", str(tmp_path), "--profile", "p1"]) == 0
    [log] = list(tmp_path.glob("teardown-check-*.log"))
    text = log.read_text()
    assert "CLEAN: no billable" in text and "profile p1" in text and "scope:" in text
    assert "Classic load balancers" in text


def test_a_failing_check_is_an_error_and_the_rest_still_run(
    aws, tmp_path: Path, monkeypatch
) -> None:
    def boom(session) -> list[str]:
        raise RuntimeError("AccessDenied")

    monkeypatch.setitem(td.CHECKS, "NAT gateways", boom)
    monkeypatch.setattr("scripts.teardown_check.make_session", lambda profile, region: aws)
    aws.client("ec2").create_volume(AvailabilityZone="ap-south-1a", Size=10)
    results = run_checks(aws)
    assert results["NAT gateways"][0].startswith("ERROR:")
    assert len(results["EBS volumes"]) == 1
    assert main(["--out-dir", str(tmp_path)]) == 1
    [log] = list(tmp_path.glob("teardown-check-*.log"))
    assert "ERROR: RuntimeError: AccessDenied" in log.read_text()
