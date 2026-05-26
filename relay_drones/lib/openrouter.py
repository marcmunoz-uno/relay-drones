"""OpenRouter chat-completion client for the advisor pool.

Why this exists: routing through OpenRouter unifies dozens of models behind
one API key with one bill. When the primary model is rate-limited or
out-of-credit, the worker falls back through a chain instead of dying.

Same envelope as a typical chat-completion wrapper so the worker doesn't
care which backend it's hitting:

    {
        "raw":            str,   # the model's text reply
        "artifact_path":  str,   # markdown trace dropped on disk
        "provider":       str,   # "openrouter:<model_id>"
        "exit_code":      int,
    }

Key precedence: explicit `api_key=` kwarg > `OPENROUTER_API_KEY` env.
No file fallback by design — keys never live in the repo or in dotfiles
the package owns. Set the env var (or source .env) before running.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from relay_drones.config import ARTIFACTS

API_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterError(RuntimeError):
    """Raised on transport / HTTP errors. Caller can fall back to another model."""


def _load_api_key(explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("OPENROUTER_API_KEY")
    if env:
        return env
    raise OpenRouterError(
        "OPENROUTER_API_KEY not set. Get one at https://openrouter.ai/keys "
        "and export it (or put it in a .env file you source)."
    )


def _persist_artifact(prompt: str, raw: str, model: str, response: dict) -> Path:
    """Drop a markdown trace so reporter.py / human readers can see what happened."""
    out_dir = ARTIFACTS / "openrouter"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")[:-4] + "Z"
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower())[:60].strip("-") or "untitled"
    path = out_dir / f"{model.replace('/', '-')}-{slug}-{stamp}.md"
    body = [
        "# openrouter advisor artifact",
        "",
        "- Provider: openrouter",
        f"- Model: {model}",
        f"- Created at: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Original task",
        "",
        prompt,
        "",
        "## Raw output",
        "",
        "```text",
        raw,
        "```",
        "",
        "## Usage",
        "",
        "```json",
        json.dumps(response.get("usage", {}), indent=2),
        "```",
        "",
    ]
    path.write_text("\n".join(body), encoding="utf-8")
    return path


DEFAULT_MAX_TOKENS = 4096
"""OpenRouter rejects paid requests it can't afford at the model's default
ceiling (often 65K+). 4K is plenty for the prose answers we ask for and
keeps requests inside small credit balances."""


def ask(
    model: str,
    prompt: str,
    *,
    api_key: Optional[str] = None,
    timeout: int = 600,
    max_tokens: Optional[int] = DEFAULT_MAX_TOKENS,
    temperature: Optional[float] = None,
) -> dict:
    """One-shot chat call. Returns the standard envelope."""
    key = _load_api_key(api_key)
    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if temperature is not None:
        payload["temperature"] = temperature

    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # OpenRouter recommends these to identify the integration.
            "HTTP-Referer": "https://github.com/marcmunoz-uno/relay-drones",
            "X-Title": "relay-drones",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:500]
        except Exception:
            pass
        raise OpenRouterError(f"HTTP {e.code} from openrouter: {detail}") from e
    except urllib.error.URLError as e:
        raise OpenRouterError(f"openrouter unreachable: {e}") from e

    choices = body.get("choices") or []
    if not choices:
        raise OpenRouterError(
            f"openrouter returned no choices; body={json.dumps(body)[:500]}"
        )
    raw = (choices[0].get("message") or {}).get("content") or ""
    raw = raw.strip()
    if not raw:
        raise OpenRouterError(
            f"empty content from {model}; finish_reason={choices[0].get('finish_reason')}"
        )

    artifact = _persist_artifact(prompt, raw, model, body)
    return {
        "raw": raw,
        "artifact_path": str(artifact),
        "provider": f"openrouter:{model}",
        "exit_code": 0,
    }


def ask_with_fallback(
    models: list[str],
    prompt: str,
    **kwargs,
) -> dict:
    """Try each model in order; return the first that succeeds.

    On any OpenRouterError (rate limit, 5xx, empty response), step to the
    next model. Raises the last error if every option fails — caller is
    expected to mark the task failed and move on.
    """
    if not models:
        raise OpenRouterError("ask_with_fallback called with empty models list")
    last_err: Optional[Exception] = None
    for m in models:
        try:
            return ask(m, prompt, **kwargs)
        except OpenRouterError as e:
            last_err = e
            continue
    raise OpenRouterError(
        f"all {len(models)} openrouter fallbacks failed; last error: {last_err}"
    )
