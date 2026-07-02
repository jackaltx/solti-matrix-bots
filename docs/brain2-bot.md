# brain2-bot — Design and Operations

Last updated: 2026-07-02

## Purpose

Second-brain capture bot. Classifies messages into a personal knowledge graph stored in
MongoDB. The user @mentions the bot with a thought, note, person, or task — the bot
classifies it via Claude API and stores it in the right collection with a confidence score.

Low-confidence classifications go to an `unclassified` bucket and the bot asks a follow-up
question. A replay log captures every classification for tuning.

---

## Bot Identity

| Field | Value |
|-------|-------|
| Matrix user | `@solti-brain2:jackaltx.com` |
| Room | `#SecondBrain:jackaltx.com` |
| Secrets | `MATRIX_SOLTI_BRAIN2_TOKEN`, `ANTHROPIC_API_KEY`, `BRAIN2_MONGODB_URI` |
| Dependencies | `matrix-nio`, `anthropic`, `pymongo` |
| Requires | MongoDB running and reachable before deploy |

---

## Classification Model

```
@solti-brain2 <message>
        ↓
  Claude API classification
        ↓
  confidence ≥ 60%  →  stored in target collection, bot replies with summary
  confidence < 60%  →  stored as unclassified, bot asks follow-up question
```

### Collections

| Class | MongoDB collection |
|-------|--------------------|
| people | `second_brain.people` |
| projects | `second_brain.projects` |
| ideas | `second_brain.ideas` |
| admin | `second_brain.admin` |
| unclassified | `second_brain.unclassified` |

A replay log in `second_brain.replay` captures every input + classification for
classifier tuning.

---

## Slash Commands

| Command | Action |
|---------|--------|
| `/help` | Show usage |
| `/status` | Bot status and MongoDB connection state |
| `/cost` | Show Anthropic API token usage for this session |

---

## Deployment

```bash
source ~/.secrets/LabMatrix
./manage-bot.sh brain2-bot prepare
./manage-bot.sh brain2-bot deploy
```

### MongoDB URI

`BRAIN2_MONGODB_URI` must point to a reachable MongoDB instance before the bot starts.
On a dedicated bot VM deploy MongoDB via `solti-docker` with `mongodb_host_port: "127.0.0.1:27017"`
so the bot (running outside Docker) can reach it on localhost.

If MongoDB is unreachable at startup the bot logs:

```
ERROR | MongoDB connection failed: localhost:27017: [Errno 111] Connection refused
```

and continues running (it will retry), but classification will fail until MongoDB is available.

### Secret Name

Must be `MATRIX_SOLTI_BRAIN2_TOKEN`. An older name (`MATRIX_BRAIN2_TOKEN`) existed in
`~/.secrets/LabMatrix` — renamed 2026-07-02. If the token appears empty in the service
file, check the secret name matches exactly.

---

## Troubleshooting

**MongoDB connection refused**: Either MongoDB isn't running, or it's only exposed inside
the Docker network (missing `mongodb_host_port` in host inventory). Verify:

```bash
ss -tlnp | grep 27017   # should show 127.0.0.1:27017
docker ps | grep mongo
```

**Bot receives @mention but doesn't classify**: Sender not in `matrix_allowed_users`, or
`ANTHROPIC_API_KEY` is empty. Check service file:

```bash
grep -E 'ANTHROPIC|BRAIN2|MATRIX_SOLTI_BRAIN2' \
  ~/.config/systemd/user/brain2-bot.service
```

**Low confidence rate**: Normal for short or ambiguous messages. The bot will ask a
follow-up. Provide more context in the original message to improve classification.
