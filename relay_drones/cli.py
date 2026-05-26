"""relay-drones CLI — drop notes, run triage, run a worker, render reports.

Installed as the `relay-drones` console script (see pyproject.toml).

    relay-drones drop "<title>" "<body>"        # write an inbox note
    relay-drones triage                          # drain inbox into queue
    relay-drones worker <role> [--once|--no-loop]
    relay-drones report [--since 6h]
    relay-drones status                          # queue counts + budget
    relay-drones budget                          # show today's handoff budget
"""
from __future__ import annotations

import argparse
import sys

from relay_drones import queue, reporter, triage, worker
from relay_drones.config import ROLES, MAX_CLAUDE_RUNS_PER_DAY
from relay_drones.ingestors._lib import write_note
from relay_drones.lib.claude_handoff import budget_status


def cmd_drop(args) -> int:
    path = write_note(args.title, args.body, priority=args.priority, source="cli")
    print(path)
    return 0


def cmd_triage(args) -> int:  # noqa: ARG001
    return triage.main()


def cmd_worker(args) -> int:
    pass_args = [args.role]
    if args.once:
        pass_args.append("--once")
    if args.no_loop:
        pass_args.append("--no-loop")
    return worker.main(pass_args)


def cmd_report(args) -> int:
    return reporter.main(["--since", args.since])


def cmd_status(args) -> int:  # noqa: ARG001
    print("# relay-drones status")
    print()
    print("## queue")
    for status, n in sorted(queue.status_counts().items()):
        print(f"  {status:>12} : {n}")
    print()
    used, cap = budget_status()
    print(f"## claude handoff budget: {used}/{cap} (UTC day)")
    print()
    print("## roles")
    for name, cfg in ROLES.items():
        models = cfg["model"] if isinstance(cfg["model"], (list, tuple)) else [cfg["model"]]
        print(f"  {name} — primary: {models[0]}; {len(models)-1} fallback(s)")
    return 0


def cmd_budget(args) -> int:  # noqa: ARG001
    used, cap = budget_status()
    print(f"{used}/{cap} (cap from RELAY_DRONES_MAX_CLAUDE_RUNS_PER_DAY="
          f"{MAX_CLAUDE_RUNS_PER_DAY})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="relay-drones")
    sub = p.add_subparsers(dest="cmd", required=True)

    drop = sub.add_parser("drop", help="write an inbox note from the CLI")
    drop.add_argument("title")
    drop.add_argument("body")
    drop.add_argument("--priority", type=int, default=3)
    drop.set_defaults(func=cmd_drop)

    t = sub.add_parser("triage", help="drain inbox/*.md into the task queue")
    t.set_defaults(func=cmd_triage)

    w = sub.add_parser("worker", help="run a worker for one role")
    w.add_argument("role")
    w.add_argument("--once", action="store_true")
    w.add_argument("--no-loop", action="store_true")
    w.set_defaults(func=cmd_worker)

    r = sub.add_parser("report", help="markdown digest of recent tasks")
    r.add_argument("--since", default="24h")
    r.set_defaults(func=cmd_report)

    s = sub.add_parser("status", help="queue + budget snapshot")
    s.set_defaults(func=cmd_status)

    b = sub.add_parser("budget", help="show today's claude-handoff budget")
    b.set_defaults(func=cmd_budget)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
