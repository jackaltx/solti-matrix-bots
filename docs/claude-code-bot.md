# claude-code-bot — Design and Operations

Last updated: 2026-07-02

## Purpose

AI assistant bot for the `#solti-dev` Matrix room. Responds to `@solti-claude-code` mentions
with code analysis, file reads, and safe bash execution inside a configurable working directory.
Designed for infrastructure and Ansible work — the working directory is typically the mylab
orchestrator root, giving the bot read access to playbooks, roles, and configs.

Read-only by design. Bash execution is sandboxed to non-destructive commands.

---

## Bot Identity

| Field | Value |
|-------|-------|
| Matrix user | `@solti-claude-code:jackaltx.com` |
| Room | `#solti-dev:jackaltx.com` |
| Secrets | `MATRIX_SOLTI_CLAUDE_CODE_TOKEN`, `ANTHROPIC_API_KEY` |
| Dependencies | `matrix-nio`, `anthropic` |

---

## Usage

Mention the bot in `#solti-dev`:

```
@solti-claude-code what roles does the monitoring collection have?
@solti-claude-code show me the salty-bot defaults
@solti-claude-code what changed in the last 5 commits to mylab?
```

The bot reads context from the `matrix_working_dir` (set in `group_vars/all.yml`) and uses
Claude API to answer. Smart model selection: Haiku 4.5 for simple lookups, Sonnet for complex
analysis.

Only users in `matrix_allowed_users` can trigger the bot. Others' @mentions are silently ignored.

---

## Model Selection

| Task complexity | Model |
|-----------------|-------|
| Simple lookups, status checks | Claude Haiku 4.5 |
| Code analysis, multi-file review | Claude Sonnet |

---

## Deployment

```bash
source ~/.secrets/LabMatrix
./manage-bot.sh claude-code-bot prepare
./manage-bot.sh claude-code-bot deploy
```

### Working Directory

The bot executes within `matrix_working_dir` (from `group_vars/all.yml`). On localhost this
is typically `~/sandbox/ansible/jackaltx/mylab`. On a remote VM it must be overridden to a
path that exists on that host:

```yaml
# inventory/bot-test.yml
matrix_working_dir_default: "/home/jackaltx/matrix-bots"
```

Without this override, the service fails at startup with `CHDIR: No such file or directory`.

---

## Known Issues

**`verify` action fails**: Role validation rejects `verify` as a valid state. Use systemctl:

```bash
systemctl --user status claude-code-bot.service
journalctl --user -u claude-code-bot -f
```

**`| default()` without `true`**: Fixed 2026-07-02. Earlier versions of the role defaults
used `| default(fallback)` without the boolean flag, causing `claude_code_bot_room` and
`matrix_working_dir` to resolve as empty strings when the corresponding env vars were unset.
If deploying from an older checkout, ensure `roles/claude_code_bot/defaults/main.yml` uses
`| default(fallback, true)` on all `lookup('env', ...)` calls.

---

## Troubleshooting

**Bot receives @mention but doesn't reply**: Check the sender is in `matrix_allowed_users`.
Check `ANTHROPIC_API_KEY` is non-empty in the service file:

```bash
grep ANTHROPIC /home/jackaltx/.config/systemd/user/claude-code-bot.service
```

**Room ID empty in service file**: `claude_code_bot_room` resolved as empty. Verify
`claude_code_bot_room_default` is set in `group_vars/all.yml` and the role defaults
use `| default(fallback, true)`.
