@AGENTS.md

# Claude Code specifics

> The shared instructions for every agent are in **`AGENTS.md`** (imported above). Put anything
> that applies to all agents THERE, not here. This file holds only what is Claude Code–specific.

- **Profiles** come from `.claude/settings.local.json` (`env`: `AZURE_CONFIG_DIR`,
  `GH_CONFIG_DIR` → the personal ones). The file is gitignored and is not hot-reloaded — a change
  needs a new session.
- **Hooks** (`.claude/settings.json`, PreToolUse): `.claude/hooks/git-guard.py` (escape hatch
  `#multi-agent-ok`). Tests: `.venv/bin/pytest tests/hooks`.
- **Permissions** (`.claude/settings.json`): Azure/GitHub reads allowed; direct Azure writes,
  `gh secret set`, `gh workflow run` denied; `git commit` / `git push` ask. Running a provisioning
  `pwsh .azure/…ps1` is NOT covered by the az denies — run one only after the user's OK.
