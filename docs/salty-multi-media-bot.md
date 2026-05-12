# Salty Bot — Voice-Friendly Multi-Media Capture for Second Brain

Last updated: 2026-05-12

---

## The Bigger Picture

**Salty is a gathering tool. The payoff is the presentation toolset.**

The bot's job is frictionless capture — text, photos, audio, video, files — while
mobile, without typing. The value is unlocked downstream when that captured content
is assembled into a structured document: an assessment report, a project diary, a
proposal with embedded photos and transcribed notes.

Two concrete use cases that shaped the architectural direction:

**Professional site audit:** A contractor walks a property photographing conditions,
recording voice observations, capturing receipts and measurements. At the end of the
visit — or across multiple visits — the gathered content is assembled into an
Assessment Report: sections by area, embedded photos, transcribed audio, cost summary.

**Gardener's plant diary:** Photos of plants over time, voice notes about conditions,
soil test PDFs, water usage data. Each visit is a capture session. The output is a
living document organized by plant or bed, with a photo timeline and accumulated notes.

The common thread: **project-scoped capture over time, assembled into a multi-media
document.** The bot handles the capture side. A presentation layer (web UI, PDF
generator, Obsidian export) handles the assembly side. MongoDB is the join point.

This framing drives architectural decisions that v1 only partially addresses. See the
Follow-On Ideas section for the project context concept and assembly tooling.

---

## Purpose (v1 scope)

Salty solves the friction of capturing an idea while mobile. Voice-to-text on a phone
produces reasonable prose but not slash commands. A trigger word ("salty") at the start
of a message is something voice-to-text handles naturally, and the rest of the message
is free-form intent. No mode-switching, no special syntax, no typing.

The bot shares the `#SecondBrain` Matrix room with `brain2-bot`, sitting in the same
conversation stream but responding to a different pattern. Brain2 responds to `@mentions`
and classifies structured content. Salty opens a session and waits for you to finish talking.

**UX constraints driving the design:**
- Voice-to-text will not produce `/commands` reliably
- A note might be one sentence or five messages and two photos
- You should be able to walk away mid-session and have it save itself
- The trigger word should feel natural spoken aloud ("salty I have an idea")

---

## Current Architecture (v1)

### Trigger and Session Model

**Option C** — hybrid trigger/accumulation:

- Only `salty <anything>` as the first word opens or continues a session
- Any message from an allowed user while a session is active is accumulated (no trigger needed)
- `salty done` / `salty cancel` / `salty status` are the only control words
- Voice punctuation on the trigger (`"Salty,"`, `"Salty."`) is stripped before matching

This means: once you say "salty I have an idea", just keep talking. The bot
stays quiet while you accumulate content so the chat feels like a notebook, not a dialog.

### Session Lifecycle

```
"salty <intent>"
      ↓
  Session opened (in-memory, keyed by sender)
      ↓
  [any message]  → text accumulated
  [image]        → download → S3 → Claude vision description → attachment
  [video]        → download → S3 → attachment (no transcription)
  [file/pdf]     → download → S3 → attachment with filename
      ↓
"salty done"  OR  10 min idle (autosave loop)
      ↓
  Claude Haiku: clean text + generate title + extract tags
      ↓
  Write to second_brain.ideas
      ↓
  Reply with title + content summary + tags
```

If a new "salty" trigger arrives while a session is already open, the existing session
is autosaved first, then the new session opens. No content is lost.

### Storage

**MongoDB** — `second_brain.ideas`:

```json
{
  "title":        "AI-generated 4-8 word title",
  "intent":       "I have an idea",
  "text_parts":   ["raw voice message 1", "raw voice message 2"],
  "cleaned_text": "Claude-cleaned combined prose",
  "attachments":  [
    {
      "type":        "image | video | file",
      "filename":    "original filename (files only)",
      "mxc_url":     "mxc://jackaltx.com/...",
      "s3_bucket":   "salty-captures",
      "s3_key":      "images/2026/05/<objectid>.jpg",
      "description": "Claude vision description (images only)"
    }
  ],
  "tags":         ["tag1", "tag2"],
  "sender":       "@jackal:jackaltx.com",
  "captured_at":  "2026-05-12T...",
  "saved_at":     "2026-05-12T...",
  "autosaved":    false,
  "source":       "salty-bot"
}
```

**S3** — `salty-captures` bucket, organized by media type:

```
salty-captures/
  images/2026/05/<objectid>.jpg
  videos/2026/05/<objectid>.mp4
  files/2026/05/<objectid>.pdf
```

### Claude Usage

