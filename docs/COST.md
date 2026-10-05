# Fargate cost estimate

All figures come from [`cost/fargate_cost.py`](../cost/fargate_cost.py), which you can rerun with `uv run python -m cost.fargate_cost`. Pricing is us-east-1 Linux on-demand: **$0.04048/vCPU-h + $0.004445/GB-h** (checked 2026-10-05).

**Assumptions.** 10 calls/min for 22 h plus 50 calls/min for 2 h, every day: 19,200 calls/day, about 584k per 730-hour month. With 4-minute calls, Little's law gives **40 concurrent calls at baseline and 200 at peak**. Each task is 1 vCPU / 2 GB ($0.04937/h) and carries **20 concurrent calls**: the orchestrator mostly waits on STT/LLM/TTS, so it is I/O-bound. Adding one spare task for AZ redundancy gives **3 tasks at baseline and 11 at peak**.

| Configuration | Fleet | x86 $/mo | Graviton $/mo |
|---|---|---:|---:|
| **A. Static, sized for peak** | 11 tasks, 24×7 | **$396** | $317 |
| **B. Baseline + scheduled scale-out** | 3 tasks 24×7, +8 tasks for 2.5 h/day | **$138** | $111 |

The 2.5 h covers the 2 h window, 15 min of pre-warm and 15 min of drain.

**Recommendation: B.** It is 65% cheaper (≈ $0.24 vs $0.68 per 1,000 calls), and this spike is predictable. A scheduled scaling action raises the task minimum to 11 fifteen minutes before the window and lowers it afterwards, with CPU target tracking kept as a backstop. Reactive scaling alone would lag the 5× jump by 3–5 minutes (metric period, alarm evaluation, 30–60 s task start-up), and that lag lands exactly as the calls arrive. A deregistration delay longer than a call keeps scale-in from cutting live calls off. A only makes sense if campaign times are unpredictable. Graviton saves another 20%. Fargate Spot does not suit live calls, because its 2-minute interruption drops them.

**Sensitivity.** Calls per task drives the result. At 10 calls/task, A costs $757 and B costs $240; at 40, A costs $216 and B costs $87. B wins in every case, and `voice-agent loadtest` measures the real figure. ALB, NAT and logs (~$50–100/mo) are the same in both configurations and excluded. GPU inference for self-hosted models costs more than either fleet, since Fargate has no GPUs.
