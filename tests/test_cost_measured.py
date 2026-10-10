from typing import Any

from cost.measured import USAGE_ONLY, format_table, spend_by_record_type, spend_by_service


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
            {"Keys": [key], "Metrics": {"UnblendedCost": {"Amount": amount}}}
            for key, amount in groups
        ]
    }


def test_service_spend_is_gross_usage_across_days_and_pages() -> None:
    ce = FakeCostExplorer(
        [
            {"ResultsByTime": [day(("EC2", "1.25"), ("ELB", "0.10"))], "NextPageToken": "t"},
            {"ResultsByTime": [day(("EC2", "0.75"))]},
        ]
    )
    assert spend_by_service(ce, "2026-10-08", "2026-10-11") == {"EC2": 2.0, "ELB": 0.10}
    assert ce.calls[0]["Filter"] == USAGE_ONLY
    assert ce.calls[0]["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"}]
    assert ce.calls[0]["TimePeriod"] == {"Start": "2026-10-08", "End": "2026-10-11"}
    assert ce.calls[1]["NextPageToken"] == "t"


def test_record_types_are_unfiltered() -> None:
    ce = FakeCostExplorer([{"ResultsByTime": [day(("Usage", "3.39"), ("Credit", "-3.39"))]}])
    assert spend_by_record_type(ce, "2026-10-08", "2026-10-11") == {"Usage": 3.39, "Credit": -3.39}
    assert "Filter" not in ce.calls[0]


def test_table_shows_gross_usage_then_credits_and_net() -> None:
    table = format_table({"ELB": 0.10, "EC2": 2.0, "Tax": 0.0}, {"Usage": 2.10, "Credit": -2.10})
    lines = table.splitlines()
    assert lines[1].startswith("EC2") and lines[2].startswith("ELB")
    assert "Tax" not in table
    assert lines[3].split() == ["Gross", "usage", "2.10"]
    assert lines[4].split() == ["Credit", "-2.10"]
    assert lines[5].split() == ["Net", "billed", "0.00"]
