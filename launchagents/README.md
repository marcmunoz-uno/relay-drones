# LaunchAgent templates (macOS)

Each `*.plist.template` has placeholders that you substitute for your own
paths before installing. The fastest path:

```bash
# 1. Set these to match your install
export REPO=/path/to/relay-drones
export PY=/usr/bin/python3
export CLAUDE=/path/to/claude    # `which claude` on your machine

# 2. Render every template into ~/Library/LaunchAgents/
for t in "$REPO"/launchagents/*.plist.template; do
  out=~/Library/LaunchAgents/$(basename "${t%.template}")
  sed -e "s|{{REPO}}|$REPO|g" \
      -e "s|{{PY}}|$PY|g" \
      -e "s|{{CLAUDE}}|$CLAUDE|g" "$t" > "$out"
done

# 3. Bootstrap the lot
for plist in ~/Library/LaunchAgents/com.example.relay-drones.*.plist; do
  launchctl bootstrap "gui/$UID" "$plist"
done
```

## What each one does

| File | When | Purpose |
|---|---|---|
| `worker.plist.template` | KeepAlive | The advisor worker. Bootstrap 1-4 copies for parallelism — duplicate the file and bump the `Label` suffix. |
| `triage.plist.template` | StartInterval 300s | Drains `inbox/*.md` into the queue every 5 min. |
| `ingestor-self-prompt.plist.template` | StartInterval 1800s | Reflects on recent completions every 30 min. |
| `ingestor-system-monitor.plist.template` | StartInterval 900s, RunAtLoad | Surfaces new macOS Python crashes every 15 min. |
| `ingestor-daily-prompt.plist.template` | StartCalendarInterval 07:00 | One reflection note per day. |
| `claude-smoketest.plist.template` | StartCalendarInterval 09:15 | Daily `claude -p "OK"` to keep the OAuth refresh token warm. |

## Running multiple worker copies

The plist has a single `Label`. For N parallel workers, copy the file N
times, change `Label` and `StandardOut/ErrorPath` to add a numeric suffix,
and bootstrap each. `claim.claim_next` is race-safe so two workers won't
double-claim a task.

## Uninstall

```bash
for plist in ~/Library/LaunchAgents/com.example.relay-drones.*.plist; do
  launchctl bootout "gui/$UID" "$plist"
done
rm ~/Library/LaunchAgents/com.example.relay-drones.*.plist
```
