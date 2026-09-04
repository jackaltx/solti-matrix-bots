# solti-matrix-bots Collection

## What This Is

> **Surfacing to unified docs:** Update `solti-docs.yml` at the collection root
> to declare which files and CLAUDE.md sections should appear on solti.jackaltx.com.
> Local `docs/` detail stays local — only declare what matters to the suite-wide audience.
> See [solti-docs/HARVEST.md](https://github.com/jackaltx/solti-docs/blob/main/HARVEST.md).

**This collection is about bot lifecycle management, not bot development.**

The Ansible roles here deploy, configure, and manage systemd user services for Matrix bots.
The bot scripts themselves (`*.py` in `roles/*/files/`) are **reference implementations** —
testing vehicles that prove the lifecycle patterns work. They are not production-quality bots
and should not be the focus of development work done in this collection.

What belongs here:

- `_bot_base` shared infrastructure (venv, systemd, secrets injection)
- Per-bot Ansible roles (state machine, `bot_properties` schema)
- `manage-bot.sh` playbook generator
- Inventory and group_vars patterns

What does NOT belong here:

- Bot feature development (that lives with the bot's own codebase)
- Matrix protocol logic
- AI integration code

A self-contained deployment tool — ships with its own `ansible.cfg`, inventory, and
`manage-bot.sh` dynamic playbook generator. Clone, configure `inventory/group_vars/all.yml`
and `~/.secrets/LabMatrix`, then run `manage-bot.sh` directly. No Galaxy installation,
no external orchestrator.

Pattern mirrors `solti-containers`: same state-driven lifecycle, same named-host inventory,
same ansible.cfg structure — applied to Matrix bots with systemd user services.

## Bots

| # | Bot | Role | Purpose |
|---|-----|------|---------|
| 1 | `matrix-watcher` | `matrix_watcher` | Event validation bot (matrix-nio) |
| 2 | `claude-code-bot` | `claude_code_bot` | AI analysis assistant (matrix-nio + anthropic SDK) |
| 3 | `brain2-bot` | `brain2_bot` | Second-brain classifier → MongoDB (matrix-nio + anthropic + pymongo) |
| 4 | `card-capture-bot` | `card_capture_bot` | Business card scanner — Claude Sonnet vision extracts contact data, S3 storage, MongoDB inbox review flow before committing to `people` collection |
| 5 | `salty-bot` | `salty_bot` | Voice-friendly capture bot — trigger word `salty`, captures text/images/video, Claude vision cleanup, stores to MongoDB + S3/MinIO |

Bots 1 and 2 were developed together in the initial collection commit. Bot order reflects development sequence, not priority.

### salty-bot

Designed for voice-to-text input — no slash commands, no mode-switching. Say `salty <anything>` to open
a session, send text/images/video across multiple messages, then `salty done` to save. Sessions autosave
after inactivity. Claude cleans up voice-to-text transcription artifacts and generates a title + tags.

Shares `#SecondBrain` room with brain2-bot.

Additional secrets required: `MATRIX_SALTY_TOKEN`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`.

Storage: `second_brain.ideas` — `text_parts[]`, `attachments[]` (S3 keys), `title`, `tags[]`.

See [`docs/salty-multi-media-bot.md`](docs/salty-multi-media-bot.md) for full design and follow-on ideas.

### card-capture-bot

Drop a business card photo in `#CardCapture` — no @mention needed. Claude Sonnet vision extracts
contact fields, stores raw image to S3, puts extracted data into a `card_inbox` MongoDB collection.
Review commands (`/commit`, `/reextract`, `/discard`, `/pending`) control promotion to the `people`
collection.

Room: `#CardCapture` — provisioned by `mylab/playbooks/matrix/card-capture-matrix-config.yml`.

Additional secrets required: `MATRIX_CARD_CAPTURE_TOKEN`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`.

See [`docs/card-capture.md`](docs/card-capture.md) for full design.

## Key Files

- [`manage-bot.sh`](manage-bot.sh) — Dynamic playbook generator (entry point)
- [`ansible.cfg`](ansible.cfg) — Self-contained Ansible config
- [`roles/_bot_base/`](roles/_bot_base/) — Shared bot infrastructure
- [`roles/matrix_watcher/`](roles/matrix_watcher/) — matrix-watcher bot
- [`roles/claude_code_bot/`](roles/claude_code_bot/) — claude-code-bot
- [`roles/brain2_bot/`](roles/brain2_bot/) — second-brain classifier
- [`roles/salty_bot/`](roles/salty_bot/) — voice capture + S3 + Claude vision
- [`roles/card_capture_bot/`](roles/card_capture_bot/) — business card scanner
- [`docs/salty-multi-media-bot.md`](docs/salty-multi-media-bot.md) — salty design and future directions
- [`docs/card-capture.md`](docs/card-capture.md) — card capture design
- [`docs/Bot-Management.md`](docs/Bot-Management.md) — operational procedures
- [`inventory/localhost.yml`](inventory/localhost.yml) — Named host registry
- [`inventory/group_vars/all.yml.example`](inventory/group_vars/all.yml.example) — Config template

## Architecture

### State-Driven Lifecycle

```text
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
- `salty_bot_room` / `salty_bot_room_default` — Room for salty-bot (`#SecondBrain:domain`)
- `card_capture_bot_room` / `card_capture_bot_room_default` — Room for card-capture-bot (`#CardCapture:domain`)
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

# Remote host (requires host prep first — see mylab/playbooks/prep-bot-host.yml)
./manage-bot.sh -h bot-test -i inventory/bot-test.yml brain2-bot deploy

# Deploy all bots to a remote host
for bot in matrix-watcher claude-code-bot brain2-bot card-capture-bot salty-bot; do
  ./manage-bot.sh -y -h bot-test -i inventory/bot-test.yml $bot prepare
  ./manage-bot.sh -y -h bot-test -i inventory/bot-test.yml $bot deploy
done
```

## Known Issues

- **`verify` action**: `matrix_watcher` and `claude_code_bot` roles do not accept `verify`
  as a valid state (validation assert fails). Check service health directly via
  `systemctl --user status <bot>.service` instead.

- **Remote `WorkingDirectory`**: `matrix_working_dir_default` in `group_vars/all.yml` is
  typically a localhost path. For remote hosts, override it in the host inventory file:

  ```yaml
  matrix_working_dir_default: "/home/jackaltx/matrix-bots"
  ```

- **`| default()` without `true`**: Fixed in `matrix_watcher` and `claude_code_bot`
  defaults. All `lookup('env', ...)` calls now use `| default(fallback, true)` so empty
  env vars correctly fall back to `group_vars` values.

## Adding New Bots

1. Create `roles/my_bot/defaults/main.yml` — define `bot_properties`
2. Create `roles/my_bot/tasks/main.yml` — state-driven delegation to `_bot_base`
3. Add script to `roles/my_bot/files/`
4. Add to `BOT_MAP` and `SUPPORTED_BOTS` in `manage-bot.sh`
5. Add `my_bot_svc` group to `inventory/localhost.yml`
6. Add room default to `inventory/group_vars/all.yml.example`

See `roles/salty_bot/` or `roles/card_capture_bot/` as reference implementations for bots with S3/MinIO dependencies.

## Claude's Role

Assist with bot role development, manage-bot.sh updates, documentation, and troubleshooting.
This CLAUDE.md provides sufficient context for collection-specific work.
