"""Command-line entry point: `voice-agent <command>`.

serve        run the voice-agent HTTP service
mock         run the mock STT/LLM/TTS provider
dlq          inspect and replay the dead-letter queue (reads the SQLite file directly)
loadtest     drive synthetic calls through a running service and report
chaos-demo   baseline -> outage -> recovery -> replay walkthrough
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from voice_agent.config import Settings
from voice_agent.store import DEAD_LETTER_STATUSES, DeadLetter, open_store


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # One INFO line per provider request drowns the signal; failures are logged by us.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    from voice_agent.api import create_app

    settings = Settings()
    _configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=args.host or settings.host,
        port=args.port or settings.port,
        log_level=settings.log_level.lower(),
    )


def _mock(args: argparse.Namespace) -> None:
    import uvicorn

    from mock_provider.app import MockConfig, create_app

    config = MockConfig.from_env()
    if args.failure_rate is not None:
        config.failure_rate = args.failure_rate
    if args.seed is not None:
        config.seed = args.seed
    _configure_logging("INFO")
    uvicorn.run(create_app(config), host=args.host, port=args.port, log_level="warning")


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _detail(entry: DeadLetter) -> dict[str, Any]:
    return {
        **entry.summary(),
        "error_detail": entry.error_detail,
        "partial": entry.partial,
        "attempts": entry.attempts,
        "last_replay_error": entry.last_replay_error,
        "replay_attempts": entry.replay_attempts,
        "payload_bytes": len(json.dumps(entry.payload)),
    }


async def _dlq(args: argparse.Namespace) -> int:
    settings = Settings()
    if args.dlq_command == "replay":
        from voice_agent.wiring import open_runtime

        # recover=False: the service may be running; only it may reclaim in-flight work.
        runtime = await open_runtime(settings, recover=False)
        try:
            if args.all:
                outcomes = await runtime.dlq.replay_pending(limit=args.limit)
            else:
                outcomes = [await runtime.dlq.replay(i) for i in args.ids]
        finally:
            await runtime.close()
        for o in outcomes:
            print(f"{o.id:>6}  {o.status:<15} {o.error or ''}")
        return 0 if all(o.status == "resolved" for o in outcomes) else 1

    store = await open_store(settings)
    try:
        if args.dlq_command == "list":
            entries = await store.list_dead_letters(status=args.status, limit=args.limit)
            if args.json:
                _print_json([e.summary() for e in entries])
                return 0
            counts = await store.dead_letter_counts()
            print("counts: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
            print(f"{'id':>6}  {'status':<10} {'stage':<9} {'error':<14} {'replays':>7}  call/turn")
            for e in entries:
                print(
                    f"{e.id:>6}  {e.status:<10} {e.failed_stage or '-':<9} "
                    f"{e.error_kind or e.reason:<14} {e.replay_count:>7}  "
                    f"{e.call_id}/{e.turn_id}"
                )
            return 0
        entry = await store.get_dead_letter(args.id)
        if entry is None:
            print(f"dead letter {args.id} not found", file=sys.stderr)
            return 1
        _print_json(_detail(entry))
        return 0
    finally:
        await store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voice-agent",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the voice-agent service")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)

    mock = sub.add_parser("mock", help="run the mock STT/LLM/TTS provider")
    mock.add_argument("--host", default="127.0.0.1")
    mock.add_argument("--port", type=int, default=9000)
    mock.add_argument("--failure-rate", type=float)
    mock.add_argument("--seed", type=int)

    dlq = sub.add_parser("dlq", help="inspect and replay dead letters")
    dlq_sub = dlq.add_subparsers(dest="dlq_command", required=True)
    ls = dlq_sub.add_parser("list")
    ls.add_argument("--status", choices=DEAD_LETTER_STATUSES)
    ls.add_argument("--limit", type=int, default=50)
    ls.add_argument("--json", action="store_true")
    show = dlq_sub.add_parser("show")
    show.add_argument("id", type=int)
    replay = dlq_sub.add_parser("replay")
    target = replay.add_mutually_exclusive_group(required=True)
    target.add_argument("ids", type=int, nargs="*", default=[])
    target.add_argument("--all", action="store_true", help="replay every pending entry")
    replay.add_argument("--limit", type=int, default=100)

    load = sub.add_parser("loadtest", help="drive synthetic calls through a running service")
    load.add_argument("--url", default="http://127.0.0.1:8080")
    load.add_argument(
        "--mock-url",
        default="http://127.0.0.1:9000",
        help="mock provider admin URL ('' to skip provider stats)",
    )
    load.add_argument("--calls", type=int, default=50)
    load.add_argument("--turns", type=int, default=4)
    load.add_argument("--concurrency", type=int, default=20)
    load.add_argument("--no-reset", action="store_true", help="keep existing mock stats")

    demo = sub.add_parser("chaos-demo", help="baseline -> outage -> recovery -> replay")
    demo.add_argument("--url", default="http://127.0.0.1:8080")
    demo.add_argument("--mock-url", default="http://127.0.0.1:9000")
    demo.add_argument("--outage-s", type=float, default=12.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        _serve(args)
        return 0
    if args.command == "mock":
        _mock(args)
        return 0
    if args.command == "dlq":
        return asyncio.run(_dlq(args))
    if args.command == "loadtest":
        from voice_agent.loadgen import loadtest

        print(
            asyncio.run(
                loadtest(
                    args.url,
                    args.mock_url or None,
                    calls=args.calls,
                    turns_per_call=args.turns,
                    concurrency=args.concurrency,
                    reset_mock=not args.no_reset,
                )
            )
        )
        return 0
    if args.command == "chaos-demo":
        from voice_agent.loadgen import chaos_demo

        asyncio.run(chaos_demo(args.url, args.mock_url, outage_s=args.outage_s))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
