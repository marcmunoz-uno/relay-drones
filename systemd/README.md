# systemd units (Linux)

User units, installed under `~/.config/systemd/user/`. Use template form
(`@.service` + `.timer`) so you can run N parallel workers without
duplicating files.

## Install

```bash
mkdir -p ~/.config/systemd/user

# Render templates
export REPO=/path/to/relay-drones
export PY=/usr/bin/python3

for t in "$REPO"/systemd/*.service "$REPO"/systemd/*.timer; do
  out=~/.config/systemd/user/$(basename "$t")
  sed -e "s|{{REPO}}|$REPO|g" -e "s|{{PY}}|$PY|g" "$t" > "$out"
done

systemctl --user daemon-reload

# Workers (start 4 in parallel)
for n in 1 2 3 4; do
  systemctl --user enable --now relay-drones-worker@triage_workers-$n.service
done

# Triage every 5 min
systemctl --user enable --now relay-drones-triage.timer

# Daily Claude OAuth keepalive
systemctl --user enable --now relay-drones-claude-smoketest.timer

# Linger so user units survive logout
sudo loginctl enable-linger "$USER"
```

## Set the OpenRouter key

Put it in `~/.config/relay-drones/env`:

```ini
OPENROUTER_API_KEY=sk-or-v1-...
```

The unit files load this via `EnvironmentFile=`.
