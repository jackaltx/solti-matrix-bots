# brain2_bot

Deploys `brain2-bot`, a Matrix bot that classifies messages and captures them to MongoDB using the Anthropic API. Acts as a "second brain" — messages sent to its room are categorized and stored for later retrieval.

## Requirements

- MongoDB instance accessible from the bot host
- Anthropic API key
- Matrix bot account with access token

## Secrets (must be set in environment before deploy)

| Variable | Description |
|----------|-------------|
| `MATRIX_SOLTI_BRAIN2_TOKEN` | Matrix bot access token |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `BRAIN2_MONGODB_URI` | MongoDB connection URI |

```bash
source ~/.secrets/LabMatrix
./manage-bot.sh brain2-bot deploy
```

## Variables (`inventory/group_vars/all.yml`)

| Variable | Required | Description |
|----------|----------|-------------|
| `domain` | yes | Matrix domain (e.g. `example.com`) |
| `matrix_homeserver_url` | yes | Homeserver URL |
| `brain2_bot_room_default` | yes | Room alias (e.g. `#SecondBrain:example.com`) |
| `matrix_allowed_users` | yes | Comma-separated permitted Matrix user IDs |

## States

```bash
./manage-bot.sh brain2-bot prepare   # set up venv and directories
./manage-bot.sh brain2-bot deploy    # start service
./manage-bot.sh brain2-bot verify    # check status
./manage-bot.sh brain2-bot remove    # stop and remove service
```
