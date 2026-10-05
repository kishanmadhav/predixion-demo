"""AWS Fargate cost model for the voice-agent orchestrator.

Run:  uv run python -m cost.fargate_cost

Every number in docs/writeup.pdf comes from this file (docs/writeup_pdf.py imports it).
Change an assumption below and re-run to see its effect.

Prices: us-east-1, Linux, on-demand, from https://aws.amazon.com/fargate/pricing/
(checked 2026-10-05). Fargate bills per second with a one-minute minimum, so
task-hours x hourly rate is exact for long-running service tasks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

HOURS_PER_MONTH = 730.0  # AWS pricing convention (8,760 h / 12)


@dataclass(frozen=True)
class Prices:
    name: str
    vcpu_hour: float
    gb_hour: float


X86_US_EAST_1 = Prices("x86", vcpu_hour=0.04048, gb_hour=0.004445)
ARM_US_EAST_1 = Prices("arm64 (Graviton)", vcpu_hour=0.03238, gb_hour=0.00356)


@dataclass(frozen=True)
class Assumptions:
    # Traffic profile from the brief
    baseline_calls_per_min: float = 10
    peak_calls_per_min: float = 50
    peak_hours_per_day: float = 2.0
    # Average connected-call duration. Collections handle times are typically 4-6 min
    # including after-call work; an AI agent has no wrap-up, so 4 min of talk time.
    call_minutes: float = 4.0
    # Task size and how many concurrent calls one task carries at the scaling target.
    # The orchestrator is I/O-bound (it relays audio and awaits STT/LLM/TTS), so
    # 20 calls per vCPU at ~60-70% CPU is a planning figure to be confirmed by load test.
    task_vcpu: float = 1.0
    task_gb: float = 2.0
    calls_per_task: int = 20
    # One spare task: N+1 across AZs, and room for a deploy or a task replacement.
    spare_tasks: int = 1
    # Scheduled scaling: scale out before the campaign window, scale in after it.
    prewarm_hours: float = 0.25  # tasks take ~30-60 s to start; give target tracking slack
    drain_hours: float = 0.25  # calls in flight finish + 300 s scale-in cooldown
    hours_per_month: float = HOURS_PER_MONTH

    @property
    def days_per_month(self) -> float:
        return self.hours_per_month / 24

    @property
    def calls_per_day(self) -> float:
        off_peak = (24 - self.peak_hours_per_day) * 60 * self.baseline_calls_per_min
        return off_peak + self.peak_hours_per_day * 60 * self.peak_calls_per_min


@dataclass(frozen=True)
class Estimate:
    name: str
    task_hours: float
    monthly_usd: float
    detail: str


def task_hourly_cost(vcpu: float, gb: float, prices: Prices) -> float:
    return vcpu * prices.vcpu_hour + gb * prices.gb_hour


def concurrent_calls(calls_per_min: float, a: Assumptions) -> float:
    """Little's law: calls in progress = arrival rate x average duration."""
    return calls_per_min * a.call_minutes


def tasks_for(calls_per_min: float, a: Assumptions) -> int:
    return math.ceil(concurrent_calls(calls_per_min, a) / a.calls_per_task) + a.spare_tasks


def peak_provisioned(a: Assumptions, prices: Prices = X86_US_EAST_1) -> Estimate:
    """Config A: a fixed fleet sized for the campaign peak, running 24x7."""
    tasks = tasks_for(a.peak_calls_per_min, a)
    hours = tasks * a.hours_per_month
    return Estimate(
        "A. Static, sized for peak",
        hours,
        hours * task_hourly_cost(a.task_vcpu, a.task_gb, prices),
        f"{tasks} tasks x {a.hours_per_month:.0f} h",
    )


def scheduled_scaling(a: Assumptions, prices: Prices = X86_US_EAST_1) -> Estimate:
    """Config B: baseline fleet 24x7, scheduled scale-out for the daily window,
    target tracking on CPU as a backstop for unplanned spikes."""
    base = tasks_for(a.baseline_calls_per_min, a)
    peak = tasks_for(a.peak_calls_per_min, a)
    burst_hours_per_day = a.peak_hours_per_day + a.prewarm_hours + a.drain_hours
    hours = base * a.hours_per_month + (peak - base) * burst_hours_per_day * a.days_per_month
    return Estimate(
        "B. Baseline + scheduled scale-out",
        hours,
        hours * task_hourly_cost(a.task_vcpu, a.task_gb, prices),
        f"{base} tasks x {a.hours_per_month:.0f} h + {peak - base} tasks x "
        f"{burst_hours_per_day:g} h/day",
    )


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def report(a: Assumptions | None = None) -> str:
    a = a or Assumptions()
    hourly = task_hourly_cost(a.task_vcpu, a.task_gb, X86_US_EAST_1)
    monthly_calls = a.calls_per_day * a.days_per_month
    lines = [
        f"Task: {a.task_vcpu:g} vCPU / {a.task_gb:g} GB = ${hourly:.5f}/h (x86, us-east-1)",
        f"Concurrency: baseline {concurrent_calls(a.baseline_calls_per_min, a):.0f}, "
        f"peak {concurrent_calls(a.peak_calls_per_min, a):.0f} calls "
        f"({a.call_minutes:g} min avg); {a.calls_per_task} calls/task + "
        f"{a.spare_tasks} spare",
        f"Volume: {a.calls_per_day:,.0f} calls/day, {monthly_calls:,.0f} calls/month",
        "",
        _row(["Configuration", "Fleet", "Task-hours/mo", "x86 $/mo", "ARM $/mo", "$/1k calls"]),
        _row(["---", "---", "---:", "---:", "---:", "---:"]),
    ]
    for fn in (peak_provisioned, scheduled_scaling):
        x86, arm = fn(a, X86_US_EAST_1), fn(a, ARM_US_EAST_1)
        lines.append(
            _row(
                [
                    x86.name,
                    x86.detail,
                    f"{x86.task_hours:,.0f}",
                    f"${x86.monthly_usd:,.2f}",
                    f"${arm.monthly_usd:,.2f}",
                    f"${x86.monthly_usd / monthly_calls * 1000:.3f}",
                ]
            )
        )

    lines += [
        "",
        "Sensitivity (x86 $/mo, A / B):",
        "",
        _row(["Calls per task", "3 min calls", "4 min calls", "6 min calls"]),
        _row(["---:", "---:", "---:", "---:"]),
    ]
    for per_task in (10, 20, 40):
        cells = [str(per_task)]
        for minutes in (3.0, 4.0, 6.0):
            s = replace(a, calls_per_task=per_task, call_minutes=minutes)
            a_usd, b_usd = peak_provisioned(s).monthly_usd, scheduled_scaling(s).monthly_usd
            cells.append(f"${a_usd:,.0f} / ${b_usd:,.0f}")
        lines.append(_row(cells))
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
