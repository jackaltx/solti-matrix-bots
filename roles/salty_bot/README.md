# salty_bot

Deploys `salty-bot`, a voice-friendly Matrix capture bot. Say `salty [anything]` to open
a capture session, send text/images/video across multiple messages, then `salty done` to
save to the `second_brain` MongoDB database. Sessions autosave after inactivity.

Shares the `#SecondBrain` room with brain2-bot (which it replaces).

## Requirements

- Shared Python venv at `~/matrix-bots/venv` (created by `prepare` action)
- MongoDB instance accessible via `BRAIN2_MONGODB_URI`
- S3-compatible object storage (MinIO) accessible via `S3_ENDPOINT_URL`
- Matrix homeserver with a registered `@salty` user in `#SecondBrain`

## Role Variables

Defined in `defaults/main.yml`. Override in `inventory/group_vars/all.yml`.

| Variable | Default | Notes |
|---|---|---|
| `domain` | env `MATRIX_DOMAIN` | Matrix server domain |
| `matrix_homeserver_url` | env `MATRIX_HOMESERVER_URL` | Homeserver URL |
| `salty_bot_room` | env / `salty_bot_room_default` | Room ID or alias |
| `brain2_mongodb_uri` | env / `mongodb://localhost:27017` | MongoDB connection string |
| `brain2_mongodb_db` | env / `second_brain` | Database name |
| `s3_endpoint_url` | env / `http://localhost:9000` | S3/MinIO API endpoint |
| `s3_bucket` | env / `salty-captures` | Bucket name (auto-created on startup) |
| `salty_autosave_minutes` | env / `10` | Inactivity timeout before autosave |
| `matrix_allowed_users` | `""` | Comma-separated Matrix user IDs |

## Secrets

Must be set before running `manage-bot.sh`:

| Variable | Description |
|---|---|
| `MATRIX_SALTY_TOKEN` | Bot Matrix access token |
| `ANTHROPIC_API_KEY` | Anthropic API key (Claude vision + text cleanup) |
| `S3_ACCESS_KEY` | S3/MinIO access key ID |
| `S3_SECRET_KEY` | S3/MinIO secret access key |

## Lifecycle

```bash
source ~/.secrets/LabMatrix

./manage-bot.sh salty-bot prepare   # create venv + directories (once)
./manage-bot.sh salty-bot deploy    # install deps, copy script, start service
./manage-bot.sh salty-bot verify    # check service status and logs
./manage-bot.sh salty-bot remove    # stop and disable service
```

## Bot Usage

Trigger word is `salty` (case-insensitive) as the first word of any message.
No slash commands — designed for voice-to-text input.

```
salty I have an idea          — start capture session
salty note this               — start capture session
salty done                    — save session to ideas collection
salty cancel                  — discard session without saving
salty status                  — show current mode and session summary
```

Once a session is open, send any combination of text, images, or video.
Session autosaves after `SALTY_AUTOSAVE_MINUTES` (default: 10) of inactivity.

## Storage

Ideas are stored in `second_brain.ideas` with:
- `text_parts[]` — accumulated voice-to-text messages (cleaned by Claude)
- `attachments[]` — S3 keys for images and video
- `title` — Claude-generated summary
- `tags[]` — Claude-extracted tags
- `sender`, `captured_at`
