"""Builds docs/writeup.pdf: the one-page cost write-up and model-swap note.

    uv run --with reportlab python docs/writeup_pdf.py

The cost figures come from cost/fargate_cost.py (they are computed here, not typed in),
so the PDF cannot drift from the calculator.
"""

from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cost.fargate_cost import (  # noqa: E402
    ARM_US_EAST_1,
    X86_US_EAST_1,
    Assumptions,
    concurrent_calls,
    peak_provisioned,
    scheduled_scaling,
    task_hourly_cost,
    tasks_for,
)

OUT = ROOT / "docs" / "writeup.pdf"
INK = colors.HexColor("#1f2328")
MUTED = colors.HexColor("#57606a")
ACCENT = colors.HexColor("#0b5cad")
RULE = colors.HexColor("#d0d7de")

base = ParagraphStyle(
    "base", fontName="Helvetica", fontSize=9.6, leading=13.2, textColor=INK, alignment=TA_LEFT
)
title = ParagraphStyle(
    "title", parent=base, fontName="Helvetica-Bold", fontSize=15, leading=18, spaceAfter=2
)
subtitle = ParagraphStyle("subtitle", parent=base, textColor=MUTED, fontSize=8.5, spaceAfter=8)
h2 = ParagraphStyle(
    "h2",
    parent=base,
    fontName="Helvetica-Bold",
    fontSize=11,
    leading=14,
    textColor=ACCENT,
    spaceBefore=8,
    spaceAfter=4,
)
body = ParagraphStyle("body", parent=base, spaceAfter=5)
small = ParagraphStyle("small", parent=base, fontSize=8.4, leading=11, textColor=MUTED)
cell = ParagraphStyle("cell", parent=base, fontSize=9, leading=11.5)
cell_b = ParagraphStyle("cell_b", parent=cell, fontName="Helvetica-Bold")


def usd(x: float) -> str:
    return f"${x:,.0f}"


