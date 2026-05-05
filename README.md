# solti-matrix-bots

Standardized Matrix bot deployment and lifecycle management using Ansible.

**Status:** ⚠️ Development - Collection exists but has never been deployed

---

## Deployment Status (2026-05-04)

The collection has not yet been used to deploy any bots. The currently running bots were deployed **manually** from `mylab/`, predating this collection.

### Currently Running (on local host)

| Service | Script | Venv | Secrets |
|---------|--------|------|---------|
| `matrix-bot.service` | `mylab/bin/matrix-bot-nio.py` | `mylab/solti-venv/` | `~/.secrets/matrix-bot.env` |

`claude-code-bot.service` was stopped. Both services were deployed by hand using `mylab/config/systemd/` unit files, not via `manage-bot.sh`.

### What This Collection Expects (not yet created)

| Resource | Path |
|----------|------|
| Bot data/venv | `~/matrix-bots/` |
| Service names | `matrix-watcher.service`, `claude-code-bot.service` |
| Secrets file | `~/.secrets/LabMatrix` |

### Script Sync Status

Bot scripts in `roles/*/files/` are **identical** (same MD5) to the live scripts in `mylab/bin/` — the collection was built by copying those working scripts. Both are in sync as of this date.

### Next Step

To migrate from manual deployment to this collection:

1. Run `./manage-bot.sh matrix-watcher prepare` to create `~/matrix-bots/`
2. Create `~/.secrets/LabMatrix` with tokens from `~/.secrets/matrix-bot.env`
3. Create `inventory/group_vars/all.yml` from the template
4. Run `./manage-bot.sh matrix-watcher deploy`
5. Stop/disable the old `matrix-bot.service` after verifying the new one is healthy

---

## Overview

This collection provides unified management for Matrix bots using the proven `solti-containers` pattern:

- **State-Driven Lifecycle**: prepare → deploy → verify → remove
- **Dynamic Playbook Generation**: `manage-bot.sh` wrapper for consistent UX
- **Shared Infrastructure**: `_bot_base` role for common bot operations
- **Configuration Externalization**: Domain-specific config via gitignored `group_vars` or environment variables
- **Systemd User Services**: User-scoped services with automatic restart
- **Shared Virtual Environments**: One venv per host for efficient resource usage

---

## Quick Start

### 1. Initial Setup

```bash
# Clone collection
cd ~/sandbox/ansible/jackaltx
git clone https://github.com/jackaltx/solti-matrix-bots.git
cd solti-matrix-bots

# Create domain configuration from template
mkdir -p inventory/group_vars
cp inventory/group_vars/all.yml.example inventory/group_vars/all.yml

# Edit with your domain and settings
nano inventory/group_vars/all.yml
```

**Example `inventory/group_vars/all.yml`:**
```yaml
---
domain: "example.com"
matrix_homeserver_url: "https://matrix.{{ domain }}"
matrix_working_dir_default: "{{ ansible_facts['env']['HOME'] }}/your/working/directory"
matrix_watcher_room_default: "#solti-verify:{{ domain }}"
claude_code_bot_room_default: "#solti-dev:{{ domain }}"
matrix_allowed_users: "@user1:{{ domain }},@user2:{{ domain }}"
```

### 2. Create Secrets File

Matrix bot tokens and API keys are loaded from `~/.secrets/LabMatrix`:

```bash
# Create or edit secrets file
nano ~/.secrets/LabMatrix

# Add bot tokens (example)
export MATRIX_WATCHER_TOKEN="syt_YOUR_TOKEN_HERE"
export MATRIX_SOLTI_CLAUDE_CODE_TOKEN="syt_YOUR_TOKEN_HERE"
export ANTHROPIC_API_KEY="sk-ant-api03-YOUR_KEY_HERE"

# Secure permissions
chmod 600 ~/.secrets/LabMatrix

# Source for current session
source ~/.secrets/LabMatrix
```

### 3. Deploy a Bot

```bash
# Prepare environment (one-time setup)
./manage-bot.sh matrix-watcher prepare

# Deploy bot
./manage-bot.sh matrix-watcher deploy

# Verify bot is running
./manage-bot.sh matrix-watcher verify
```

---

## Available Bots

### matrix-watcher

