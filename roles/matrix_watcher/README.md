# matrix_watcher

Deploys `matrix-watcher`, a lightweight Matrix bot that monitors room events and validates them. Useful as a canary or audit trail for Matrix room activity.

## Requirements

- Matrix bot account with access token

## Secrets (must be set in environment before deploy)

| Variable | Description |
|----------|-------------|
| `MATRIX_SOLTI_WATCHER_TOKEN` | Matrix bot access token |

```bash
source ~/.secrets/LabMatrix
./manage-bot.sh matrix-watcher deploy
```

## Variables (`inventory/group_vars/all.yml`)

| Variable | Required | Description |
|----------|----------|-------------|
| `domain` | yes | Matrix domain (e.g. `example.com`) |
| `matrix_homeserver_url` | yes | Homeserver URL |
| `matrix_watcher_room_default` | yes | Room alias to monitor |
| `matrix_allowed_users` | yes | Comma-separated permitted Matrix user IDs |

## States

```bash
./manage-bot.sh matrix-watcher prepare
./manage-bot.sh matrix-watcher deploy
./manage-bot.sh matrix-watcher verify
./manage-bot.sh matrix-watcher remove
```
