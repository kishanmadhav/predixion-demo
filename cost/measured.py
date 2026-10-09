"""Measured Round 2 spend, by AWS service, from Cost Explorer.

Run:  uv run python -m cost.measured --start 2026-10-08 --end 2026-10-11

Cost Explorer lags by up to 24 hours, so run it the day after the teardown for the
final numbers. Each run makes one or two API requests, which Cost Explorer bills at
$0.01 each. Gross spend: the account has no credits, so UnblendedCost is what is paid.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from typing import Any

import boto3


def spend_by_service(ce: Any, start: str, end: str) -> dict[str, float]:
    """Sum UnblendedCost per service over [start, end), following pagination."""
    totals: dict[str, float] = defaultdict(float)
    kwargs: dict[str, Any] = {
        "TimePeriod": {"Start": start, "End": end},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
    }
    while True:
        page = ce.get_cost_and_usage(**kwargs)
        for day in page["ResultsByTime"]:
            for group in day["Groups"]:
                totals[group["Keys"][0]] += float(group["Metrics"]["UnblendedCost"]["Amount"])
        token = page.get("NextPageToken")
        if not token:
            return dict(totals)
        kwargs["NextPageToken"] = token


def format_table(totals: dict[str, float]) -> str:
    rows = sorted(((s, c) for s, c in totals.items() if round(c, 2) > 0), key=lambda r: -r[1])
    width = max([len(s) for s, _ in rows] + [len("Total")])
    lines = [f"{'Service':<{width}}  {'USD':>8}"]
    lines += [f"{service:<{width}}  {cost:>8.2f}" for service, cost in rows]
    lines.append(f"{'Total':<{width}}  {sum(totals.values()):>8.2f}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", required=True, help="first day, YYYY-MM-DD (inclusive)")
    parser.add_argument("--end", required=True, help="last day, YYYY-MM-DD (exclusive)")
    parser.add_argument("--profile", default="predixion")
    args = parser.parse_args()
    ce = boto3.Session(profile_name=args.profile).client("ce", region_name="us-east-1")
    print(format_table(spend_by_service(ce, args.start, args.end)))


if __name__ == "__main__":
    main()