Event validation bot for Matrix rooms.

- **Script**: `matrix-bot-nio.py`
- **Dependencies**: `matrix-nio>=0.25.2`
- **Secrets Required**: `MATRIX_WATCHER_TOKEN`
- **Room Config**: `matrix_watcher_room_default` in group_vars

### claude-code-bot

AI-powered analysis assistant using Anthropic Claude SDK.

- **Script**: `claude-code-bot.py`
- **Dependencies**: `matrix-nio>=0.25.2`, `anthropic>=0.96.0`
- **Secrets Required**: `MATRIX_SOLTI_CLAUDE_CODE_TOKEN`, `ANTHROPIC_API_KEY`
- **Room Config**: `claude_code_bot_room_default` in group_vars
- **Models**: Smart selection between Haiku 4.5 (simple tasks) and Sonnet 4.5 (complex analysis)

---

## Usage Examples

### Local Deployment

```bash
# Prepare + Deploy in one command (prepare is idempotent)
./manage-bot.sh matrix-watcher prepare
./manage-bot.sh matrix-watcher deploy

# Deploy claude-code-bot
./manage-bot.sh claude-code-bot deploy

# Verify both bots
./manage-bot.sh matrix-watcher verify
./manage-bot.sh claude-code-bot verify

# Remove bot (preserves data by default)
./manage-bot.sh matrix-watcher remove

# Remove bot and delete data
DELETE_DATA=true ./manage-bot.sh matrix-watcher remove
```

### Remote Deployment

```bash
# Deploy to specific host
./manage-bot.sh -h monitor11 claude-code-bot deploy

# Use custom inventory
./manage-bot.sh -i inventory/remote.yml -h monitor11 claude-code-bot deploy

# Skip confirmation prompts (for automation)
./manage-bot.sh -y -h monitor11 claude-code-bot deploy
```

### Environment Variable Overrides

Configuration can be overridden via environment variables (takes priority over group_vars):

```bash
# Override homeserver URL
MATRIX_HOMESERVER_URL="https://matrix-alt.example.com" \
  ./manage-bot.sh matrix-watcher deploy

# Override room assignment
MATRIX_WATCHER_ROOM="#different-room:example.com" \
  ./manage-bot.sh matrix-watcher deploy

# Override working directory
MATRIX_WORKING_DIR="/custom/path" \
  ./manage-bot.sh claude-code-bot deploy
```

---

## Architecture

### Directory Structure

```
solti-matrix-bots/
├── manage-bot.sh              # Dynamic playbook generator
├── galaxy.yml                 # Collection metadata
├── README.md                  # This file
├── .gitignore                 # Excludes inventory/group_vars/
├── inventory/
│   ├── localhost.yml          # Generic localhost (public-safe)
│   └── group_vars/
│       └── all.yml.example    # Configuration template
├── roles/
│   ├── _bot_base/             # Shared bot infrastructure
│   │   ├── defaults/main.yml
│   │   ├── tasks/
│   │   │   ├── prepare.yml    # Venv setup, directories
│   │   │   ├── present.yml    # Service deployment
│   │   │   ├── cleanup.yml    # Service removal
│   │   │   └── verify.yml     # Health check
│   │   └── templates/
│   │       └── bot.service.j2 # Systemd user service
│   ├── matrix_watcher/
│   │   ├── defaults/main.yml  # Bot-specific config
│   │   ├── tasks/main.yml     # State-driven lifecycle
│   │   └── files/
│   │       └── matrix-bot-nio.py
│   └── claude_code_bot/
│       ├── defaults/main.yml
│       ├── tasks/main.yml
│       └── files/
│           └── claude-code-bot.py
└── docs/
    └── Bot-Management.md      # Detailed usage guide
```

### bot_properties Pattern

Each bot defines a `bot_properties` dictionary in its role defaults:

