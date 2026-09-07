# card_capture_bot

Deploys `card-capture-bot`, a Matrix bot that captures business card images, stores them to
S3-compatible object storage, and holds extracted contact data in an inbox collection for
review before committing to the `people` MongoDB collection.

Full design documentation: [docs/card-capture.md](../../docs/card-capture.md)

## Requirements

- Shared Python venv at `~/matrix-bots/venv` (created by `prepare` action)
- MongoDB instance accessible via `BRAIN2_MONGODB_URI`
- S3-compatible object storage (MinIO or equivalent) accessible via `S3_ENDPOINT_URL`
- Matrix homeserver with a registered `@card-capture` user and `#CardCapture` room
  (provisioned by `mylab/playbooks/matrix/card-capture-matrix-config.yml`)

## Role Variables

Defined in `defaults/main.yml`. Override in `inventory/group_vars/all.yml`.

| Variable | Default | Notes |
|---|---|---|
| `domain` | env `MATRIX_DOMAIN` | Matrix server domain |
| `matrix_homeserver_url` | env `MATRIX_HOMESERVER_URL` | Homeserver URL |
| `card_capture_bot_room` | env / `card_capture_bot_room_default` | Room ID or alias |
| `brain2_mongodb_uri` | env / `mongodb://localhost:27017` | MongoDB connection string |
| `brain2_mongodb_db` | env / `second_brain` | Database name |
| `s3_endpoint_url` | env / `http://localhost:9000` | S3/MinIO API endpoint |
| `s3_bucket` | env / `card-captures` | Bucket name (auto-created on startup) |
| `matrix_allowed_users` | `""` | Comma-separated Matrix user IDs |

## Secrets

Resolved from shell environment at deploy time. Must be set before running `manage-bot.sh`:

| Variable | Description |
|---|---|
| `MATRIX_CARD_CAPTURE_TOKEN` | Bot Matrix access token |
| `ANTHROPIC_API_KEY` | Anthropic API key (Claude Sonnet vision) |
| `S3_ACCESS_KEY` | S3/MinIO access key ID |
| `S3_SECRET_KEY` | S3/MinIO secret access key |

For localhost MinIO, bridge credentials in `~/.secrets/LabMatrix`:
```bash
export S3_ACCESS_KEY="${MINIO_ROOT_USER}"
export S3_SECRET_KEY="${MINIO_ROOT_PASSWORD}"
```

## Dependencies

None. Uses shared `_bot_base` role for lifecycle management.

## Lifecycle

```bash
source ~/.secrets/LabMatrix

./manage-bot.sh card-capture-bot prepare   # create venv + directories (once)
./manage-bot.sh card-capture-bot deploy    # install deps, copy script, start service
./manage-bot.sh card-capture-bot verify    # check service status and logs
./manage-bot.sh card-capture-bot remove    # stop and disable service
```

## Future: Sub-Agent Model

card-capture-bot is currently a single always-on instance in `#CardCapture`. The intended
long-term model is **one card-capture instance per project room**, spawned by a delegator
(salty-bot) when a session starts. Each room maps to a project; the bot is scoped to that
room's data and S3 bucket prefix for the duration of the project.

Under this model:

- S3 credentials become per-room (not per-bot-type) — Vault path shifts to `kv/rooms/<room-id>/...`
- The delegator needs Vault write access to provision credentials at room-creation time
- MongoDB writes are scoped to a project identifier, not a global `card_inbox` collection

The current Ansible deploy (one instance, static credentials) is the correct foundation.
Phase 2 is a runtime provisioning problem, not an Ansible problem.

## Bot Usage

Drop any business card photo in `#CardCapture` — no `@mention` needed.

```
/commit [note]   — save last pending card to people
/reextract       — re-run vision on last card's S3 image
/discard         — discard last card (S3 image kept for audit)
/pending         — show all cards waiting in inbox
/event <name>    — tag subsequent scans with an event name
/status          — connection health and session stats
/help            — command list
```
