# Why ap-south-1 (Mumbai): notes for a collections workload

Short version: the callers and borrowers are in India, the data is personal and financial, and the whole stack including the models runs in one in-country region.

> **Where the demo actually ran.** The demo account's organization SCP allows workloads only in ap-southeast-2 (Sydney), so the recorded deployment is in Sydney (`region = "ap-southeast-2"`, `az_ids = ["apse2-az1", "apse2-az2"]`). The Terraform is region-agnostic; the notes below describe the production choice, Mumbai. With synthetic data only, running the demo outside India raises no residency issue.

## Borrower data stays in India

- **DPDP Act 2023.** Borrower names, phone numbers, repayment status and call audio are personal data. Keeping them in an Indian region avoids the cross-border transfer question altogether, instead of relying on transfer rules and any restrictions the government may notify later.
- **RBI, April 2018 directive on storage of payment system data.** Payment-system data must be stored only in India. Collections systems touch loan and repayment data that can fall under it, so the safe design is no copy outside India.
- **What lives where.** DynamoDB (turns, transcripts, dead letters), CloudWatch Logs and metrics, ECR images, the ALB, the ECS tasks and the GPU host running the models are all in ap-south-1.
- **No third-party model API.** STT, LLM and TTS are open-weight models (faster-whisper, Qwen2.5-7B-Instruct-AWQ on vLLM, Kokoro) that run on our own GPU instance. A hosted model API would send transcripts to someone else's servers, often outside India. Here a transcript never leaves the region.
- **What does leave the region:** only the model weights and container images coming in (Hugging Face, Docker Hub, GHCR through the NAT gateway). No borrower data goes out.

## Demo access model

The demo ALB is HTTP-only and unauthenticated. It is restricted by security group to `allowed_cidrs` (a single /32, the operator's IP) and serves synthetic data only, so access control is the network allow-list, not IAM. The DLQ and admin endpoints share that public listener. For production: an HTTPS listener with an ACM certificate, service-to-service auth (SigV4 or mTLS) or a private ALB behind the telephony layer, and the DLQ and admin endpoints off the public listener. IAM governs the AWS control plane and the tasks' access to DynamoDB, not callers of the HTTP API.

## Logs contain transcripts

- The service logs and the dead-letter records hold transcripts. Log retention is 7 days (`log_retention_days`), and access to the logs and the DynamoDB table is limited to the IAM roles and users in this account. The HTTP API itself is not IAM-protected (see "Demo access model").
- For production, mask PAN and Aadhaar numbers (and phone numbers where possible) before anything is logged or stored, and set the retention to what policy requires, not to the demo's 7 days.

## Latency to callers

Mumbai is close to the large Indian metros, so the network leg of a turn should be small next to model time. Do not quote a figure from a vendor page. Measure it from where the callers are, or from your own laptop for the demo, and write down what you get. The numbers will come from the demo run, so none are quoted here.

```bash
URL=$(terraform -chdir=infra output -raw url)
for i in $(seq 20); do
  curl -s -o /dev/null -w 'connect=%{time_connect}s ttfb=%{time_starttransfer}s total=%{time_total}s\n' $URL/health
done
```

`/health` does no model or store work, so `connect` and `ttfb` are the network plus the ALB plus the task. Turn latency through the models is on the dashboard ("Turn latency percentiles (ms)").

## GPU availability and subnets by AZ ID

g5 instances are offered in only two of Mumbai's Availability Zones, the ones with IDs `aps1-az1` and `aps1-az3`. AZ names (ap-south-1a, 1b, 1c) map to different IDs in different accounts, so the same name can be a zone without g5 in your account. The Terraform therefore takes AZ IDs (`az_ids`, default `["aps1-az1", "aps1-az3"]`) and creates the private subnets in those zones. That guarantees the GPU Auto Scaling group can launch in either subnet, and the Fargate tasks use the same two AZs. In Sydney, where the demo ran, g5 is offered in `apse2-az1` and `apse2-az2` (checked 2026-10-08), which is the current default.

## Disaster recovery: ap-south-2 (Hyderabad)

For in-country DR, ap-south-2 is the second Indian region, so a regional failure does not force a move of borrower data abroad. A warm-standby design would be:

- DynamoDB global tables, or point-in-time recovery with a restore into ap-south-2;
- the same Terraform with `region = "ap-south-2"` (the g5 AZ IDs there must be checked first, and the G-instance quota requested separately);
- DNS failover in front of the two ALBs.

This is not built in the exercise.

## Price in the region

On-demand prices in ap-south-1 differ from us-east-1. Fargate is about 5% above us-east-1, which is small next to the choice of destroying the stack between sessions. The cost table in the README uses the ap-south-1 prices checked on 2026-10-07.
