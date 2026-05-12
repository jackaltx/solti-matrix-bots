# Card Capture Bot — Design and Current State

Last updated: 2026-05-12

## Purpose

Collapse the gap between meeting someone and having them in your second brain. User photographs a
business card at a conference or meeting, drops it into the `#CardCapture` Matrix room, and the
bot extracts contact data, stores the image to S3, and holds the record for a quick review before
committing to the `people` collection. No manual entry, no desk required.

**UX constraints driving the design:**
- User is standing at a booth or walking a conference floor, not at a desk
- Mobile image quality varies — glare, angle, motion blur
- Capture should feel instant; review is a glance before walking away
- A scan that isn't committed should never be silently lost

---

## Current Architecture

### Capture → Inbox → Commit Flow

```
Drop image in #CardCapture
        ↓
Download from Matrix homeserver
        ↓
Upload to S3 (card-captures bucket)   ← fails hard if S3 unavailable
        ↓
Run Claude Sonnet vision extraction
        ↓
Write to MongoDB inbox collection (status: pending)
        ↓
Reply with extracted fields + /commit hint

--- user reviews reply ---

/reextract    → re-run vision on stored S3 image, update inbox record
/commit [note]→ move inbox record to people collection
/discard      → mark inbox record discarded (S3 image retained for audit)
/pending      → show all cards waiting across all senders
```

### Key Design Decisions

**Room as trigger.** Any image dropped in `#CardCapture` is processed unconditionally.
No `@mention`, no trigger word. The room boundary is the signal.

**S3 first.** Upload happens before vision extraction. If S3 fails, the card is rejected
immediately — the bot will not write a record it cannot re-extract later.

**Inbox, not direct write.** Cards land in `inbox` with `status: pending`. The `people`
collection contains only records the user has explicitly reviewed and committed. Low-confidence
extractions go to inbox the same as high-confidence ones — the user decides, not the confidence
score.

**Last-pending is per-sender, DB-derived.** `/commit`, `/reextract`, and `/discard` always
operate on the most recent pending card for the sending user. This is looked up from MongoDB
on every command — no in-memory state — so it survives bot restarts and works correctly when
multiple users are capturing simultaneously.

**Bucket + key stored, not URL.** MongoDB records store `card_image_bucket` and `card_image_key`
separately. The endpoint is an env var. Migrating from MinIO to any S3-compatible store is a
one-line env var change with no data migration.

---

## S3 Image Storage

### Object key scheme

```
card-captures/
  2026/
    05/
      <mongodb-objectid>.<ext>
```

The ObjectId is pre-generated before both the S3 upload and the MongoDB insert. If S3 upload
fails the MongoDB write never happens. The extension is derived from the actual media type
(`image/jpeg` → `.jpg`, `image/png` → `.png`, `image/webp` → `.webp`).

### MongoDB fields in inbox record

```json
{
  "_id":                 "<ObjectId — same as S3 filename stem>",
  "status":             "pending | committed | discarded",
  "extracted":          { "name": "", "title": "", "company": "", "email": "",
                          "phone": "", "address": "", "website": "",
                          "social": [], "confidence": 0.0,
                          "missing_fields": [], "notes": "" },
  "confidence":         0.95,
  "card_image_mxc":     "mxc://jackaltx.com/...",
  "card_image_bucket":  "card-captures",
  "card_image_key":     "2026/05/<objectid>.jpg",
  "captured_at":        "2026-05-10T19:47:24Z",
  "committed_at":       null,
  "meet_note":          "",
  "sender":             "@jackal:jackaltx.com",
  "event_name":         "AWS re:Invent 2026",
  "extraction_attempts": 1
}
```

### Fields added to people on commit

```json
{
  "card_image_bucket": "card-captures",
  "card_image_key":    "2026/05/<objectid>.jpg",
  "meet_note":         "optional note from /commit",
  "event_name":        "captured at scan time from /event context",
  "source":            "card-capture"
}
```

---

## Bot Commands

| Command | Action |
|---|---|
| _(drop image)_ | Extract → inbox → reply with fields + `/commit` hint |
| `/commit [note]` | Move last pending card to `people`; note stored as `meet_note` |
| `/reextract` | Re-fetch image from S3, re-run vision, update inbox record |
| `/discard` | Mark inbox record discarded; S3 image kept for audit |
| `/pending` | Show all pending cards across all senders |
| `/event <name> [hours]` | Tag subsequent scans with an event name (default 4h TTL) |
| `/event` | Show current event and time remaining |
| `/event clear` | Clear current event |
| `/status` | MongoDB + S3 connection health + session stats |
| `/help` | Command list |