| Step | Model | Purpose |
|---|---|---|
| Image arrives | Sonnet (vision) | 2-3 sentence description of image content |
| Save / autosave | Haiku | Clean voice text, generate title, extract tags |

Image description happens at capture time (immediate feedback). Text processing
happens at save time (one call covers the whole session). Both are gracefully
skipped if `ANTHROPIC_API_KEY` is missing — raw text and no tags are stored instead.

### Ansible Role

Deployed via `salty_bot` role in `solti-matrix-bots`:

```bash
source ~/.secrets/LabMatrix
./manage-bot.sh salty-bot prepare
./manage-bot.sh salty-bot deploy
./manage-bot.sh salty-bot verify
```

Shares the Python venv at `~/matrix-bots/venv` with all other bots. Requires
the `@salty` Matrix user (provisioned by `mylab/playbooks/matrix/salty-matrix-config.yml`).

---

## What Works Now (v1)

- [x] Voice-friendly trigger word with punctuation tolerance
- [x] Multi-message text accumulation (silent during session)
- [x] Image capture: Matrix download → S3 → Claude vision description
- [x] Video capture: Matrix download → S3 (stored, not described)
- [x] File capture: Matrix download → S3 with original filename (PDFs, docs, etc.)
- [x] Claude Haiku text cleanup + title + tag extraction on save
- [x] 10-minute autosave on idle (configurable via `SALTY_AUTOSAVE_MINUTES`)
- [x] New trigger over open session autosaves first (no lost content)
- [x] `salty status` shows session age, content count, time until autosave
- [x] `salty cancel` discards session without saving
- [x] Stores to `second_brain.ideas` in shared MongoDB

---

## Known Gaps and Technical Debt

### Session Persistence

Sessions are held in memory. A bot restart drops any open session without saving.
For a 10-minute autosave window this is mostly acceptable, but a crash mid-session
loses everything since the last message. Options:

- Write session state to MongoDB on every incoming message (adds latency)
- Write a `sessions` collection on open/update, clean up on save/cancel
- Accept the loss — sessions are short-lived and the autosave window is short

### Audio Messages

iPhones can send voice memos as `m.audio` events. The current bot has no
`RoomMessageAudio` callback — voice memos are silently ignored. This is a significant
gap given the bot's voice-first design philosophy. Options:

- Store the audio file to S3 unmodified (same pattern as video)
- Transcribe via a local Whisper instance or an external API
- Send the audio to Claude as a file attachment if/when the API supports audio input

### Video Transcription

Videos are stored to S3 but not described or transcribed. A short video clip
of a whiteboard explanation is captured but its content is invisible to search.
This requires a transcription step (Whisper, ffmpeg + STT) that is beyond v1 scope
but would significantly improve recall quality.

### Large File Handling

No size limits on downloads. A large video or PDF will be downloaded in full
before the S3 upload begins. For very large files this could stall the bot's
async loop. A size check after download with a user-facing warning is the
minimal fix.

### Keyword Position Assumption

The bot assumes "salty" is literally the first word. Voice-to-text sometimes
prepends filler ("um, salty" or "okay salty"). A small lookahead — check the
first two words, or use a regex for "salty" within the first 20 characters —
would make the trigger more robust.

### No Output Commands

The bot is capture-only. There is no way to query or browse ideas from Matrix.
`second_brain.ideas` is write-only from the bot's perspective. Output and CRUD
are deliberately deferred — see the ideas section below.

---

## Follow-On Ideas

These are not planned work — they are directions worth considering as the system matures.

### Project Context — the key missing concept

The single most important architectural addition. Right now a session is a one-shot
capture that lands in `second_brain.ideas`. For the site audit and garden diary use
cases, you need a **persistent container** that accumulates captures across multiple
sessions and visits.

Proposed trigger pattern:
```
salty project Oak Street Audit     ← create/open a named project
salty                               ← all subsequent captures tag to active project
salty close project                 ← mark project complete, ready for assembly
salty project status               ← show active project + capture count
```

MongoDB schema addition — `second_brain.projects`:

```json
{
  "_id":        "<ObjectId>",
  "name":       "Oak Street Audit",
  "status":     "active | closed",
  "created_at": "...",
  "closed_at":  null,
  "sender":     "@jackal:example.com",
  "captures":   ["<ideas ObjectId>", "<ideas ObjectId>", ...]
}
```

Each `ideas` document gets a `project_id` field when captured under an active project.
The assembly tool queries by `project_id` to get the full timeline in order.

Projects are per-sender but could be shared (multiple people capturing to the same
project from the same room — useful for team site audits).

### Document Assembly — the presentation toolset

