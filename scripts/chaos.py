"""Drive the GPU host's chaos proxy from your laptop, without exposing it.

The proxy listens only inside the VPC. This sends `curl` to it over SSM Run Command,
authenticated by your AWS profile.

  uv run python scripts/chaos.py stats
  uv run python scripts/chaos.py outage --stage llm --seconds 60
  uv run python scripts/chaos.py rate --rate 0.2
  uv run python scripts/chaos.py reset
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from typing import Any

import boto3

TAG_NAME = "collectionsinference-gpu"


def instance_id(ec2: Any) -> str:
    reservations = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [TAG_NAME]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )["Reservations"]
    ids = [i["InstanceId"] for r in reservations for i in r["Instances"]]
    if not ids:
        sys.exit('no running GPU host (is model_tier = "gpu" applied?)')
    return str(ids[0])


def curl(method: str, path: str, body: dict[str, object] | None) -> str:
    command = f"curl -sS --fail-with-body -X {method} http://localhost:8080{path}"
    if body is not None:
        command += f" -H 'content-type: application/json' -d {shlex.quote(json.dumps(body))}"
    return command


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", default="predixion")
    parser.add_argument("--region", default="ap-south-1")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("stats")
    sub.add_parser("reset")
    outage = sub.add_parser("outage")
    outage.add_argument("--stage", choices=["stt", "llm", "tts"], required=True)
    outage.add_argument("--seconds", type=float, required=True)
    rate = sub.add_parser("rate")
    rate.add_argument("--rate", type=float, required=True)
    rate.add_argument("--stage", choices=["stt", "llm", "tts"])
    args = parser.parse_args()

    if args.action == "stats":
        command = curl("GET", "/admin/stats", None)
    elif args.action == "reset":
        command = curl("POST", "/admin/reset", None)
    elif args.action == "outage":
        command = curl("POST", "/admin/chaos", {"stage": args.stage, "outage_s": args.seconds})
    else:
        body: dict[str, object] = {"failure_rate": args.rate}
        if args.stage:
            body["stage"] = args.stage
        command = curl("POST", "/admin/chaos", body)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    target = instance_id(session.client("ec2"))
    ssm = session.client("ssm")
    sent = ssm.send_command(
        InstanceIds=[target], DocumentName="AWS-RunShellScript", Parameters={"commands": [command]}
    )
    command_id = sent["Command"]["CommandId"]
    for _ in range(30):
        time.sleep(1)
        try:
            result = ssm.get_command_invocation(CommandId=command_id, InstanceId=target)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if result["Status"] in ("Success", "Failed", "Cancelled", "TimedOut"):
            print(result["StandardOutputContent"] or result["StandardErrorContent"])
            return 0 if result["Status"] == "Success" else 1
    print("timed out waiting for SSM")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
