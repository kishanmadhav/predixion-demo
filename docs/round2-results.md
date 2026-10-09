# Round 2: live run results (2026-10-09)

One deployment from `terraform apply`, one rehearsal and one recorded campaign, all on 2026-10-09 in ap-southeast-2. All times are India Standard Time (IST, UTC+5:30), the same as the dashboard renders. The models ran on the CPU tier, an approved deviation described in [`cpu-tier.md`](cpu-tier.md).

- **Model host:** m7a.2xlarge running llama.cpp (Qwen2.5-1.5B Q4_K_M) and Speaches (faster-whisper-tiny.en, Kokoro-82M).
- **Fault injection:** 20% throughout, from the chaos proxy in front of the models.
- **Raw outputs and dashboard renders** are in [`round2-evidence/`](round2-evidence). The renders are PNGs from `cloudwatch:GetMetricWidgetImage`, over the run window.

## Timeline (IST, UTC+5:30)

| Time | Event |
|---|---|
| 19:40 | `terraform plan`. `check_iam.py`: no wildcard actions (re-run on the applied state: same result) |
| 19:47 | `apply` finished. The model host's ASG failed once on the account's first use of the Auto Scaling service-linked role, then launched on AWS's own retry; `terraform untaint` + `apply` reconciled it |
| 19:51 | Model host healthy behind the internal NLB, 4 minutes after launch (images, then a ~1 GB GGUF and the Speaches models, then a TTS warm-up) |
| 19:52-19:56 | Rehearsal `smoke:3:8`: 57 turns, 55 completed, 2 degraded, 0 HTTP failures, p50 7.2 s, p99 19.6 s |
| 19:56 | Campaign start: `baseline:10:3,spike:15:8,cooldown:10:3`, 3 turns per call, 30 s apart |
| 20:06 | Spike starts |
| 20:09-20:11 | Target tracking scales the service 2 → 6 → 7 tasks |
| 20:14:07 | Injected LLM outage, 60 s (`scripts/chaos.py outage --stage llm --seconds 60`) |
| 20:14:52 | LLM breaker open (61% failure over its window); degraded turns go to the DLQ |
| 20:18 | LLM breaker closed after half-open probes |
| 20:19-20:25 | DLQ replay in batches: 64 entries, all resolved, 0 pending |
| 20:31 | Campaign ends: 0 HTTP failures |

## Campaign

| Phase | Calls | Turns | Completed | Degraded | HTTP failures | p50 | p90 | p99 |
|---|---|---|---|---|---|---|---|---|
| Baseline, 3 calls/min | 32 | 96 | 94 | 2 | 0 | 6.9 s | 12.8 s | 26.2 s |
| Spike, 8 calls/min, with the LLM outage | 118 | 354 | 294 | 60 | 0 | 10.5 s | 24.8 s | 36.2 s |
| Cooldown, 3 calls/min | 24 | 72 | 72 | 0 | 0 | 9.1 s | 21.5 s | 38.5 s |

- **Failure handling held under real injected load.**
  - Every request got an answer: 522 turns and 0 HTTP failures.
  - A degraded turn is answered with a fallback prompt or a handoff, and dead-lettered. It is never an error to the caller.
- **The injected failures did not degrade turns.** At baseline, 2 of 96 turns degraded despite 20% of provider attempts failing: retries absorbed the rest.
- **Most spike degradation came from the outage and the replay.**
  - 31 of the spike's 60 degraded turns are LLM `server_error` or `circuit_open` from the outage minute.
  - Most of the STT timeouts and `circuit_open`s came from the replay; see the lessons below.

## Autoscaling

- **The signal** is ActiveCalls per task, emitted through EMF. The target is 2 per task, set for the CPU tier's lower call rate.
- **The scale-out** came when the per-task average reached 7-8 at the start of the spike. Desired went 2 → 6 at 20:09:45 and → 7 at 20:10:45; running followed within a minute.
  - See `05-tasks-desired-vs-running-autoscaling.png` and `04-active-calls-scaling-signal.png`.
- **The baseline held flat at the 2-task floor** for its whole 10 minutes, with no flapping.
- **Scale-in** is described in the next section.

## Scale-in

Scale-in is deliberately slow: the target-tracking low alarm needs 15 minutes of low values, and the scale-in cooldown is 180 s.

- **Automatic scale-in after the campaign.** The `AlarmLow` alarm took the service from 7 to 6 tasks at 20:37:37, and from 6 to 5 at 20:41:37. That is one task per cooldown, as configured.
- **Manual reset before the recorded take.** At 20:42 the service was set back to its 2-task floor by hand (`aws ecs update-service --desired-count 2`), so the stack was back at its floor for filming. The task count at 2 is therefore not part of the automatic scale-in.

## Lessons for the runbook

1. **A DLQ replay competes with live traffic.**
   - What happened: replaying 64 turns at 20:19, in the middle of the spike, saturated STT on the CPU host. Its breaker opened from 20:20 to 20:25, and p99 peaked near 35 s, just under the 36 s alarm line (3 × the 12 s baseline).
   - Change: on a constrained model tier, replay after the campaign window, or in small batches (`limit=10`).
2. **Large replay batches outlive the ALB idle timeout.**
   - What happened: a `limit=50` batch on CPU takes longer than the ALB's 60 s idle timeout. The client got a 504 while the server finished the batch: every entry was resolved.
   - Change: use `limit=10` on the CPU tier.
3. **A new account's first Auto Scaling group can fail once.** The service-linked role is still propagating. Re-run `apply` after `terraform untaint 'aws_autoscaling_group.model_host[0]'`, or wait a minute and apply again.
4. **The allow-listed operator IP can change mid-session** on a home connection. The ALB then times out, which looks like an outage. Check `curl -s https://checkip.amazonaws.com` against `allowed_cidrs` first.
