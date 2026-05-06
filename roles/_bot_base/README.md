# _bot_base

Shared base role used by all Matrix bots in this collection. Handles common infrastructure so individual bot roles stay focused on their specific logic.

## What it does

- Creates shared bot data directories
- Sets up a shared Python virtual environment
- Installs per-bot Python requirements into the venv
- Deploys bot scripts as systemd user services
- Manages service lifecycle (start, stop, enable, disable)

## States

| State | Action |
|-------|--------|
| `prepare` | One-time setup: directories, venv creation |
| `present` | Deploy and start the systemd service |
| `absent` | Stop and remove the service (data preserved) |
| `verify` | Check service status and recent logs |

## `bot_properties` contract

Each bot role defines a `bot_properties` dict in its `defaults/main.yml`. `_bot_base` consumes it:

```yaml
bot_properties:
  root: "my-bot"                          # directory name under data_dir
  name: "my-bot.service"                  # systemd unit name
  script_name: "my-bot.py"               # script filename
  data_dir: "{{ real_user_dir }}/matrix-bots"
  bot_dir:  "{{ real_user_dir }}/matrix-bots/my-bot"
  venv_dir: "{{ real_user_dir }}/matrix-bots/venv"
  requirements:
    - "matrix-nio>=0.25.2"
  environment:
    MATRIX_HOMESERVER_URL: "{{ matrix_homeserver_url }}"
  secrets:
    - MY_BOT_TOKEN
```

Secrets are resolved from the shell environment at deploy time via `lookup('env', name)` and written into the systemd service unit.
