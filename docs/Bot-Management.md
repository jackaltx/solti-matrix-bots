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

## 6. Remote VM Deployment (bot-test validated 2026-07-02)

Deploying to a dedicated VM requires extra steps beyond the localhost flow.

### 6.1 Host Prep (one-time)

Run the prep playbook from `mylab` before any bot deployment:

```bash
ansible-playbook -i ../solti-matrix-bots/inventory/<host>.yml \
  ../mylab/playbooks/prep-bot-host.yml
```

This installs `python3-venv`, enables **systemd linger** (so user services survive
logout), and creates `~/.config/systemd/user/`. Without linger, all bot services
stop the moment the SSH session closes.

### 6.2 Required Infrastructure

salty-bot and card-capture-bot require **MongoDB** and **S3** before deploy.
Deploy via `solti-docker` targeting the same VM:

```bash
cd solti-docker
MONGODB_ROOT_USER=admin MONGODB_ROOT_PASSWORD=<pass> \
  ./svc-manage.sh -y -h <host> -i inventory/hosts/<host>.yml mongodb prepare
  ./svc-manage.sh -y -h <host> -i inventory/hosts/<host>.yml mongodb deploy

RUSTFS_ACCESS_KEY=<key> RUSTFS_SECRET_KEY=<secret> \
  ./svc-manage.sh -y -h <host> -i inventory/hosts/<host>.yml rustfs prepare
  ./svc-manage.sh -y -h <host> -i inventory/hosts/<host>.yml rustfs deploy
```

**MongoDB** must bind to `127.0.0.1:27017` (not just the Docker network) so
the bot systemd services can reach it. Set in host inventory:

```yaml
mongodb_host_port: "127.0.0.1:27017"
```

**RustFS** (S3-compatible) similarly needs `rustfs_host_port: "127.0.0.1:9000"`.

After deploying RustFS, create the required buckets:

```bash
AWS_ACCESS_KEY_ID=<key> AWS_SECRET_ACCESS_KEY=<secret> \
  ~/matrix-bots/venv/bin/aws --endpoint-url http://localhost:9000 s3 mb s3://salty-captures
  ~/matrix-bots/venv/bin/aws --endpoint-url http://localhost:9000 s3 mb s3://card-captures
```

### 6.3 Host Inventory Overrides

Remote host inventories need two critical overrides that are often missed:

```yaml
hosts:
  bot-test:
    ansible_host: "192.168.101.81"
    ansible_user: jackaltx
    # WorkingDirectory must exist on the REMOTE host, not the control node
    matrix_working_dir_default: "/home/jackaltx/matrix-bots"
    # S3 endpoint on the same host
    s3_endpoint_url: "http://localhost:9000"
    s3_bucket: "salty-captures"
```

Without `matrix_working_dir_default`, the service inherits the control node path
from `group_vars/all.yml` which doesn't exist on the VM — causing immediate crash
with `CHDIR: No such file or directory`.

### 6.4 Secret Names (exact names required)

| Bot | Secret env var |
|-----|---------------|
| matrix-watcher | `MATRIX_SOLTI_MATRIX_WATCHER_TOKEN` |
| claude-code-bot | `MATRIX_SOLTI_CLAUDE_CODE_TOKEN` |
| brain2-bot | `MATRIX_SOLTI_BRAIN2_TOKEN` |
| salty-bot | `MATRIX_SALTY_TOKEN` |
| card-capture-bot | `MATRIX_CARD_CAPTURE_TOKEN` |
| all AI bots | `ANTHROPIC_API_KEY` |
| brain2/salty/card | `BRAIN2_MONGODB_URI` |
| salty/card | `S3_ACCESS_KEY`, `S3_SECRET_KEY` |

Source `~/.secrets/LabMatrix` on the **control node** before running `manage-bot.sh`.
Secrets are baked into the systemd service file at template time — if a secret is
missing at deploy time it renders as an empty string and the bot fails silently.

### 6.5 Deploy All Bots to a Remote Host

```bash
source ~/.secrets/LabMatrix
export S3_ACCESS_KEY=<key> S3_SECRET_KEY=<secret>

cd solti-matrix-bots
for bot in matrix-watcher claude-code-bot brain2-bot card-capture-bot salty-bot; do
  ./manage-bot.sh -y -h bot-test -i inventory/bot-test.yml $bot prepare
  ./manage-bot.sh -y -h bot-test -i inventory/bot-test.yml $bot deploy
done
```

### 6.6 salty-bot Usage

salty-bot uses a **trigger word** (`salty`) as the first word of any message.
There are no slash commands. You must open a session before sending media.

```text
salty <description>   ← open session (description becomes context)
<send images/text>    ← accumulated into session
salty done            ← save to MongoDB + S3, Claude generates title/tags
salty cancel          ← discard session
salty status          ← show current session state
```

Images sent without an open session are silently ignored. Voice-to-text may
produce "Salted done" instead of "salty done" — the bot treats this as text
accumulation. Autosave fires after 10 minutes of inactivity.

Only users in `matrix_allowed_users` (`group_vars/all.yml`) can trigger the bot.
Messages from other users are received but silently dropped.

---

## 7. Security Guidelines

- **User Authorization**: AI bots like `claude-code-bot` should implement a strict whitelist of Matrix User IDs allowed to trigger commands.
- **Read-Only Operations**: By default, bot tools should be restricted to read-only operations within specific sandbox directories.
- **Service Isolation**: Each bot runs as a separate systemd user service to provide process-level isolation.
