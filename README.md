# solti-matrix-bots

A self-contained deployment tool for Matrix bots on Linux using Ansible and systemd user services.

---

## What This Is — App, Not Library

Most Ansible collections are **libraries**: roles and modules that your playbooks import.
This collection is different — it is a **deployable application** you run directly from its own directory.

It ships with:

- Its own `ansible.cfg` — no system-wide Ansible config needed
- Its own inventory — a named host registry that works on any Linux machine
- `manage-bot.sh` — a dynamic playbook generator that eliminates the need to write playbooks

You clone it, configure two files, and run one command. It works on any recent Linux
distribution with Python 3.8+, Ansible, and systemd. No Galaxy installation, no external
orchestrator, no shared infrastructure required.

```bash
git clone https://github.com/jackaltx/solti-matrix-bots.git
cd solti-matrix-bots
cp inventory/group_vars/all.yml.example inventory/group_vars/all.yml
# edit all.yml and ~/.secrets/LabMatrix
source ~/.secrets/LabMatrix
./manage-bot.sh brain2-bot prepare
./manage-bot.sh brain2-bot deploy
```

This pattern is borrowed from [solti-containers](https://github.com/jackaltx/solti-containers),
which applies the same approach to Podman quadlet services.

---

## Available Bots

### matrix-watcher

Event validation bot. Joins a room and logs all Matrix events. Useful for verifying
infrastructure connectivity and room state. No AI, no user interaction.

- **Dependencies**: `matrix-nio`
- **Secret**: `MATRIX_WATCHER_TOKEN`

### claude-code-bot

AI analysis assistant. Responds to `@solti-claude-code` mentions with file reads, code
search, and safe bash execution inside a configurable working directory. Read-only by design.
Smart model selection: Haiku 4.5 for simple tasks, Sonnet 4.5 for complex analysis.

- **Dependencies**: `matrix-nio`, `anthropic`
- **Secrets**: `MATRIX_SOLTI_CLAUDE_CODE_TOKEN`, `ANTHROPIC_API_KEY`

### brain2-bot

Second brain capture bot. Responds to `@solti-brain2` mentions, classifies each message
via Claude API (people / projects / ideas / admin / unclassified), and stores the result
in MongoDB with a replay log for classifier tuning.

Confidence ≥ 60% → stored in target collection, bot replies with summary.
Confidence < 60% → stored as unclassified, bot asks a follow-up question.

Slash commands: `/help` `/status` `/cost`

- **Dependencies**: `matrix-nio`, `anthropic`, `pymongo`
- **Secrets**: `MATRIX_SOLTI_BRAIN2_TOKEN`, `ANTHROPIC_API_KEY`, `BRAIN2_MONGODB_URI`
- **Requires**: MongoDB running and reachable before deploy

---

## Quick Start

### 1. Create site configuration

`inventory/group_vars/all.yml` is **required** — the bots will not deploy without it.

```bash
cp inventory/group_vars/all.yml.example inventory/group_vars/all.yml
nano inventory/group_vars/all.yml
```

Minimum required content:

```yaml
domain: "example.com"
matrix_homeserver_url: "https://matrix.{{ domain }}"
matrix_working_dir_default: "{{ ansible_facts['env']['HOME'] }}/your/working/directory"
matrix_watcher_room_default: "#your-room:{{ domain }}"
claude_code_bot_room_default: "#your-room:{{ domain }}"
brain2_bot_room_default: "#SecondBrain:{{ domain }}"
matrix_allowed_users: "@user1:{{ domain }},@user2:{{ domain }}"
```

`matrix_allowed_users` is a comma-separated list of Matrix user IDs permitted to interact
with the bots. Users not in this list receive an unauthorized error.

### 2. Create secrets file

```bash
nano ~/.secrets/LabMatrix
chmod 600 ~/.secrets/LabMatrix
```

```bash
# ~/.secrets/LabMatrix — never commit, chmod 600

# Matrix bot tokens (from Synapse admin API or your matrix_config playbook)
export MATRIX_WATCHER_TOKEN="syt_YOUR_TOKEN_HERE"
export MATRIX_SOLTI_CLAUDE_CODE_TOKEN="syt_YOUR_TOKEN_HERE"
export MATRIX_SOLTI_BRAIN2_TOKEN="syt_YOUR_TOKEN_HERE"

# Anthropic API key (https://console.anthropic.com/settings/keys)
export ANTHROPIC_API_KEY="sk-ant-api03-YOUR_KEY_HERE"

# brain2-bot: MongoDB URI with credentials
export BRAIN2_MONGODB_URI="mongodb://user:password@localhost:27017"
```

### 3. Deploy

```bash
source ~/.secrets/LabMatrix

# One-time environment setup (creates venv, directories)
./manage-bot.sh brain2-bot prepare

# Deploy and start the service
./manage-bot.sh brain2-bot deploy

# Check health
./manage-bot.sh brain2-bot verify
```

---

## Usage

### All bot commands

```bash
./manage-bot.sh matrix-watcher prepare
./manage-bot.sh matrix-watcher deploy
./manage-bot.sh matrix-watcher verify
./manage-bot.sh matrix-watcher remove

./manage-bot.sh claude-code-bot deploy
./manage-bot.sh brain2-bot deploy

# Remove and delete data
DELETE_DATA=true ./manage-bot.sh brain2-bot remove
```

### Remote host deployment

```bash
# Deploy to a named host from your inventory
./manage-bot.sh -h myserver brain2-bot deploy

# Custom inventory file
./manage-bot.sh -i inventory/remote.yml -h myserver brain2-bot deploy

# Skip confirmation prompts
./manage-bot.sh -y -h myserver brain2-bot deploy
```

### Environment overrides

```bash
# Override any config at run time
MATRIX_HOMESERVER_URL="https://alt.example.com" \
  ./manage-bot.sh brain2-bot deploy

BRAIN2_MONGODB_URI="mongodb://user:pass@dbhost:27017" \
  ./manage-bot.sh brain2-bot deploy
```

### Monitoring

```bash
systemctl --user status brain2-bot.service
journalctl --user -u brain2-bot -f
systemctl --user list-units 'matrix-*' --all
```

---

## Architecture

### Directory structure

```
solti-matrix-bots/
├── ansible.cfg                # Self-contained Ansible config
├── manage-bot.sh              # Dynamic playbook generator (entry point)
├── galaxy.yml                 # Collection metadata
├── inventory/
│   ├── localhost.yml          # Named host registry (firefly = localhost)
│   └── group_vars/
│       └── all.yml.example    # Configuration template (copy to all.yml)
└── roles/
    ├── _bot_base/             # Shared infrastructure for all bots
    │   ├── tasks/
    │   │   ├── prepare.yml    # Venv setup, directories
    │   │   ├── present.yml    # Service deployment
    │   │   ├── cleanup.yml    # Service removal
    │   │   └── verify.yml     # Health check
    │   └── templates/
    │       └── bot.service.j2 # Systemd user service template
    ├── matrix_watcher/
    ├── claude_code_bot/
    └── brain2_bot/
```

### How manage-bot.sh works

`manage-bot.sh` translates a simple CLI verb into a complete Ansible run:

```
./manage-bot.sh brain2-bot deploy
        ↓
  generates tmp/manage-brain2-bot-deploy-<pid>.yml
        ↓
  ansible-playbook -i inventory/localhost.yml tmp/...yml
        ↓
  brain2_bot role → _bot_base/tasks/present.yml → systemd service
```

No playbooks to write. No Galaxy namespace to remember. Clone, configure, run.

### Lifecycle states

```
prepare → present → verify → absent
   ↓         ↓        ↓         ↓
 setup    deploy   health   remove
```

All operations are idempotent. Running `deploy` twice is safe.

### bot_properties pattern

Each bot role defines a `bot_properties` dict that is the contract with `_bot_base`:

```yaml
bot_properties:
  root: "brain2-bot"
  name: "brain2-bot.service"
  script_name: "brain2-bot.py"
  data_dir: "{{ real_user_dir }}/matrix-bots"
  bot_dir:  "{{ real_user_dir }}/matrix-bots/brain2-bot"
  venv_dir: "{{ real_user_dir }}/matrix-bots/venv"   # shared
  requirements: ["matrix-nio>=0.25.2", "anthropic>=0.96.0", "pymongo>=4.0"]
  environment:
    MATRIX_HOMESERVER_URL: "{{ matrix_homeserver_url }}"
    MATRIX_ALLOWED_USERS:  "{{ matrix_allowed_users | default('') }}"
  secrets:
    - MATRIX_SOLTI_BRAIN2_TOKEN
    - ANTHROPIC_API_KEY
    - BRAIN2_MONGODB_URI
```

`_bot_base` reads this dict and handles venv creation, script deployment, service
rendering, and systemd enable/start — the same way for every bot.

---

## Security

| What | Where | Status |
|------|-------|--------|
| Collection source | This repo | Public ✓ |
| Generic inventory | `inventory/localhost.yml` | Public ✓ |
| Site config | `inventory/group_vars/all.yml` | Gitignored |
| Tokens / API keys / DB URIs | `~/.secrets/LabMatrix` | Never in repo |
| Allowed user list | `matrix_allowed_users` in `all.yml` | Gitignored |

Secrets are baked into the systemd service file at deploy time via `lookup('env', name)`.
Source `~/.secrets/LabMatrix` before running `manage-bot.sh` or tokens render as empty strings.

---

## Troubleshooting

### Bot fails to authenticate

Secret rendered as empty string. Source your secrets file before deploying:

```bash
source ~/.secrets/LabMatrix && ./manage-bot.sh brain2-bot deploy
```

### Missing required configuration

`inventory/group_vars/all.yml` doesn't exist or is missing required keys.

```bash
cp inventory/group_vars/all.yml.example inventory/group_vars/all.yml
nano inventory/group_vars/all.yml
```

### Remote host

Ensure the remote has Python 3.8+, a systemd user session, and `~/.secrets/LabMatrix`.

---

## Adding a New Bot

1. Create `roles/my_bot/defaults/main.yml` — define `bot_properties`
2. Create `roles/my_bot/tasks/main.yml` — state-driven delegation to `_bot_base`
3. Add bot script to `roles/my_bot/files/`
4. Add to `BOT_MAP` and `SUPPORTED_BOTS` in `manage-bot.sh`
5. Add `my_bot_svc` group to `inventory/localhost.yml`

See `roles/brain2_bot/` as the reference implementation.

---

## Related Projects

- **[solti-containers](https://github.com/jackaltx/solti-containers)** — same pattern for Podman quadlet services
- **[solti-matrix-mgr](https://github.com/jackaltx/solti-matrix-mgr)** — Matrix Synapse admin utilities
- **[solti-conductor](https://github.com/jackaltx/solti-conductor)** — orchestrator template

---

## License

GPL-3.0-or-later

## Author

JackalTX — [github.com/jackaltx/solti-matrix-bots](https://github.com/jackaltx/solti-matrix-bots)
