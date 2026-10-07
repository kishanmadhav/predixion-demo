"""Prove the stack is gone: list anything left in the region that costs money.

  uv run python scripts/teardown_check.py            # profile predixion, ap-south-1
Writes teardown/teardown-check-<UTC>.log and exits 0 only when nothing is left.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3


def make_session(profile: str, region: str) -> Any:
    return boto3.Session(profile_name=profile, region_name=region)


def _pages(client: Any, operation: str, key: str, **kwargs: Any) -> list[Any]:
    paginator = client.get_paginator(operation)
    return [item for page in paginator.paginate(**kwargs) for item in page.get(key, [])]


def _instances(s: Any) -> list[str]:
    return [
        f"{i['InstanceId']} {i['InstanceType']} {i['State']['Name']}"
        for r in _pages(s.client("ec2"), "describe_instances", "Reservations")
        for i in r["Instances"]
        if i["State"]["Name"] != "terminated"
    ]


def _snapshots(s: Any) -> list[str]:
    # moto seeds AMI snapshots owned by other accounts yet matches them for "self",
    # so also require the owner to be this account (a no-op on real AWS).
    account = s.client("sts").get_caller_identity()["Account"]
    return [
        sn["SnapshotId"]
        for sn in _pages(s.client("ec2"), "describe_snapshots", "Snapshots", OwnerIds=["self"])
        if sn.get("OwnerId") == account
    ]


CHECKS: dict[str, Callable[[Any], list[str]]] = {
    "EC2 instances": _instances,
    "NAT gateways": lambda s: [
        f"{n['NatGatewayId']} {n['State']}"
        for n in _pages(s.client("ec2"), "describe_nat_gateways", "NatGateways")
        if n["State"] not in ("deleted",)
    ],
    "Load balancers": lambda s: [
        lb["LoadBalancerName"]
        for lb in _pages(s.client("elbv2"), "describe_load_balancers", "LoadBalancers")
    ],
    "Elastic IPs": lambda s: [
        a.get("PublicIp", "?") for a in s.client("ec2").describe_addresses()["Addresses"]
    ],
    "EBS volumes": lambda s: [
        f"{v['VolumeId']} {v['Size']}GB {v['State']}"
        for v in _pages(s.client("ec2"), "describe_volumes", "Volumes")
    ],
    "EBS snapshots": _snapshots,
    "Network interfaces": lambda s: [
        f"{n['NetworkInterfaceId']} {n.get('Description', '')}"
        for n in _pages(s.client("ec2"), "describe_network_interfaces", "NetworkInterfaces")
    ],
    "VPC endpoints": lambda s: [
        e["VpcEndpointId"]
        for e in _pages(s.client("ec2"), "describe_vpc_endpoints", "VpcEndpoints")
        if e["State"].lower() not in ("deleted",)
    ],
    "Non-default VPCs": lambda s: [
        v["VpcId"]
        for v in _pages(s.client("ec2"), "describe_vpcs", "Vpcs")
        if not v.get("IsDefault")
    ],
    "Auto Scaling groups": lambda s: [
        g["AutoScalingGroupName"]
        for g in _pages(
            s.client("autoscaling"), "describe_auto_scaling_groups", "AutoScalingGroups"
        )
    ],
    "ECS clusters": lambda s: [
        c["clusterName"]
        for arns in [s.client("ecs").list_clusters()["clusterArns"]]
        if arns
        for c in s.client("ecs").describe_clusters(clusters=arns)["clusters"]
        if c["status"] == "ACTIVE"
    ],
    "DynamoDB tables": lambda s: _pages(s.client("dynamodb"), "list_tables", "TableNames"),
    "ECR repositories": lambda s: [
        r["repositoryName"]
        for r in _pages(s.client("ecr"), "describe_repositories", "repositories")
    ],
    "CloudWatch log groups": lambda s: [
        g["logGroupName"] for g in _pages(s.client("logs"), "describe_log_groups", "logGroups")
    ],
    "CloudWatch alarms": lambda s: [
        a["AlarmName"] for a in _pages(s.client("cloudwatch"), "describe_alarms", "MetricAlarms")
    ],
    "CloudWatch dashboards": lambda s: [
        d["DashboardName"]
        for d in _pages(s.client("cloudwatch"), "list_dashboards", "DashboardEntries")
    ],
    "SNS topics": lambda s: [
        t["TopicArn"] for t in _pages(s.client("sns"), "list_topics", "Topics")
    ],
}


def run_checks(session: Any) -> dict[str, list[str]]:
    return {name: check(session) for name, check in CHECKS.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="predixion")
    parser.add_argument("--region", default="ap-south-1")
    parser.add_argument("--out-dir", default="teardown")
    args = parser.parse_args(argv)
    session = make_session(args.profile, args.region)
    identity = session.client("sts").get_caller_identity()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    results = run_checks(session)
    leftovers = sum(len(v) for v in results.values())
    lines = [
        f"Teardown check {stamp}",
        f"account {identity['Account']}  identity {identity['Arn']}  region {args.region}",
        "",
    ]
    for name, found in results.items():
        lines.append(f"[{'LEFT' if found else ' ok '}] {name}: {len(found)}")
        lines += [f"         - {item}" for item in found]
    lines += [
        "",
        "CLEAN: no billable resources remain"
        if not leftovers
        else f"NOT CLEAN: {leftovers} resource(s) remain",
    ]
    text = "\n".join(lines)
    print(text)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"teardown-check-{stamp}.log").write_text(text + "\n", encoding="utf-8")
    return 0 if not leftovers else 1


if __name__ == "__main__":
    raise SystemExit(main())
