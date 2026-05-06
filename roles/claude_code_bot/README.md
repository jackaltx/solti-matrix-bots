# claude_code_bot

Deploys `claude-code-bot`, a Matrix bot that provides AI analysis assistance via the Anthropic Claude API. Messages sent to its room are processed by Claude and responses are posted back.

## Requirements

- Anthropic API key
- Matrix bot account with access token

## Secrets (must be set in environment before deploy)

| Variable | Description |
|----------|-------------|
| `MATRIX_SOLTI_CLAUDE_TOKEN` | Matrix bot access token |
| `ANTHROPIC_API_KEY` | Anthropic API key |

```bash
source ~/.secrets/LabMatrix
./manage-bot.sh claude-code-bot deploy
```

## Variables (`inventory/group_vars/all.yml`)

| Variable | Required | Description |
|----------|----------|-------------|
| `domain` | yes | Matrix domain (e.g. `example.com`) |
| `matrix_homeserver_url` | yes | Homeserver URL |
| `claude_code_bot_room_default` | yes | Room alias |
| `matrix_allowed_users` | yes | Comma-separated permitted Matrix user IDs |

## States

```bash
./manage-bot.sh claude-code-bot prepare
./manage-bot.sh claude-code-bot deploy
./manage-bot.sh claude-code-bot verify
./manage-bot.sh claude-code-bot remove
```
