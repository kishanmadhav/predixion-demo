# Fargate cost estimate

All figures come from [`cost/fargate_cost.py`](../cost/fargate_cost.py) (`uv run python -m cost.fargate_cost`).
Prices are us-east-1, Linux, on-demand, checked 2026-10-05: **$0.04048 per vCPU-hour and $0.004445 per GB-hour**, billed per second.

**Assumptions.**
- **Load:** 10 calls/min for 22 h and 50 calls/min for 2 h, every day. That is 19,200 calls/day, or about 584k/month over a 730-hour month.
- **Concurrency:** with a 4-minute average call, Little's law gives **40 calls in progress at baseline and 200 at peak**. Collections handle times are 4–6 min including wrap-up, and an AI agent has no wrap-up.
- **Task size:** each task is 1 vCPU / 2 GB, so **$0.04937 per hour**.
- **Task capacity:** I plan on **20 concurrent calls per task**. The orchestrator is I/O-bound: it relays audio and waits on STT, LLM and TTS. This number needs a load test.
- **Spare:** one extra task for AZ redundancy and deploys. That gives **3 tasks at baseline and 11 at peak**.

| Configuration | Fleet | Task-hours/mo | x86 $/mo | Graviton $/mo |
|---|---|---:|---:|---:|
| **A. Static, sized for peak** | 11 tasks, 24×7 | 8,030 | **$396** | $317 |
| **B. Baseline + scheduled scale-out** | 3 tasks 24×7, plus 8 tasks for 2.5 h/day | 2,798 | **$138** | $111 |

In config B, each day's burst covers the 2 h window plus 15 min to pre-warm before it and 15 min to drain after it.

**Recommendation: B.** It is about 65% cheaper, saving ~$258/month (≈ $0.24 vs $0.68 per 1,000 calls), and the spike is the easy kind: it is scheduled.
- **Scaling setup:** an Application Auto Scaling *scheduled action* raises the task minimum to 11 fifteen minutes before the campaign window and lowers it to 3 after the window ends. CPU target tracking at ~60% stays on as a backstop for unplanned spikes.
- **Why reactive scaling alone isn't enough:** without the schedule, target tracking would lag the 10→50 calls/min step by roughly 3–5 minutes. That lag comes from 1-minute metrics, alarm evaluation and 30–60 s task start-up, and it falls exactly when the calls arrive.
- **Scaling in:** use a deregistration delay longer than a call, plus scale-in protection while tasks have live calls, so scaling in never cuts a call off.
- **When A is better:** only if campaign times are unpredictable.
- **Graviton/ARM** saves a further 20%.
- **Fargate Spot** (~70% off) is not safe for live calls, because its 2-minute interruption drops them. It is fine for the dead-letter replay workers.

**Sensitivity.** Capacity per task drives the result more than anything else. At 10 calls/task, A costs $757 and B $240. At 40 calls/task, A costs $216 and B $87. B wins in every case. `voice-agent loadtest` is how to measure the real capacity.

**Excluded, because they are the same for both configurations:**
- ALB: ~$16/mo plus LCUs.
- NAT gateway: ~$33/mo plus $0.045/GB if the providers are external.
- CloudWatch Logs: $0.50/GB.

**Inference is the larger cost.** Fargate has no GPUs. Self-hosted open-weight STT, LLM and TTS would run on GPU EC2/EKS nodes, and a single always-on GPU instance (≈ $1/h for a g5.xlarge; check current pricing) costs more per month than either Fargate fleet.
