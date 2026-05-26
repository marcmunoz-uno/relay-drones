# The OAuth trick

The whole reason relay-drones exists in this shape: you can invoke Claude Code from a script *without paying per token*, by leaning on the OAuth credentials your `claude` CLI already has.

## How Claude Code authenticates

The `claude` CLI checks two sources, in this order:

1. **`ANTHROPIC_API_KEY`** environment variable. If present, billed per-token via the Anthropic Console.
2. **OAuth credentials** in `~/.claude/.credentials.json` (written when you `claude` → `/login`). If present, calls go against your Claude.ai subscription (Pro, Max, etc.). No per-call billing — just rate limits.

The CLI picks whichever it finds first. So if your shell exports `ANTHROPIC_API_KEY` (most developer shells do), every `claude -p` invocation bills per token, even though OAuth is sitting right there.

## The fix

When the worker spawns Claude, build the child's environment *without* `ANTHROPIC_API_KEY`:

```python
import os, subprocess

env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

subprocess.run(
    ["claude", "-p", prompt,
     "--model", "claude-sonnet-4-6",
     "--permission-mode", "bypassPermissions",
     "--output-format", "json",
     "--no-session-persistence"],
    env=env, timeout=600,
)
```

That's it. The parent process can keep `ANTHROPIC_API_KEY` set for whatever else it does. The child can't see it, so OAuth wins.

## Verifying it worked

After running the handoff, the output JSON includes `total_cost_usd`. This value is **informational** when you're on a subscription — it tells you what the call *would* have cost at API rates. You're not actually charged. Subscription billing is rate-limited, not metered.

Confirm OAuth is in play by:

```bash
ANTHROPIC_API_KEY= claude -p "say hi" --output-format json
# If this works, OAuth is set up.
```

If the call fails with an auth error, log in interactively:

```bash
claude
/login    # browser flow with your Claude.ai account
/exit
ls -la ~/.claude/.credentials.json   # should exist
```

## Failure modes to know about

### Token expiration

OAuth tokens refresh automatically when used. If the worker pool sits idle for days the refresh window may close and the next `claude -p` will fail with an auth error until you `/login` interactively again.

**Mitigation:** the `claude-smoketest` LaunchAgent / systemd timer pings `claude -p "OK"` once a day with the cheapest model. Keeps the token warm.

### macOS Keychain on LaunchAgents

Claude Code sometimes stores OAuth in the user keychain rather than (or in addition to) the on-disk credentials file. If the keychain is locked (e.g., right after a reboot before you log in), a LaunchAgent fired pre-login can't read it and `claude -p` fails.

**Mitigation:** check `~/.claude/.credentials.json` exists after `/login`. If it doesn't, your install is keychain-only and LaunchAgent calls before unlock will fail. Workarounds:

- Add `KeepAlive` so the agent retries after unlock.
- Run the workers as a regular user-space service (`launchctl bootstrap gui/$UID ...`) so they only start after login.
- File an issue with Claude Code about disk-backed credentials.

### Subscription rate limits

Max plan has session/weekly caps. 40 escalations/day at ~30s each is comfortably inside typical caps, but if you run something heavier you could hit the rolling 5-hour limit and lock yourself out of interactive use too.

**Mitigation:** `RELAY_DRONES_MAX_CLAUDE_RUNS_PER_DAY=40` is the default. Tune down if you also use Claude Code interactively a lot.

### Anthropic Terms of Service

Claude Code (the CLI) called headlessly *with your subscription* is a supported use case. What's NOT supported is exfiltrating the OAuth token to drive raw Anthropic API calls outside Claude Code — that would breach subscription terms.

The pattern this project uses (`subprocess.run(["claude", ...])`) is fine because it's literally Claude Code making the call. We never touch the OAuth token, we never call the API directly, we just don't set the env var that would override the CLI's default auth.