def build() -> None:
    a = Assumptions()
    hourly = task_hourly_cost(a.task_vcpu, a.task_gb, X86_US_EAST_1)
    base_tasks, peak_tasks = (
        tasks_for(a.baseline_calls_per_min, a),
        tasks_for(a.peak_calls_per_min, a),
    )
    monthly_calls = a.calls_per_day * a.days_per_month
    pa, pb = peak_provisioned(a), scheduled_scaling(a)
    pa_arm, pb_arm = peak_provisioned(a, ARM_US_EAST_1), scheduled_scaling(a, ARM_US_EAST_1)
    saving = pa.monthly_usd - pb.monthly_usd
    burst = a.peak_hours_per_day + a.prewarm_hours + a.drain_hours

    def sens(per_task: int) -> tuple[str, str]:
        from dataclasses import replace

        s = replace(a, calls_per_task=per_task)
        return usd(peak_provisioned(s).monthly_usd), usd(scheduled_scaling(s).monthly_usd)

    s10, s40 = sens(10), sens(40)

    story = [
        Paragraph("Voice agent: cost write-up and model-swap note", title),
        Paragraph(
            "Round 1, local track &nbsp;|&nbsp; github.com/kishanmadhav/predixion-demo"
            " &nbsp;|&nbsp; figures computed by cost/fargate_cost.py",
            subtitle,
        ),
        Paragraph("1. AWS Fargate cost estimate (us-east-1, Linux, on-demand)", h2),
        Paragraph(
            "<b>Pricing:</b> $0.04048 per vCPU-hour and $0.004445 per GB-hour (checked "
            "2026-10-05), billed per second. "
            f"<b>Load:</b> 10 calls/min for 22 h plus 50 calls/min for 2 h every day, i.e. "
            f"{a.calls_per_day:,.0f} calls/day and about {monthly_calls / 1000:,.0f}k per "
            "730-hour month. "
            f"<b>Concurrency:</b> with {a.call_minutes:g}-minute calls, Little's law gives "
            f"{concurrent_calls(a.baseline_calls_per_min, a):.0f} calls in progress at baseline "
            f"and {concurrent_calls(a.peak_calls_per_min, a):.0f} at peak. "
            f"<b>Tasks:</b> 1 vCPU / 2 GB (${hourly:.5f}/h), each carrying "
            f"{a.calls_per_task} concurrent calls, because the orchestrator is I/O-bound and "
            "mostly waits on STT, LLM and TTS. One spare task for AZ redundancy gives "
            f"<b>{base_tasks} tasks at baseline and {peak_tasks} at peak</b>.",
            body,
        ),
    ]

    rows = [
        [
            Paragraph("Configuration", cell_b),
            Paragraph("Fleet", cell_b),
            Paragraph("Task-hours/mo", cell_b),
            Paragraph("x86 $/mo", cell_b),
            Paragraph("Graviton $/mo", cell_b),
        ],
        [
            Paragraph("A. Static, sized for peak", cell),
            Paragraph(f"{peak_tasks} tasks, 24x7", cell),
            Paragraph(f"{pa.task_hours:,.0f}", cell),
            Paragraph(f"<b>{usd(pa.monthly_usd)}</b>", cell),
            Paragraph(usd(pa_arm.monthly_usd), cell),
        ],
        [
            Paragraph("B. Baseline + scheduled scale-out", cell),
            Paragraph(
                f"{base_tasks} tasks 24x7, +{peak_tasks - base_tasks} tasks for {burst:g} h/day",
                cell,
            ),
            Paragraph(f"{pb.task_hours:,.0f}", cell),
            Paragraph(f"<b>{usd(pb.monthly_usd)}</b>", cell),
            Paragraph(usd(pb_arm.monthly_usd), cell),
        ],
    ]
    table = Table(rows, colWidths=[4.3 * cm, 5.4 * cm, 3.0 * cm, 1.9 * cm, 2.7 * cm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f6f8fa")),
                ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#eef6ee")),
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, RULE),
                ("LINEBELOW", (0, 1), (-1, 1), 0.4, RULE),
                ("BOX", (0, 0), (-1, -1), 0.6, RULE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    story += [
        table,
        Paragraph(
            f"The {burst:g} h/day burst in B covers the 2 h campaign window, 15 minutes of "
            "pre-warm before it and 15 minutes of drain after it.",
            small,
        ),
        Spacer(1, 5),
        Paragraph(
            f"<b>Recommendation: B.</b> It is {saving / pa.monthly_usd:.0%} cheaper (about "
            f"{usd(saving)}/month, or ${pb.monthly_usd / monthly_calls * 1000:.2f} vs "
            f"${pa.monthly_usd / monthly_calls * 1000:.2f} per 1,000 calls), and the spike is "
            "predictable. A scheduled scaling action raises the task minimum to "
            f"{peak_tasks} fifteen minutes before the window and lowers it to {base_tasks} "
            "afterwards, with CPU target tracking kept as a backstop for unplanned spikes. "
            "Reactive scaling alone would lag the 5x jump by 3-5 minutes (metric period, alarm "
            "evaluation, 30-60 s task start-up), and that lag lands exactly as the calls arrive. "
            "A deregistration delay longer than a call keeps scale-in from cutting live calls "
            "off. A only makes sense if campaign times are unpredictable. Graviton saves a "
            "further 20%. Fargate Spot does not suit live calls, because its 2-minute "
            "interruption drops them; it is fine for dead-letter replay workers.",
            body,
        ),
        Paragraph(
            "<b>Sensitivity.</b> Calls per task drives the result most. At 10 calls/task, A "
            f"costs {s10[0]} and B {s10[1]}; at 40, A costs {s40[0]} and B {s40[1]}. B wins in "
            "every case, and <font face='Courier'>voice-agent loadtest</font> measures the real "
            "figure. ALB, NAT gateway and logs (about $50-100/month) are the same in both "
            "configurations and are excluded. Self-hosted GPU inference for STT/LLM/TTS would "
            "cost more than either fleet, since Fargate has no GPUs.",
            body,
        ),
        Paragraph("2. Swapping the mock for a real open-weight model", h2),
        Paragraph(
            "The resilience layer and the pipeline depend on only two things: three one-method "
            "Protocols in <font face='Courier'>providers/base.py</font> (<font face='Courier'>"
            "transcribe(audio) -&gt; str</font>, <font face='Courier'>complete(messages) -&gt; "
            "str</font>, <font face='Courier'>synthesize(text) -&gt; bytes</font>) and the "
            "<font face='Courier'>ProviderError</font> taxonomy. <font face='Courier'>"
            "providers/factory.py</font> is the only code that knows which adapter backs each "
            "stage, so replacing the mock with an open-weight stack is a configuration change: "
            "set <font face='Courier'>{STT,LLM,TTS}_PROVIDER=openai</font> and point each "
            "<font face='Courier'>*_BASE_URL</font> at a server that speaks the "
            "OpenAI-compatible API, such as Speaches running faster-whisper for STT, vLLM, "
            "Ollama or llama.cpp serving Qwen or Llama for the LLM, and Kokoro-FastAPI for TTS. "
            "The <font face='Courier'>openai_compat</font> adapters already build those "
            "requests and classify those servers' failures (Ollama's queue-full 503 and "
            "llama.cpp's model-loading 503 are retried; Speaches' 403 for a bad key is not). "
            "Each stage can be swapped on its own, and <font face='Courier'>"
            "docker-compose.openweight.yml</font> runs the full open-weight stack on CPU by "
            "changing only environment variables. A model that runs in-process (say "
            "<font face='Courier'>faster_whisper.WhisperModel</font> called through "
            "<font face='Courier'>asyncio.to_thread</font>), or a server with a different API, "
            "needs one new adapter of about 40 lines that implements the Protocol and raises "
            "<font face='Courier'>ProviderError</font>; retries, circuit breakers, deadlines and "
            "dead-lettering then apply to it unchanged, with no edits to the resilience layer "
            "or the pipeline.",
            body,
        ),
        Paragraph(
            "Verified: the adapters are contract-tested against each server's documented "
            "request and response shapes. Not run here: the open-weight compose stack, which "
            "needs multi-GB model downloads. With real models, raise LLM_TIMEOUT_S and "
            "TURN_DEADLINE_S to match measured latency.",
            small,
        ),
    ]

    doc = SimpleDocTemplate(
        str(OUT),
        pagesize=A4,
        leftMargin=1.7 * cm,
        rightMargin=1.7 * cm,
        topMargin=1.4 * cm,
        bottomMargin=1.2 * cm,
        title="Cost write-up and model-swap note",
        author="Kishan Madhav",
    )
    doc.build(story)


if __name__ == "__main__":
    build()
    print(f"wrote {OUT}")
