"""system_monitor — scan macOS for Python crashes and queue investigation notes.

This ingestor is an EXAMPLE. It surfaces one class of operational pain
(Python `.ips` crash reports under ~/Library/Logs/DiagnosticReports/) and
queues an inbox note per new crash. Use it as a template for your own
event sources: log scanners, healthcheck pollers, queue-depth watchers,
Slack DMs, RSS feeds — anything that produces text and a stable dedup key.

Dedup is by `(coalition, top faulting frame)` so flapping crashes from
the same root cause don't spam the inbox.

Linux users: this won't find anything. Replace with a journald scanner,
systemd `--failed` parser, or whatever your platform exposes.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from relay_drones.config import INBOX
from relay_drones.ingestors._lib import load_state, save_state, write_note

INGESTOR = "system_monitor"
CRASH_DIR = Path.home() / "Library" / "Logs" / "DiagnosticReports"

CRASH_LOOKBACK_HOURS = 6
MAX_NEW_NOTES = 5
INBOX_BACKPRESSURE = 8


def _pending_inbox_count() -> int:
    return sum(1 for p in INBOX.glob("*.md") if p.parent == INBOX)


def _recent_crashes(since: datetime) -> list[dict]:
    if not CRASH_DIR.exists():
        return []
    out = []
    for p in sorted(CRASH_DIR.glob("Python-*.ips")):
        # Filename embeds the timestamp: Python-YYYY-MM-DD-HHMMSS[.NNN].ips
        m = re.match(r"Python-(\d{4})-(\d{2})-(\d{2})-(\d{6})", p.name)
        if not m:
            continue
        y, mo, d, hms = m.groups()
        try:
            ts = datetime(
                int(y), int(mo), int(d),
                int(hms[:2]), int(hms[2:4]), int(hms[4:6]),
                tzinfo=timezone.utc,
            )
        except ValueError:
            continue
        if ts < since:
            continue
        try:
            with open(p) as f:
                f.readline()  # first line is a JSON header, skip
                body = json.load(f)
            coal = body.get("coalitionName") or "?"
            ft = body.get("faultingThread", 0)
            threads = body.get("threads", [])
            top = "?"
            if ft < len(threads):
                frames = threads[ft].get("frames", [])
                if frames:
                    ii = frames[0].get("imageIndex")
                    img = (
                        body.get("usedImages", [])[ii]["name"]
                        if ii is not None
                        else "?"
                    )
                    top = f"{img}: {frames[0].get('symbol','')}"
            subtype = body.get("exception", {}).get("subtype", "")
            out.append({
                "file": p.name,
                "ts": ts.isoformat(),
                "coalition": coal,
                "top": top,
                "subtype": subtype,
            })
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _crash_key(c: dict) -> str:
    return f"crash:{c['coalition']}:{c['top']}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if _pending_inbox_count() >= INBOX_BACKPRESSURE:
        print(f"[system_monitor] inbox has {_pending_inbox_count()} pending — skip")
        return 0

    state = load_state(INGESTOR)
    seen: set[str] = set(state.get("seen_keys") or [])

    since = datetime.now(timezone.utc) - timedelta(hours=CRASH_LOOKBACK_HOURS)
    crashes = _recent_crashes(since)

    new = []
    for c in crashes:
        k = _crash_key(c)
        if k not in seen:
            new.append((k, c))

    if not new:
        print("[system_monitor] no new crashes since last run")
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        if not args.dry_run:
            save_state(INGESTOR, state)
        return 0

    new = new[:MAX_NEW_NOTES]
    written = 0
    for key, item in new:
        title = f"Investigate Python crash in {item['coalition']}"
        body = (
            "A Python segfault was recorded on this machine.\n"
            f"- IPS file: `~/Library/Logs/DiagnosticReports/{item['file']}`\n"
            f"- Coalition: `{item['coalition']}`\n"
            f"- Top faulting frame: `{item['top']}`\n"
            f"- Exception subtype: `{item['subtype']}`\n\n"
            "Read the IPS (parse JSON from the second line), identify the "
            "faulting thread (`body['faultingThread']`), and propose the "
            "smallest fix that prevents this class of crash. Focus on the "
            "identified module — don't recommend broader changes."
        )
        if args.dry_run:
            print(f"  [dry-run] would write: {title}")
        else:
            p = write_note(title, body, priority=2, source="system_monitor")
            print(f"  wrote {p.name}")
            written += 1
        seen.add(key)

    if not args.dry_run:
        state["seen_keys"] = list(seen)[-500:]
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        state["last_written"] = written
        save_state(INGESTOR, state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
