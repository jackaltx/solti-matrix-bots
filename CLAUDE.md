# solti-matrix-bots Collection

## What This Is

A self-contained deployment tool for Matrix bots — **app, not library**. Ships with its own
`ansible.cfg`, inventory, and `manage-bot.sh` dynamic playbook generator. Clone, configure
`inventory/group_vars/all.yml` and `~/.secrets/LabMatrix`, then run `manage-bot.sh` directly.
No Galaxy installation, no external orchestrator.

Pattern mirrors `solti-containers`: same state-driven lifecycle, same named-host inventory,
same ansible.cfg structure — applied to Matrix bots with systemd user services.

## Bots

- `matrix-watcher` — Event validation bot (matrix-nio)
- `claude-code-bot` — AI analysis assistant (matrix-nio + anthropic SDK)
- `brain2-bot` — Second brain classifier + MongoDB capture (matrix-nio + anthropic + pymongo)

## Key Files

- [`manage-bot.sh`](manage-bot.sh) — Dynamic playbook generator (entry point)
- [`ansible.cfg`](ansible.cfg) — Self-contained Ansible config
- [`roles/_bot_base/`](roles/_bot_base/) — Shared bot infrastructure
- [`roles/matrix_watcher/`](roles/matrix_watcher/) — matrix-watcher bot
- [`roles/claude_code_bot/`](roles/claude_code_bot/) — claude-code-bot
- [`roles/brain2_bot/`](roles/brain2_bot/) — brain2-bot (second brain)
- [`inventory/localhost.yml`](inventory/localhost.yml) — Named host registry
- [`inventory/group_vars/all.yml.example`](inventory/group_vars/all.yml.example) — Config template

## Architecture

### State-Driven Lifecycle

```
prepare → present → verify → absent
   ↓         ↓        ↓         ↓
 setup    deploy   health   remove
```

- `prepare`: One-time venv setup, directory creation
- `deploy`: Service deployment (idempotent)
- `verify`: Health check (status, logs)
- `remove`: Service removal (preserves data by default)

### bot_properties Pattern

Each bot defines `bot_properties` in `roles/{bot}/defaults/main.yml` — the contract with `_bot_base`:

```yaml
bot_properties:
  root: "brain2-bot"
  name: "brain2-bot.service"
  script_name: "brain2-bot.py"
  data_dir: "{{ real_user_dir }}/matrix-bots"
  bot_dir:  "{{ real_user_dir }}/matrix-bots/brain2-bot"
  venv_dir: "{{ real_user_dir }}/matrix-bots/venv"
  requirements: ["matrix-nio>=0.25.2", "anthropic>=0.96.0", "pymongo>=4.0"]
  environment:
    MATRIX_HOMESERVER_URL: "{{ matrix_homeserver_url }}"
    MATRIX_ALLOWED_USERS:  "{{ matrix_allowed_users | default('') }}"
  secrets:
    - MATRIX_SOLTI_BRAIN2_TOKEN
    - ANTHROPIC_API_KEY
    - BRAIN2_MONGODB_URI
```

### Configuration Externalization

Priority (highest → lowest):

1. Environment variables
2. `inventory/group_vars/all.yml` — **required, gitignored**
3. Role defaults

**`all.yml` required keys:**

- `domain` — Matrix domain
- `matrix_homeserver_url` — Homeserver URL
- `matrix_working_dir_default` — Working directory for bot execution
- `matrix_watcher_room_default` — Room for matrix-watcher
- `claude_code_bot_room_default` — Room for claude-code-bot
- `brain2_bot_room_default` — Room for brain2-bot (`#SecondBrain:domain`)
- `matrix_allowed_users` — Comma-separated Matrix user IDs permitted to use bots

### Security Model

**Public (in repo):** Collection source, roles, manage-bot.sh, generic inventory

**Gitignored:** `inventory/group_vars/all.yml` (domain config), `~/.secrets/LabMatrix` (tokens)

Secrets are resolved from shell environment at deploy time via `lookup('env', name)` and
baked into the systemd service file. Source `~/.secrets/LabMatrix` before `manage-bot.sh`.

## Usage

```bash
# Required before any deploy
source ~/.secrets/LabMatrix

./manage-bot.sh brain2-bot prepare
./manage-bot.sh brain2-bot deploy
./manage-bot.sh brain2-bot verify
./manage-bot.sh brain2-bot remove

# Remote host
./manage-bot.sh -h myserver brain2-bot deploy
```

## Adding New Bots

1. Create `roles/my_bot/defaults/main.yml` — define `bot_properties`
2. Create `roles/my_bot/tasks/main.yml` — state-driven delegation to `_bot_base`
3. Add script to `roles/my_bot/files/`
4. Add to `BOT_MAP` and `SUPPORTED_BOTS` in `manage-bot.sh`
5. Add `my_bot_svc` group to `inventory/localhost.yml`
6. Add room default to `inventory/group_vars/all.yml.example`

See `roles/brain2_bot/` as the reference implementation.

## Claude's Role

Assist with bot role development, manage-bot.sh updates, documentation, and troubleshooting.
This CLAUDE.md provides sufficient context for collection-specific work.
