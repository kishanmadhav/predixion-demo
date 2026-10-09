from typing import Any

from cost.measured import format_table, spend_by_service


class FakeCostExplorer:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def get_cost_and_usage(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.pages[len(self.calls) - 1]


def day(*groups: tuple[str, str]) -> dict[str, Any]:
    return {
        "Groups": [
            {"Keys": [service], "Metrics": {"UnblendedCost": {"Amount": amount}}}
            for service, amount in groups
        ]
    }


def test_sums_each_service_across_days_and_pages() -> None:
    ce = FakeCostExplorer(
        [
            {"ResultsByTime": [day(("EC2", "1.25"), ("ELB", "0.10"))], "NextPageToken": "t"},
            {"ResultsByTime": [day(("EC2", "0.75"))]},
        ]
    )
    assert spend_by_service(ce, "2026-10-08", "2026-10-11") == {"EC2": 2.0, "ELB": 0.10}
    assert ce.calls[1]["NextPageToken"] == "t"
    assert ce.calls[0]["TimePeriod"] == {"Start": "2026-10-08", "End": "2026-10-11"}


def test_table_sorts_by_cost_hides_zero_rows_and_totals() -> None:
    table = format_table({"ELB": 0.10, "EC2": 2.0, "Tax": 0.0})
    lines = table.splitlines()
    assert lines[1].startswith("EC2") and lines[2].startswith("ELB")
    assert "Tax" not in table
    assert lines[-1].split() == ["Total", "2.10"]
