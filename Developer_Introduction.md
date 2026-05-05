# Developer Introduction: solti-matrix-bots

This is an **Ansible collection** that deploys and manages Matrix bots as systemd user services.
If you're familiar with server administration but new to Ansible or this codebase, start here.
For deployment commands, see [README.md](README.md).

---

## What Ansible Is Doing Here

Ansible is the automation engine. It connects to a host (local or remote), runs tasks in sequence,
and ensures the system reaches a defined state. Tasks are written in YAML and executed by Ansible
modules — `copy`, `pip`, `systemd`, `template`, and others.

In this project, Ansible:
- Creates directories (`~/matrix-bots/`)
- Installs Python packages into a shared venv
- Copies bot scripts to the right location
- **Renders a Jinja2 template** into a systemd service file
- Enables and starts the service

The entry point is `manage-bot.sh`, which generates a short Ansible playbook on the fly and runs it.

---

## What Jinja2 Is Doing Here

Jinja2 is a templating language embedded in Ansible. It uses `{{ variable }}` syntax.

There is one explicit Jinja2 template in this collection:

**[roles/_bot_base/templates/bot.service.j2](roles/_bot_base/templates/bot.service.j2)**

This template is rendered at deploy time into a real systemd unit file
(`~/.config/systemd/user/matrix-watcher.service`, for example). Variables like
`{{ bot_properties.venv_dir }}` and `{{ bot_properties.restart_policy }}` are substituted
with values from Ansible's variable system before the file is written to disk.

Jinja2 also appears throughout the YAML files themselves — any `{{ }}` expression in a task
or defaults file is Jinja2. For example:

```yaml
# From roles/matrix_watcher/defaults/main.yml
working_dir: "{{ lookup('env', 'MATRIX_WORKING_DIR') | default(matrix_working_dir) }}"
```

This reads an environment variable at runtime, falling back to a group_vars value.

---

## The Core Pattern: bot_properties

Every bot role defines a single dictionary called `bot_properties` in its
`defaults/main.yml`. This is the **configuration contract** between a specific bot role
and the shared `_bot_base` role that actually does the work.

```
roles/matrix_watcher/
  defaults/main.yml   ← defines bot_properties (what, where, how)
  tasks/main.yml      ← validates input, then delegates to _bot_base
  files/              ← the Python bot script

roles/_bot_base/
  tasks/prepare.yml   ← reads bot_properties, sets up venv and dirs
  tasks/present.yml   ← reads bot_properties, deploys service
  tasks/cleanup.yml   ← reads bot_properties, removes service
  tasks/verify.yml    ← reads bot_properties, checks health
  templates/
    bot.service.j2    ← reads bot_properties, renders systemd unit
```

Adding a new bot means writing a new role that follows the same `bot_properties` schema.
The base role handles the rest.

---

## The State-Driven Lifecycle

Bots are not just deployed — they have a lifecycle managed through an explicit `state` parameter.
This pattern comes from `solti-containers` (which uses the same model for Podman quadlets).

```
prepare → present → verify → absent
```

Each bot's `tasks/main.yml` reads a `{bot_name}_state` variable and delegates to the
appropriate `_bot_base` task file. The lifecycle is **idempotent**: running `deploy` twice
is safe and produces the same result.

`manage-bot.sh` translates CLI verbs (`deploy`, `remove`, `verify`) to Ansible state values
and injects them as variables when it runs the generated playbook.

---

## Configuration: Three Layers

Values flow from highest to lowest priority:

1. **Environment variables** — override everything, used for one-off runs
2. **`inventory/group_vars/all.yml`** — site-specific config, gitignored
3. **Role defaults** — fallback values, safe for public commit

Secrets (bot tokens, API keys) are resolved from the **shell environment at deploy time**.
The template uses `lookup('env', secret_name)` to read each token from the current shell and
bake its value directly into the rendered systemd unit file. This means you must source your
secrets file before running `manage-bot.sh`:

```bash
source ~/.secrets/LabMatrix
./manage-bot.sh matrix-watcher deploy
```

If the secret isn't in the environment when Ansible runs, it renders as an empty string — the
service starts but immediately fails to authenticate. This is the most common first-deploy mistake.

### Sample `~/.secrets/LabMatrix`

```bash
# ~/.secrets/LabMatrix
# Shell-sourceable secrets file — chmod 600, never commit

# Matrix bot tokens
# Obtain from Synapse admin API or Element > Settings > Help & About > Access Token
export MATRIX_WATCHER_TOKEN="syt_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export MATRIX_SOLTI_CLAUDE_CODE_TOKEN="syt_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# Anthropic API key
# Obtain from https://console.anthropic.com/settings/keys
export ANTHROPIC_API_KEY="sk-ant-api03-xxxxxxxx"
```

The variable names here must match exactly what each bot's `bot_properties.secrets` list declares —
that's what `lookup('env', secret_name)` resolves against at deploy time.

---

## Where to Start Reading

To understand the system, read in this order:

1. [roles/matrix_watcher/defaults/main.yml](roles/matrix_watcher/defaults/main.yml) — see what `bot_properties` looks like in practice
2. [roles/_bot_base/templates/bot.service.j2](roles/_bot_base/templates/bot.service.j2) — see the only Jinja2 template
3. [roles/_bot_base/tasks/present.yml](roles/_bot_base/tasks/present.yml) — see how a deployment actually runs
4. [manage-bot.sh](manage-bot.sh) — see how the CLI wraps everything

---

## README vs This Document

The **README** is an operator's guide: setup steps, commands, troubleshooting, secret management.

This document is a developer's guide: mental model, pattern explanation, where things live and why.
Once the model clicks, the README commands will make sense.
