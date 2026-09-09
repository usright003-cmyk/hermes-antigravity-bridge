#!/usr/bin/env python3
"""Opt-in A-F validation against a running bridge using synthetic Hermes state."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path

BASE = os.environ.get("HAB_LIVE_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
TOKEN_FILE = os.environ.get("HAB_LIVE_TOKEN_FILE", "")
MODEL_HIGH = os.environ.get("HAB_LIVE_MODEL_HIGH", "gemini-3.8-flash-high")
MODEL_LOW = os.environ.get("HAB_LIVE_MODEL_LOW", "gemini-3.8-flash-low")


def load_token() -> str:
    if not TOKEN_FILE:
        raise SystemExit("HAB_LIVE_TOKEN_FILE is required")
    token = Path(TOKEN_FILE).expanduser().read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit("live token file is empty")
    return token


def complete(token: str, messages: list[dict], *, model: str = MODEL_LOW, tools=None) -> str:
    body = {"model": model, "messages": messages}
    if tools is not None:
        body["tools"] = tools
    request = urllib.request.Request(
        BASE + "/v1/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload["choices"][0]["message"]["content"]


def hermes_turn(home: Path, prompt: str, model: str, session: str | None = None) -> tuple[str, str]:
    command = [
        "hermes", "chat", "--oneshot", "-Q", "--pass-session-id",
        "--source", "validation", "--max-turns", "1", "--provider", "custom", "-m", model,
    ]
    if session:
        command.extend(["--resume", session])
    command.extend(["-q", prompt])
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=360, check=False)
    if completed.returncode != 0:
        raise RuntimeError("Hermes live turn failed: " + completed.stdout + completed.stderr)
    combined = completed.stdout + "\n" + completed.stderr
    match = re.search(r"session_id:\s*(\S+)", combined)
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not match or not lines:
        raise RuntimeError("Hermes live turn did not return a session and answer")
    return match.group(1), lines[-1]


def main() -> None:
    token = load_token()
    results = []
    answer = complete(token, [
        {"role": "system", "content": "Follow the current request."},
        {"role": "user", "content": "Old topic"},
        {"role": "assistant", "content": "Old answer"},
        {"role": "user", "content": "Return exactly LIVE_A_OK and nothing else."},
    ])
    assert answer == "LIVE_A_OK"
    results.append({"test": "A", "result": answer})

    messages = [{"role": "system", "content": "Synthetic memory"}]
    for index in range(723):
        messages.extend([
            {"role": "user", "content": f"old-{index} " + "x" * 1500},
            {"role": "assistant", "content": f"answer-{index} " + "y" * 1500},
        ])
    messages.append({"role": "user", "content": "Return exactly LIVE_B_OK and nothing else."})
    answer = complete(token, messages)
    assert answer == "LIVE_B_OK"
    results.append({"test": "B", "result": answer})

    answer = complete(token, [
        {"role": "system", "content": "HEAD\n" + "s" * 100000 + "\nTAIL"},
        {"role": "user", "content": "Return exactly LIVE_C_OK and nothing else."},
    ])
    assert answer == "LIVE_C_OK"
    results.append({"test": "C", "result": answer})

    with tempfile.TemporaryDirectory(prefix="hab-live-hermes-") as home_text:
        home = Path(home_text)
        (home / "memories").mkdir(parents=True)
        (home / "memories/MEMORY.md").write_text("LIVE_MEMORY_TOKEN=cedar-comet-482.\n", encoding="utf-8")
        (home / "memories/USER.md").write_text("Synthetic live-test profile.\n", encoding="utf-8")
        (home / "config.yaml").write_text(
            "model:\n"
            f"  default: {MODEL_HIGH}\n"
            "  provider: custom\n"
            f"  base_url: {BASE}/v1\n"
            f"  api_key: {token}\n"
            "agent:\n  max_turns: 2\n  reasoning_effort: low\n"
            "compression:\n  enabled: false\n"
            "memory:\n  memory_enabled: true\n  user_profile_enabled: true\n  nudge_interval: 9999\n"
            "streaming:\n  enabled: false\nplatform_toolsets:\n  cli: []\n",
            encoding="utf-8",
        )
        (home / "config.yaml").chmod(0o600)
        session, answer = hermes_turn(home, "Remember LIVE_D_FACT=amber-falcon-401. Reply exactly LIVE_D_ACK.", MODEL_HIGH)
        assert answer == "LIVE_D_ACK"
        resumed, answer = hermes_turn(home, "What is LIVE_D_FACT? Reply with its value only.", MODEL_LOW, session)
        assert resumed == session and answer == "amber-falcon-401"
        results.append({"test": "D", "result": answer})
        _, answer = hermes_turn(home, "What is LIVE_MEMORY_TOKEN in Hermes memory? Reply with its value only.", MODEL_LOW, session)
        assert answer == "cedar-comet-482"
        results.append({"test": "E", "result": answer})
        _, answer = hermes_turn(home, "Remember LIVE_F_ALIAS=violet-moon-502. Reply exactly LIVE_F_ACK.", MODEL_LOW, session)
        assert answer == "LIVE_F_ACK"
        _, answer = hermes_turn(home, "Latest task: prefix LIVE_FINAL_ to LIVE_F_ALIAS and reply only with the result.", MODEL_LOW, session)
        assert answer == "LIVE_FINAL_violet-moon-502"
        results.append({"test": "F", "result": answer})
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
