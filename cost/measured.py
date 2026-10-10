"""Measured Round 2 spend, by AWS service, from Cost Explorer.

Run:  uv run python -m cost.measured --start 2026-10-08 --end 2026-10-11

Reports gross usage per service (what the resources cost), then any credits and the
net amount billed. Cost Explorer lags by up to 24 hours, so run it the day after the
teardown for final numbers. Each run makes a few API requests, billed at $0.01 each.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from typing import Any

import boto3

USAGE_ONLY = {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}}


def _sum_by(
    ce: Any, start: str, end: str, key: str, filter_: dict[str, Any] | None
) -> dict[str, float]:
    """Sum UnblendedCost per `key` dimension over [start, end), following pagination."""
    totals: dict[str, float] = defaultdict(float)
    kwargs: dict[str, Any] = {
        "TimePeriod": {"Start": start, "End": end},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        "GroupBy": [{"Type": "DIMENSION", "Key": key}],
    }
    if filter_ is not None:
        kwargs["Filter"] = filter_
    while True:
        page = ce.get_cost_and_usage(**kwargs)
        for day in page["ResultsByTime"]:
            for group in day["Groups"]:
                totals[group["Keys"][0]] += float(group["Metrics"]["UnblendedCost"]["Amount"])
        token = page.get("NextPageToken")
        if not token:
            return dict(totals)
        kwargs["NextPageToken"] = token


def spend_by_service(ce: Any, start: str, end: str) -> dict[str, float]:
    """Gross usage cost per service: credits and refunds are excluded."""
    return _sum_by(ce, start, end, "SERVICE", USAGE_ONLY)


def spend_by_record_type(ce: Any, start: str, end: str) -> dict[str, float]:
    """Usage, Credit, Refund, Tax... totals; their sum is the net amount billed."""
    return _sum_by(ce, start, end, "RECORD_TYPE", None)


def format_table(totals: dict[str, float], record_types: dict[str, float] | None = None) -> str:
    rows = sorted(((s, c) for s, c in totals.items() if round(c, 2) > 0), key=lambda r: -r[1])
    width = max([len(s) for s, _ in rows] + [len("Net billed")])
    lines = [f"{'Service':<{width}}  {'USD':>8}"]
    lines += [f"{service:<{width}}  {cost:>8.2f}" for service, cost in rows]
    lines.append(f"{'Gross usage':<{width}}  {sum(totals.values()):>8.2f}")
    if record_types:
        for kind, amount in sorted(record_types.items()):
            if kind != "Usage" and round(amount, 2) != 0:
                lines.append(f"{kind:<{width}}  {amount:>8.2f}")
        lines.append(f"{'Net billed':<{width}}  {sum(record_types.values()):>8.2f}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", required=True, help="first day, YYYY-MM-DD (inclusive)")
    parser.add_argument("--end", required=True, help="last day, YYYY-MM-DD (exclusive)")
    parser.add_argument("--profile", default="predixion")
    args = parser.parse_args()
    ce = boto3.Session(profile_name=args.profile).client("ce", region_name="us-east-1")
    print(
        format_table(
            spend_by_service(ce, args.start, args.end),
            spend_by_record_type(ce, args.start, args.end),
        )
    )


if __name__ == "__main__":
    main()
