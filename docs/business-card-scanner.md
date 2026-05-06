# Business Card Scanner — brain2-bot Extension

## Concept

Extend brain2-bot to accept image uploads in the Matrix room. User photographs a business
card at a conference or meeting, drops it into the Matrix chat, and the bot extracts contact
data and stores it in the `people` collection — no manual entry.

## Target Use Case

Conference and business meeting workflow. The friction point is the gap between meeting someone
and having them in your second brain. Currently that requires manual entry, usually done later
(and often forgotten). This collapses it to: snap card → send to Matrix room → done.

UX constraints driven by this context:
- User is standing at a booth or walking a floor, not at a desk
- Mobile image quality varies (glare, angle, blur)
- Speed matters — the whole round-trip should feel fast
- Reply must be scannable at a glance to verify capture before walking away

## How It Works

### Current flow (text)
```
@mention in room → classify text → store in collection → reply summary
```

### Extended flow (image)
```
m.image in room → download from homeserver → Claude vision extraction
                → store in people collection → reply showing captured fields
```

1. User uploads card image to the Matrix room (`m.image` message type)
2. Bot detects `m.image` event (separate callback from existing `m.text` handler)
3. Bot downloads image bytes via nio's `download()` against the homeserver media API
4. Sends image to Claude vision (Sonnet — not Haiku, vision quality matters)
5. Prompt asks for structured extraction: name, title, company, email, phone, address, website, social handles
6. Response routes directly to `people` collection — no classifier confidence routing needed
7. Bot replies with a summary of captured fields so user can verify on the spot

## What Exists in brain2-bot Already

| Component | Status | Notes |
|---|---|---|
| `build_people_doc()` | Ready | Schema fits business card fields well |
| `store_entry()` + `replay_log` | Ready | Unchanged |
| Reply/ACK flow | Ready | Unchanged |
| `m.image` event handler | Missing | New callback needed |
| Media download | Missing | nio `download()` call |
| Vision extraction prompt | Missing | Claude vision, not Haiku text classifier |

## Implementation Notes

### Event handling
matrix-nio uses `add_event_callback()` per event type. Add a second callback for
`RoomEncryptedMedia` / `RoomMessageMedia` alongside the existing `RoomMessage` handler.
The image msgtype check: `event.msgtype == "m.image"`.

### Media download
```python
response = await client.download(mxc=event.url)
image_bytes = response.body
```
The homeserver returns raw bytes. Pass as base64 to the Claude vision API.

### Claude vision call
Use `AsyncAnthropic` (not sync `Anthropic`) — image downloads + vision API calls make
the async client important here. The vision call replaces the classifier; route directly
to `people`, skip confidence threshold logic.

```python
response = await aclient.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": CARD_EXTRACTION_PROMPT}
        ]
    }]
)
```

### Low-confidence / bad image handling
If the vision model cannot extract enough fields (e.g., blurry image), bot should:
- Store what it got in `unclassified` with `source: "card-image"` 
- Reply listing what it captured and what it missed
- Ask a follow-up: "I got name and company but couldn't read the email — can you type it?"
User can reply inline to complete the record (requires a pending-record reconciliation flow
or manual `/update <id>` command).

### Internationalization
Claude vision handles multilingual cards natively — no extra work needed.

### Image storage
Decision pending: store the raw image in MongoDB (GridFS) alongside the people doc, or
discard after extraction? Storing enables re-extraction if the prompt improves. GridFS adds
dependency complexity. Could also store the mxc:// URI and re-download on demand (but
homeserver media may be purged).

## Reply Format

Needs to be scannable in 2 seconds while standing at a conference:

```
**Captured** — Sarah Chen, VP Engineering @ Acme Corp
  email: sarah@acme.com  |  phone: +1-555-0123
  website: acme.com
ID: `64f3a...`
```

Flag missing fields clearly: `⚠️ phone not found on card`

## Comparison to Text Classification

| | Text classifier | Card scanner |
|---|---|---|
| Model | Haiku (fast, cheap) | Sonnet (vision quality) |
| Confidence routing | Yes (< 0.6 → unclassified) | No (always people) |
| Schema | Dynamic by classification | Fixed: people fields |
| Async importance | Low (Haiku fast) | High (image download + vision) |

## Open Questions

- Store raw image bytes in MongoDB alongside the people doc?
- `/update <id>` command to patch a record with follow-up info?
- Should card images require the `@mention` trigger or fire on any image in the room?
- Room restriction — card scanner only in `#SecondBrain` room, not general rooms?

## Recommended Starting Point: Dedicated `#CardCapture` Room

Rather than adding image handling to brain2-bot's existing room, start with a dedicated
`#CardCapture` room and a minimal `card_capture_bot` role.

**Why this is clean:**

- The room itself signals intent — no @mention syntax, no trigger word needed
- Any image dropped in the room gets processed unconditionally
- Completely isolated from the main `#SecondBrain` room during development
- Easy to wipe and retry without affecting live data

**What card_capture_bot needs** (compared to brain2-bot):

- No classifier machinery
- No slash commands (or just `/status` and `/help`)
- `m.image` handler only — ignore all text
- Vision extraction → `build_people_doc()` → `people` collection
- Reuse MongoDB connection pattern from brain2-bot unchanged

**Phone UX:** Open `#CardCapture`, drop image, walk away. The room boundary replaces
all the trigger/mention complexity.

This is a natural fit as a new bot role following the existing `bot_properties` pattern,
with `card_capture_bot` as its own entry in `BOT_MAP` and `inventory/localhost.yml`.