### Reply format (capture)

```
**Inbox** — Sarah Chen, VP Engineering @ Acme Corp
  email: sarah@acme.com  |  phone: +1-555-0123
  confidence: 95%  |  image saved

/commit  to save  ·  /reextract  if fields are wrong
```

---

## Deployment

The bot is deployed via the `card_capture_bot` Ansible role in `solti-matrix-bots`:

```bash
source ~/.secrets/LabMatrix      # Matrix token, Anthropic key, S3 credentials
./manage-bot.sh card-capture-bot prepare
./manage-bot.sh card-capture-bot deploy
```

**Required secrets** (in `~/.secrets/LabMatrix` before deploy):

| Variable | Source |
|---|---|
| `MATRIX_CARD_CAPTURE_TOKEN` | Generated by `card-capture-matrix-config.yml` playbook |
| `ANTHROPIC_API_KEY` | Anthropic console |
| `S3_ACCESS_KEY` | MinIO root user (bridged from `MINIO_ROOT_USER`) |
| `S3_SECRET_KEY` | MinIO root password (bridged from `MINIO_ROOT_PASSWORD`) |

**Non-secret env vars** (in `inventory/group_vars/all.yml`):

| Variable | Default | Notes |
|---|---|---|
| `S3_ENDPOINT_URL` | `http://localhost:9000` | MinIO API endpoint |
| `S3_BUCKET` | `card-captures` | Created on startup if absent |
| `BRAIN2_MONGODB_URI` | from secrets | Shared with brain2-bot |
| `BRAIN2_MONGODB_DB` | `second_brain` | Shared database |

---

## Gaps and Technical Debt

### Infrastructure

**All services on localhost.**
The bot, MongoDB, and MinIO all run on `firefly` (localhost). The Ansible role supports `-h
<host>` for remote deployment but this path has not been tested. A remote deployment would
need the S3 endpoint and MongoDB URI updated to point to the appropriate hosts, and firewall
rules to allow the bot to reach them.

**MinIO root credentials used by the bot.**
`S3_ACCESS_KEY` / `S3_SECRET_KEY` are currently bridged from `MINIO_ROOT_USER` /
`MINIO_ROOT_PASSWORD` — the MinIO admin credentials. The bot should have its own MinIO
user with write access only to the `card-captures` bucket.

**Credential bridging is manual.**
`S3_ACCESS_KEY` and `S3_SECRET_KEY` must be manually added to `~/.secrets/LabMatrix`
from values in `~/.secrets/LabProvision`. There is no Ansible task that does this
automatically.

### Functionality

**No inbox TTL or bulk cleanup.**
Pending records accumulate indefinitely. If a user captures cards and never commits or
discards them, inbox grows without bound. There is no `/discard-all`, no expiry, and no
admin tooling to bulk-clean the inbox.

**No per-record addressing from Matrix.**
`/commit`, `/reextract`, and `/discard` always operate on the *last* pending card for the
sender. There is no way to select a specific inbox record by ID from the Matrix room. To
act on an older pending record you must commit or discard the newer ones first.

**No `/update` command.**
Once a card is committed to `people` there is no bot command to patch it. Corrections
require direct MongoDB access.

**No re-extraction of committed records.**
`/reextract` only works on pending inbox records. The S3 image is retained after commit,
making bulk re-extraction with an improved prompt feasible as a script, but there is no
bot command or Ansible task for it.

**Matrix media retention is 7 days.**
The `#CardCapture` room has a 7-day retention policy. After that, the `mxc://` URI in
the record is dead. S3 is the durable copy — which is why the S3 upload must succeed
before any record is written.

### Future Work (not planned, noted for reference)

- MinIO dedicated bot user with scoped permissions
- Web viewer: query inbox/people where `source: card-capture`, render image from S3
- vCard export: iterate `people` collection, write `.vcf` per record
- Bulk re-extraction script for prompt improvements
- `/update <id> <field> <value>` command to patch committed records
- Remote deployment validation
