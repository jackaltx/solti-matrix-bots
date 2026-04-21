# solti-matrix-bots Collection

## What This Is

Ansible collection for standardized Matrix bot deployment and lifecycle management. Self-contained with execution wrapper (`manage-bot.sh`) and library (roles).

## Quick Context

**Pattern**: Mirrors `solti-containers` quadlet management pattern applied to Matrix bots with systemd user services.

**Bots Managed**:
- `matrix-watcher` - Event validation bot (matrix-nio)
- `claude-code-bot` - AI analysis assistant (matrix-nio + anthropic SDK)

**Key Files**:
- [`manage-bot.sh`](manage-bot.sh) - Dynamic playbook generator
- [`roles/_bot_base/`](roles/_bot_base/) - Shared bot infrastructure
- [`roles/matrix_watcher/`](roles/matrix_watcher/) - matrix-watcher bot
- [`roles/claude_code_bot/`](roles/claude_code_bot/) - claude-code-bot
- [`inventory/localhost.yml`](inventory/localhost.yml) - Generic localhost inventory
- [`inventory/group_vars/all.yml.example`](inventory/group_vars/all.yml.example) - Configuration template

## Architecture

### State-Driven Lifecycle

```
prepare → present → verify → absent
   ↓         ↓        ↓         ↓
 setup    deploy   health   remove
```

**Actions**:
- `prepare`: One-time venv setup, directory creation
- `deploy`: Service deployment (idempotent)
- `verify`: Health check (status, logs)
- `remove`: Service removal (preserves data by default)

### bot_properties Pattern

Each bot defines configuration via `bot_properties` dict in `roles/{bot}/defaults/main.yml`:

```yaml
bot_properties:
  root: "matrix-watcher"               # Bot identifier
  name: "matrix-watcher.service"       # Systemd service name
  script_name: "matrix-bot-nio.py"     # Python script
  venv_dir: "~/matrix-bots/venv"       # Shared venv per host
  working_dir: "{{ matrix_working_dir }}"  # Externalized
  requirements: ["matrix-nio>=0.25.2"]  # Python deps
  environment:                          # Env vars (from env/group_vars)
    MATRIX_HOMESERVER_URL: "{{ matrix_homeserver_url }}"
    MATRIX_ROOM_ID: "{{ matrix_watcher_room }}"
  secrets: ["MATRIX_WATCHER_TOKEN"]    # From ~/.secrets/LabMatrix
```

### Configuration Externalization

**Priority Order**:
1. Environment variables (highest)
2. `inventory/group_vars/all.yml` (gitignored)
3. Role defaults (fallback)

**Externalized Values**:
- `domain` - Matrix domain
- `matrix_homeserver_url` - Homeserver URL
- `matrix_working_dir` - Working directory for bot execution
- `matrix_watcher_room` - Room assignment for matrix-watcher
- `claude_code_bot_room` - Room assignment for claude-code-bot

### Security Model

**Public (in repo)**:
- Collection structure, roles, manage-bot.sh
- Generic inventory (`localhost.yml`)
- Bot existence disclosure (acceptable)

**Private (gitignored)**:
- `inventory/group_vars/all.yml` - Domain-specific config
- `~/.secrets/LabMatrix` - Tokens, API keys

Domain abstraction follows `solti-containers` pattern.

## Usage

### Initial Setup

```bash
# Create domain config from template
mkdir -p inventory/group_vars
cp inventory/group_vars/all.yml.example inventory/group_vars/all.yml
nano inventory/group_vars/all.yml

# Create secrets file
nano ~/.secrets/LabMatrix
# Add: export MATRIX_WATCHER_TOKEN="..."
#      export MATRIX_SOLTI_CLAUDE_CODE_TOKEN="..."
#      export ANTHROPIC_API_KEY="..."
```

### Deployment

```bash
# Local deployment
./manage-bot.sh matrix-watcher prepare
./manage-bot.sh matrix-watcher deploy
./manage-bot.sh matrix-watcher verify

# Remote deployment
./manage-bot.sh -h monitor11 claude-code-bot deploy

# Environment override
MATRIX_HOMESERVER_URL="https://alt.example.com" \
  ./manage-bot.sh matrix-watcher deploy

# Remove with data deletion
DELETE_DATA=true ./manage-bot.sh matrix-watcher remove
```

### Monitoring

```bash
# Service status
systemctl --user status matrix-watcher.service

# Logs
journalctl --user -u matrix-watcher -f

# All bot services
systemctl --user list-units 'matrix-*' --all
```

## Critical Files

### Execution

- [`manage-bot.sh`](manage-bot.sh) - Dynamic playbook generator (main entry point)

### Roles

- [`roles/_bot_base/`](roles/_bot_base/) - Shared infrastructure for all bots
  - [`tasks/prepare.yml`](roles/_bot_base/tasks/prepare.yml) - Venv setup
  - [`tasks/present.yml`](roles/_bot_base/tasks/present.yml) - Service deployment
  - [`tasks/cleanup.yml`](roles/_bot_base/tasks/cleanup.yml) - Service removal
  - [`tasks/verify.yml`](roles/_bot_base/tasks/verify.yml) - Health check
  - [`templates/bot.service.j2`](roles/_bot_base/templates/bot.service.j2) - Systemd template

- [`roles/matrix_watcher/`](roles/matrix_watcher/) - Event validation bot
  - [`defaults/main.yml`](roles/matrix_watcher/defaults/main.yml) - bot_properties config
  - [`files/matrix-bot-nio.py`](roles/matrix_watcher/files/matrix-bot-nio.py) - Bot script

- [`roles/claude_code_bot/`](roles/claude_code_bot/) - AI analysis bot
  - [`defaults/main.yml`](roles/claude_code_bot/defaults/main.yml) - bot_properties config
  - [`files/claude-code-bot.py`](roles/claude_code_bot/files/claude-code-bot.py) - Bot script

### Inventory

- [`inventory/localhost.yml`](inventory/localhost.yml) - Generic localhost (public)
- [`inventory/group_vars/all.yml.example`](inventory/group_vars/all.yml.example) - Config template
- `inventory/group_vars/all.yml` - User-created (gitignored)

## Design Decisions

### Shared Venv per Host

One venv for all bots at `~/matrix-bots/venv/`. Reduces disk space, simplifies dependency management, faster deployment.

### Bot Scripts in Role Files

Bot scripts deployed via Ansible `copy` module from `roles/{bot}/files/`. Makes roles portable, versioned in git, declarative.

### Systemd User Services

All bots run as user services (`scope: user`). No sudo required, user-level isolation, easier debugging.

### State-Driven Lifecycle

Three-state pattern (prepare/present/absent) from `solti-containers`. Idempotent operations, clear lifecycle stages.

## Adding New Bots

1. Create role: `roles/my_new_bot/`
2. Define `bot_properties` in `defaults/main.yml`
3. Add bot script to `files/`
4. Create `tasks/main.yml` with state-driven logic
5. Update `manage-bot.sh`: Add to `BOT_MAP` and `SUPPORTED_BOTS`
6. Update `inventory/localhost.yml`: Add `my_new_bot_svc` group

See existing roles as examples.

## Claude's Role

Assist with bot role development, manage-bot.sh updates, documentation, and troubleshooting. For collection-specific work, this CLAUDE.md provides sufficient context.

**Status**: ⚠️ Development - Awaiting user review before initial git commit