```yaml
bot_properties:
  root: "matrix-watcher"
  name: "matrix-watcher.service"
  script_name: "matrix-bot-nio.py"

  # Directories
  data_dir: "{{ real_user_dir }}/matrix-bots"
  bot_dir: "{{ real_user_dir }}/matrix-bots/matrix-watcher"
  venv_dir: "{{ real_user_dir }}/matrix-bots/venv"  # Shared

  # Externalized configuration
  working_dir: "{{ lookup('env', 'MATRIX_WORKING_DIR') | default(matrix_working_dir) }}"

  # Dependencies
  requirements:
    - "matrix-nio>=0.25.2"

  # Environment variables (from env or group_vars)
  environment:
    MATRIX_HOMESERVER_URL: "{{ matrix_homeserver_url }}"
    MATRIX_ROOM_ID: "{{ matrix_watcher_room }}"
    MATRIX_BOT_USER_ID: "@matrix-watcher:{{ domain }}"

  # Secrets (from ~/.secrets/LabMatrix)
  secrets:
    - MATRIX_WATCHER_TOKEN
```

### Configuration Priority

1. **Environment Variables** (highest priority)
2. **Group Vars** (`inventory/group_vars/all.yml`)
3. **Role Defaults** (fallback values)

---

## Security Model

### Public (Safe in Repository)

- ✅ Collection structure and roles
- ✅ Generic inventory (`localhost.yml`)
- ✅ Bot service groups (`matrix_watcher_svc`, `claude_code_bot_svc`)
- ✅ Bot script existence (type disclosure acceptable)

### Private (Gitignored)

- ❌ `inventory/group_vars/all.yml` - Domain-specific configuration
- ❌ `~/.secrets/LabMatrix` - Tokens, API keys

### Domain Abstraction

Domain-specific configuration is externalized to `inventory/group_vars/all.yml` (gitignored), following the `solti-containers` pattern. Users create this file on first clone from the provided template.

---

## Bot Management

### Service Locations

- **Systemd Services**: `~/.config/systemd/user/{bot}.service`
- **Bot Data**: `~/matrix-bots/{bot}/`
- **Shared Venv**: `~/matrix-bots/venv/`
- **Logs**: `journalctl --user -u {bot}.service`

### Lifecycle States

- **prepare**: One-time setup (venv, directories)
- **present**: Deploy bot (idempotent, safe to re-run)
- **absent**: Remove bot (preserves data unless `DELETE_DATA=true`)
- **verify**: Health check (service status, recent logs)

### Monitoring

```bash
# View service status
systemctl --user status matrix-watcher.service

# View recent logs
journalctl --user -u matrix-watcher -f

# Check all bot services
systemctl --user list-units 'matrix-*' --all
```

---

## Adding New Bots

To add a new Matrix bot to the collection:

1. **Create bot role**: `roles/my_new_bot/`
2. **Define bot_properties**: In `roles/my_new_bot/defaults/main.yml`
3. **Add bot script**: Place in `roles/my_new_bot/files/`
4. **Update manage-bot.sh**: Add to `BOT_MAP` and `SUPPORTED_BOTS`
5. **Update inventory**: Add `my_new_bot_svc` group

See existing roles (`matrix_watcher`, `claude_code_bot`) as examples.

---

## Troubleshooting

### Bot fails to start

```bash
# Check service status
systemctl --user status matrix-watcher.service

# View full logs
journalctl --user -u matrix-watcher --since "1 hour ago"

# Verify secrets loaded
source ~/.secrets/LabMatrix
echo ${MATRIX_WATCHER_TOKEN:0:20}...  # Should show token prefix
```

### Missing configuration

```
Error: Missing required configuration
```

**Fix:** Create `inventory/group_vars/all.yml` from template:
```bash
cp inventory/group_vars/all.yml.example inventory/group_vars/all.yml
nano inventory/group_vars/all.yml
```

### Deployment fails on remote host

Ensure:
- Remote host has Python 3.8+ installed
- Ansible can SSH to remote host
- User has systemd user session enabled
- `~/.secrets/LabMatrix` exists on remote host

---

## Related Projects

- **[solti-containers](https://github.com/jackaltx/solti-containers)** - Podman quadlet management (pattern inspiration)
- **[solti-matrix-mgr](https://github.com/jackaltx/solti-matrix-mgr)** - Matrix Synapse admin utilities
- **[solti-conductor](https://github.com/jackaltx/solti-conductor)** - Orchestrator template

---

## License

GPL-3.0-or-later

## Author

JackalTX

## Repository

https://github.com/jackaltx/solti-matrix-bots