The gathering is only as valuable as the output. Once a project is closed (or even
while open), a separate assembly tool produces the structured document. This is NOT
a bot feature — it is a separate concern that reads from MongoDB.

**For a site audit report:**

- Query all `ideas` where `project_id = X`, ordered by `captured_at`
- Group by location/time if tagged
- Render images inline (S3 presigned URLs)
- Embed transcribed audio as quoted text blocks
- Summarize costs from receipt captures (Claude extraction)
- Output: PDF, HTML, or Markdown

**For a garden diary:**

- Query by project, further filter by plant/bed tag
- Render as a photo timeline with notes beneath each image
- Overlay soil test data, water usage (if captured as files or text)
- Output: Obsidian markdown with embedded images, or a paginated web view

**The implementation path:** A Python script that takes a `project_id`, queries
MongoDB, fetches S3 assets, and renders to a template. Start with Markdown (simple,
Obsidian-compatible), add PDF via `weasyprint` or `pandoc` later. Claude can assist
with structure: summarize the captures, write section headings, highlight anomalies.

### Audio Capture (Stage 1 — implemented)

`RoomMessageAudio` callback: download → S3 → session attachment. No transcription.
The audio file is preserved and the S3 key is stored in the `ideas` document.
Transcription decision deferred to the user or an async post-processing job.

### Audio Transcription (Stage 2 — future)

Once audio is in S3, transcription can happen asynchronously:

- Local Whisper container (no external API, runs on CPU for short clips)
- Triggered on `salty done` or as a background job scanning `ideas` where
  `attachments[].type == "audio"` and `attachments[].transcript` is null
- Transcript written back to the MongoDB attachment object
- Makes all voice content searchable and includable in assembled documents

### Output and Retrieval (high value, natural next step)

The most obvious gap: you can put things in but not get them out via Matrix.

**Simple retrieval commands:**
```
salty show last          — display the most recently saved idea
salty show 3             — display last 3 ideas
salty search dashboards  — full-text search across ideas
```

The challenge is formatting MongoDB documents readably in a Matrix message.
Truncation and pagination matter here — a long note will flood the room.

**Semantic search:**
Rather than keyword search, embed ideas at capture time (or lazily on first query)
and search by cosine similarity. Claude's embedding API or a local model
(e.g. `all-MiniLM`) could power this. The `description` field on image attachments
makes images searchable too.

### Intent Classification and Routing

Currently everything lands in `second_brain.ideas`. The trigger intent ("I have an idea",
"I need to remember", "I want to") could be used to route to different collections:

| Trigger pattern | Target collection | Notes |
|---|---|---|
| "I have an idea" / "idea" | `ideas` | current default |
| "remind me" / "don't forget" | `reminders` | needs TTL + notification |
| "make a note" / "note this" | `notes` | general capture |
| "task" / "I need to" / "to do" | `tasks` | could feed a task system |

This would require a small classifier call at session open (cheap with Haiku)
and would make the `ideas` collection semantically cleaner.

### Reminder / Follow-Up Scheduling

"salty remind me about this in 3 days" — parse the time expression, store a
`remind_at` field, and have a background job ping the Matrix room when it fires.
The autosave loop's once-per-minute check could double as a reminder scanner.

The Matrix notification could be a simple room message quoting the saved idea title
and linking back to the MongoDB ID for the user to act on.

### Event Context (card-capture pattern)

`card-capture-bot` has an `/event <name> [hours]` mechanism that tags all captures
during a conference or meeting window. The same pattern applied to salty would let
you say "salty event AWS re:Invent 4h" and have all subsequent captures tagged
automatically for the duration. Useful for bulk-reviewing everything captured at
an event.

### Audio Transcription Pipeline

The full voice-native vision: drop a voice memo, get back a clean text note.

```
Voice memo (m.audio)
        ↓
Download from Matrix → S3
        ↓
Transcribe (Whisper local or API)
        ↓
Feed transcript into session as a text part
        ↓
Claude cleanup at save time
        ↓
Stored with audio_s3_key + transcript
```

This would make the entire capture flow hands-free — say "salty I have an idea",
record a voice memo, say "salty done", and get a clean searchable note with no typing.

### PDF Content Extraction

PDFs are currently stored to S3 with filename but no content extraction.
Claude's API supports PDF documents as input — passing the PDF bytes directly
to Claude at capture time (same way images are handled) would extract text and
produce a description, making PDFs searchable in the ideas collection.

### Cross-Bot Integration

Salty and brain2-bot share a room and a database. Ideas that mention people
("I had a great conversation with Sarah Chen about...") could be cross-linked
to the `people` collection that `card-capture-bot` populates. Either:

