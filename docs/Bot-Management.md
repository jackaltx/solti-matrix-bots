# Matrix Bot Management Guide

This guide provides detailed instructions for managing Matrix bots using the `solti-matrix-bots` collection.

---

## 1. Architecture Overview

This collection follows the **Solti Standard** for service management, adapted for Matrix bots running as systemd user services.

### Key Components

- **`manage-bot.sh`**: The unified entry point. It dynamically generates Ansible playbooks to perform lifecycle actions.
- **`_bot_base` Role**: Shared infrastructure. Handles directory creation, shared virtual environments, and systemd service templates.
- **Bot Roles**: Specific configurations for each bot (e.g., `matrix_watcher`, `claude_code_bot`).
- **Shared Venv**: A single Python virtual environment located at `~/matrix-bots/venv` is shared by all bots on a host to minimize resource usage.

---

## 2. Bot Lifecycle (States)

Each bot is managed through four primary states:

| Action | State | Description |
| :--- | :--- | :--- |
| **`prepare`** | `prepare` | One-time setup: creates directories and initializes the shared venv. |
| **`deploy`** | `present` | Idempotent deployment: installs requirements, copies scripts, and starts the service. |
| **`verify`** | `verify` | Health check: displays service status and recent journald logs. |
| **`remove`** | `absent` | Cleanup: stops/disables the service and removes the unit file. |

---

## 3. Configuration Model

### 3.1 Domain Abstraction
All domain-specific settings are stored in `inventory/group_vars/all.yml` (gitignored). This ensures the collection can be safely shared without leaking environment details.

### 3.2 Secret Management
Secrets (API keys, tokens) are **never** stored in Ansible variables.
- Bots load secrets via `EnvironmentFile=%h/.secrets/LabMatrix` in their systemd units.
- You must ensure this file exists on the target host with appropriate permissions (`600`).

### 3.3 The `bot_properties` Pattern
Each bot role defines its configuration in `defaults/main.yml` using a `bot_properties` dictionary. This dictionary is passed to `_bot_base` to drive the deployment.

---

## 4. Operational Procedures

### Deploying a New Bot
1. Ensure `all.yml` has the required room and domain settings.
2. Ensure `~/.secrets/LabMatrix` has the required tokens.
3. Run the preparation: `./manage-bot.sh <bot-name> prepare`
4. Deploy the bot: `./manage-bot.sh <bot-name> deploy`
5. Verify health: `./manage-bot.sh <bot-name> verify`

### Troubleshooting
- **Logs**: Use `journalctl --user -u <bot-name>.service -f`
- **Status**: Use `systemctl --user status <bot-name>.service`
- **Manual Test**: You can run the bot script directly using the shared venv:
  ```bash
  ~/matrix-bots/venv/bin/python ~/matrix-bots/<bot-name>/<script>.py
  ```

---

## 5. Adding a New Bot to the Collection

To add a new bot, follow these steps:

1. **Create Role**: `mkdir -p roles/my_bot/{defaults,files,tasks}`
2. **Define Properties**: Create `roles/my_bot/defaults/main.yml` with the `bot_properties` dict.
3. **Add Script**: Place your Python script in `roles/my_bot/files/`.
4. **Lifecycle Tasks**: Create `roles/my_bot/tasks/main.yml` following the state-driven pattern (see `matrix_watcher` for a template).
5. **Update Wrapper**: Add your bot to the `BOT_MAP` and `SUPPORTED_BOTS` in `manage-bot.sh`.
6. **Update Inventory**: Add a `my_bot_svc` group to `inventory/localhost.yml` (or your custom inventory).

---

## 6. Security Guidelines

- **User Authorization**: AI bots like `claude-code-bot` should implement a strict whitelist of Matrix User IDs allowed to trigger commands.
- **Read-Only Operations**: By default, bot tools should be restricted to read-only operations within specific sandbox directories.
- **Service Isolation**: Each bot runs as a separate systemd user service to provide process-level isolation.
