"""The git guard is the only ENFORCED half of the multi-agent commit rule -- test it.

The guard (`.claude/hooks/git-guard.py`) runs as a PreToolUse hook and is loaded by path, not
imported as a package module, so this test loads it the same way. Two properties matter equally:
it must deny the sweeping shapes (or the rule is back to being advisory), and it must never deny
anything else (a guard that blocks `git status` gets switched off within the hour).
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "git-guard.py"

_spec = importlib.util.spec_from_file_location("git_guard", GUARD)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


DENIED = [
    'git add -A && git commit -m "x"',
    "git add --all",
    "git add -u",
    "git add .",
    'git commit -q -m "message"',
    'git commit -am "message"',
    "git commit --amend --no-edit",
    'git commit --all -m "x"',
    "git stash",
    "git stash push",
    "git reset --hard origin/master",
    "git checkout -- .",
    "git restore .",
    "git clean -fd",
    "git clean --force",
    "cd /tmp && git add -A",  # a git call that is not the first command in the line
]

ALLOWED = [
    'git add a.py b.py && git commit -m "msg" -- a.py b.py',
    'git add src/web/CLAUDE.md && git commit -m "m" --only src/web/CLAUDE.md',
    "git add src/web/CLAUDE.md",
    "git status --porcelain",
    "git log --oneline -5 && git show --stat HEAD",
    "git diff --cached --name-status",
    "git push -q origin master",
    "git pull --rebase",
    "git checkout HEAD -- src/web/app.py",
    "git restore --staged src/web/app.py",
    "git stash push -- src/web/app.py",
    "git stash list",
    "git clean -nd",
    "git rm -q .github/workflows/x.yml",
    'grep -rn "git add -A" docs/',  # talking about it is not doing it
    'echo "git commit -m x"',
]


@pytest.mark.parametrize("command", DENIED)
def test_sweeping_commands_are_denied(command):
    assert guard.check(command) is not None, command


@pytest.mark.parametrize("command", ALLOWED)
def test_safe_commands_pass(command):
    assert guard.check(command) is None, command


def test_override_token_releases_everything():
    for command in DENIED:
        assert guard.check(command + "  #multi-agent-ok") is None, command


def test_deny_payload_is_the_shape_claude_code_expects():
    proc = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    # the reason must carry the fix, not just the complaint
    assert "-- <the same paths>" in out["permissionDecisionReason"]


@pytest.mark.parametrize("payload", ["", "{}", '{"tool_input": null}', "not json", "[]"])
def test_fails_open_on_malformed_input(payload):
    """A guard that wedges the session is worse than the bug it prevents."""
    proc = subprocess.run(
        [sys.executable, str(GUARD)], input=payload, capture_output=True, text=True
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