- Tag detection: if an idea mentions a name that matches a `people` record, add a reference
- Explicit link: "salty link this to Sarah Chen" — store `people_ref: <ObjectId>`

This starts to look like a knowledge graph. The shared MongoDB database is the
natural join point.

### Web Viewer / Review Interface

A lightweight web app that queries `second_brain.ideas`, renders attachments inline
from S3 presigned URLs, and supports basic review actions (tag edit, delete, link).
The Matrix bot is the capture interface; the web UI is the review/browse interface.

This is likely where output and CRUD land — not in Matrix (awkward for
structured data), but in a purpose-built UI that reads the same MongoDB database.

### Obsidian / Markdown Export

Many users of second-brain systems use Obsidian. An export script (or bot command)
that converts `second_brain.ideas` to dated markdown files with YAML frontmatter and
embedded image links would let the data live in both systems:

```markdown
---
title: Bot Health Dashboard Idea
tags: [bots, monitoring, dashboard]
captured: 2026-05-12
source: salty-bot
---

We should build a single dashboard that shows all bot health in one place...

![[salty-captures/images/2026/05/abc123.jpg]]
```

### CRM and Outlook Integration

The `second_brain` MongoDB database is the integration point for external systems.
Both `people` (from card-capture) and `ideas` (from salty) are in the same database,
which makes it a natural backend for pushing data outward.

**Outlook Contacts sync (`people` → Outlook):**
Microsoft Graph API supports creating and updating contacts programmatically.
A sync script or scheduled job could iterate `people` where `source: card-capture`
and upsert to Outlook contacts, mapping the MongoDB schema to vCard fields:

```text
people.name        → contact displayName
people.email[]     → emailAddresses
people.phone[]     → businessPhones / mobilePhone
people.company     → companyName
people.title       → jobTitle
people.card_image  → S3 presigned URL stored as a note
people.meet_note   → body / notes field
people.event_name  → categories tag
```

**CRM integration (HubSpot, Salesforce, etc.):**
Most CRMs expose a contacts + notes API. The `people` collection maps cleanly
to a CRM contact record. The `meet_note` field and any linked `ideas` referencing
that person become CRM activity notes. The `event_name` field tags contacts by
where they were met.

A lightweight sync agent (Python script, run on a schedule) could:

1. Query `people` where `synced_crm: false` (or `synced_at < updated_at`)
2. Upsert to CRM via API
3. Write back `synced_at` and `crm_id` to the MongoDB record

**Outlook Calendar from reminders:**
When the reminder scheduler (see above) fires, it could create an Outlook calendar
event via Graph API rather than (or in addition to) sending a Matrix message.
"salty remind me in 3 days to follow up with Sarah Chen" → calendar event with
the contact linked.

**The key architectural point:** the bots are the capture interface, MongoDB is the
integration hub, and external systems (Outlook, CRM, Obsidian) are consumers that
read from MongoDB. Nothing couples the bots directly to external APIs — the sync
layer is a separate concern that can be built, scheduled, or replaced independently.

### Multi-User Shared Sessions

Two people in `#SecondBrain` capturing notes from the same meeting. Currently
sessions are per-sender — each person has their own session. A shared session
(triggered by "salty shared session for standup") that both users can contribute
to would produce a richer collaborative capture.

Implementation is straightforward (key sessions by a session ID rather than
sender, with an invite mechanism), but the UX of shared state in a group room
needs careful thought.

---

## Deployment Reference

```bash
# Provision Matrix user (one-time)
cd mylab
source ~/.secrets/LabProvision && source ~/.secrets/LabMatrix
./bin/matrix-playbook.sh salty-matrix-config.yml

# Deploy bot
cd solti-matrix-bots
source ~/.secrets/LabProvision && source ~/.secrets/LabMatrix
./manage-bot.sh salty-bot prepare
./manage-bot.sh salty-bot deploy

# Monitor
journalctl --user -u salty-bot.service -f
```

**Required secrets** (in `~/.secrets/LabMatrix` before deploy):

| Variable | Source |
|---|---|
| `MATRIX_SALTY_TOKEN` | Generated by `salty-matrix-config.yml` playbook |
| `ANTHROPIC_API_KEY` | Anthropic console |
| `S3_ACCESS_KEY` | MinIO root user (bridged from `MINIO_ROOT_USER` in LabProvision) |
| `S3_SECRET_KEY` | MinIO root password |
| `BRAIN2_MONGODB_URI` | Shared with brain2-bot and card-capture-bot |
