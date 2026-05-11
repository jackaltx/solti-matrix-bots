# Card Capture Bot — Inbox + S3 Storage Plan

## Current State

Bot captures card images, runs Claude Sonnet vision, and writes directly to MongoDB
`people` collection. The mxc:// URI is stored but image bytes are not persisted
independently of the Matrix homeserver.

**Problems with current approach:**
- Direct write to `people` — no chance to review before committing
- Image only survives as long as Matrix media retention keeps it
- No re-extraction without re-photographing the card

---

## Target Architecture

### Capture → Inbox → Commit flow

```
Drop image in #CardCapture
        ↓
Download from Matrix homeserver
        ↓
Upload to S3 (card-captures bucket)
        ↓
Run Claude Sonnet vision extraction
        ↓
Write to MongoDB inbox collection (status: pending)
        ↓
Reply with extracted fields

--- user reviews ---

/reextract    → re-run vision on stored S3 image, update inbox record
/commit [note]→ move inbox record to people collection
/discard      → delete inbox record (S3 image retained for audit)
```

### Why inbox instead of direct write

- `people` collection contains only confirmed records
- Mobile UX: snap card, glance at reply, `/commit` or `/reextract` while still at the booth
- No ID needed for any command — always operates on the last captured card

---

## S3 Image Storage

### Design principle

Store bucket + key in MongoDB, not the full URL. The endpoint is an env var.
Switching from MinIO to RustFS (or any S3-compatible store) = one env var change,
no data migration.

### Object key scheme

```
card-captures/
  2026/
    05/
      <mongodb-objectid>.jpg
```

Named by ObjectId — no collisions, no special characters from card data.
Name and date are queryable from MongoDB; the key is just a stable pointer.

### MongoDB fields added to inbox record

```json
{
  "card_image_mxc":    "mxc://jackaltx.com/qKKlFdVRnhkRrDuqZeIBwmMD",
  "card_image_bucket": "card-captures",
  "card_image_key":    "2026/05/69fbb0e6962a16a6c7f23890.jpg"
}
```

The full access URL is constructed at runtime:
```python
f"{S3_ENDPOINT_URL}/{bucket}/{key}"
```

### S3 env vars

| Variable | Purpose |
|---|---|
| `S3_ENDPOINT_URL` | MinIO/RustFS/S3 endpoint (e.g. `http://localhost:9000`) |
| `S3_ACCESS_KEY` | Access key ID |
| `S3_SECRET_KEY` | Secret access key |
| `S3_BUCKET` | Bucket name (default: `card-captures`) |

---

## MongoDB Collections

### `inbox` (new)

```json
{
  "_id":             "ObjectId",
  "status":          "pending | committed | discarded",
  "extracted":       { ...vision output fields... },
  "confidence":      0.85,
  "card_image_mxc":  "mxc://...",
  "card_image_bucket": "card-captures",
  "card_image_key":  "2026/05/<id>.jpg",
  "captured_at":     "2026-05-06T21:00:00Z",
  "committed_at":    null,
  "meet_note":       "",
  "sender":          "@jackal:jackaltx.com",
  "extraction_attempts": 1
}
```

### `people` (existing — unchanged schema)

Committed records land here. Two new fields added:

```json
{
  "card_image_bucket": "card-captures",
  "card_image_key":    "2026/05/<id>.jpg",
  "meet_note":         "AWS re:Invent 2026",
  "source":            "card-capture"
}
```

---

## Bot Commands (revised)

| Command | Action |
|---|---|
| _(drop image)_ | Extract → inbox → reply with fields |
| `/reextract` | Re-run vision on last pending card's S3 image, update inbox record |
| `/commit [note]` | Move last pending card to `people`; optional meet note stored |
| `/discard` | Mark inbox record discarded; S3 image kept for audit |
| `/pending` | Show count and summary of cards waiting in inbox |
| `/status` | MongoDB + S3 connection + session stats |
| `/help` | Command list |

---

## Implementation Steps

### Step 1 — S3 plumbing (blocker: MinIO ready)

- Add `boto3` to venv requirements
- Add S3 env vars to `card-capture-bot.service`
- Add `S3_ENDPOINT_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET` to `~/.secrets/LabMatrix`
- Write `upload_card_image(image_bytes, object_id, media_type)` → returns `(bucket, key)`
- Create `card-captures` bucket if it doesn't exist on startup

### Step 2 — Inbox collection

- Replace direct `people` write with `inbox` write (`status: pending`)
- Store S3 bucket + key alongside mxc URI
- Track `_current_inbox_id` as session variable (last pending card)

### Step 3 — Commands

- `/reextract` — fetch image bytes from S3, re-run `extract_card()`, update inbox record
- `/commit [note]` — build people doc from inbox record, insert to `people`, set inbox `status: committed`
- `/discard` — set inbox `status: discarded`
- `/pending` — query inbox for `status: pending`, reply with count + names

### Step 4 — Reply format update

Pending state shown clearly in capture reply:
```
**Inbox** — Sarah Chen, VP Engineering @ Acme Corp
  email: sarah@acme.com  |  phone: +1-555-0123
  confidence: 85%

/commit  to save  ·  /reextract  if fields are wrong
```

---

## Future: GUI / Export

Because images are in S3 and records are in MongoDB with a stable key reference:

- **Web viewer** — query `inbox` or `people` where `source: card-capture`, render
  image from S3 alongside fields. Simple Flask/FastAPI app.
- **vCard export** — script reads `people` collection, writes `.vcf` per record,
  optionally attaches card image as PHOTO field
- **Migration** — moving from MinIO to RustFS: copy bucket, update `S3_ENDPOINT_URL`,
  done. No MongoDB changes needed.
- **Bulk re-extraction** — script iterates `inbox` or `people` where `source: card-capture`,
  re-runs vision with improved prompt, updates records

---

## Dependencies

| Package | Already in venv | Notes |
|---|---|---|
| `matrix-nio` | yes | |
| `anthropic` | yes | AsyncAnthropic for vision |
| `pymongo` | yes | |
| `boto3` | **no** | Add to requirements |

---

## Blockers

1. **MinIO instance ready** — need `S3_ENDPOINT_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`
2. **`card-captures` bucket created** — bot can auto-create on startup if credentials allow

Once MinIO is configured, Steps 1–4 above can be implemented in one pass.
