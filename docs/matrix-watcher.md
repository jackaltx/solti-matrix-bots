# matrix-watcher — Design and Operations

Last updated: 2026-07-02

## Purpose

Passive event monitor for Matrix rooms. Joins a room and logs every event to journald.
No AI, no user interaction, no state. Its job is to prove the Matrix pipeline is working:
if events are flowing through the homeserver and reaching the bot, the infrastructure is healthy.

Primary use cases:
- Verifying a new bot VM has working Matrix connectivity before deploying AI bots
- Debugging room membership, event delivery, or homeserver routing issues
- Confirming the `solti-event` protocol layer (`com.solti.event`) is being emitted correctly

---

## Bot Identity

| Field | Value |
|-------|-------|
| Matrix user | `@matrix-watcher:jackaltx.com` |
| Room | `#solti-verify:jackaltx.com` |
| Secret | `MATRIX_SOLTI_MATRIX_WATCHER_TOKEN` |
| Dependencies | `matrix-nio` only |

---

## What It Does

On startup, connects to the homeserver, joins the configured room, and registers callbacks
for all Matrix event types. Each event is logged at INFO level:

```
2026-07-02 17:29:08 | Room !FVP...:jackaltx.com handling event of type RoomMessageText
```

That's it. No replies, no processing, no storage.

The startup banner shows connectivity status:

```
Matrix Bot Starting (matrix-nio)
  Homeserver: https://matrix-web.jackaltx.com
  Bot User:   @matrix-watcher:jackaltx.com
  Room:       #solti-verify:jackaltx.com
  Protocol:   layered (L1=com.solti.event)
```

---

## Deployment

```bash
source ~/.secrets/LabMatrix
./manage-bot.sh matrix-watcher prepare
./manage-bot.sh matrix-watcher deploy
```

Health check (role `verify` action is broken — use systemctl directly):

```bash
systemctl --user status matrix-watcher.service
journalctl --user -u matrix-watcher -f
```

---

## Known Issues

**`verify` action fails**: The role's state validation rejects `verify` as a valid state.
Use `systemctl --user status` instead.

**Token name**: Must be `MATRIX_SOLTI_MATRIX_WATCHER_TOKEN` in `~/.secrets/LabMatrix`.
An older name (`MATRIX_WATCHER_TOKEN`) existed in early versions and will silently produce
an empty token, causing authentication failure.

---

## Troubleshooting

**Bot starts but no events appear**: Check room membership. The bot must be invited to
`#solti-verify:jackaltx.com` and have accepted. Verify with:

```bash
journalctl --user -u matrix-watcher | grep -i "join\|room"
```

**Authentication failure on startup**: Token is empty. Verify the secret is exported:

```bash
echo $MATRIX_SOLTI_MATRIX_WATCHER_TOKEN
source ~/.secrets/LabMatrix && ./manage-bot.sh matrix-watcher deploy
```
