# Salty-Bot Session Persistence Plan (2026-05-12)

## 1. Problem Statement
`salty-bot` currently stores active capture sessions in an in-memory dictionary. This architecture has two primary flaws:
1. **Volatile State**: Any service restart, crash, or deployment immediately discards all open sessions, leading to data loss for the user.
2. **Race Conditions**: Since sessions are popped from memory before processing completes, messages sent during the "Save" window are ignored.

## 2. Proposed Solution: MongoDB-Backed Sessions
Move all active session state to a new `sessions` collection in the existing `second_brain` database.

### 2.1 Collection Schema
- **Collection Name**: `sessions`
- **Primary Key (`_id`)**: The sender's Matrix ID (e.g., `@user:domain.com`). This ensures one active session per user.

**Document Structure:**
```json
{
  "_id": "@user:domain.com",
  "intent": "Initial trigger text",
  "started_at": "ISODate",
  "last_activity": "ISODate",
  "text_parts": ["Part 1", "Part 2"],
  "attachments": [
    {
      "type": "image",
      "mxc_url": "mxc://...",
      "s3_bucket": "salty-captures",
      "s3_key": "...",
      "description": "..."
    }
  ]
}
```

### 2.2 Operational Logic Changes

#### A. Trigger Phase (salty <intent>)
- Perform an **upsert** to the `sessions` collection.
- If a session already exists for that user, trigger an immediate "Autosave" of the old content before overwriting with the new trigger intent.

#### B. Accumulation Phase (Text/Image/Video/Audio)
- Use MongoDB atomic operators to avoid reading the whole document:
  - `$push`: Append to `text_parts` or `attachments`.
  - `$set`: Update `last_activity` to the current timestamp.
- **Benefit**: Even if the bot crashes immediately after a message arrives, the message is already safe in the database.

#### C. Completion Phase (salty done)
1. **Retrieve**: Read the full session document.
2. **Flag**: (Optional) Update the document with a `status: "processing"` flag to ignore incoming messages during the Claude/S3 window.
3. **Process**: Run Claude vision/cleanup.
4. **Commit**: Write to the `ideas` collection.
5. **Cleanup**: Delete the document from the `sessions` collection only *after* the commit succeeds.

#### D. Autosave Loop
- Periodically query the `sessions` collection:
  `db.sessions.find({"last_activity": {"$lt": now - 10 minutes}})`
- Process each returned session using the standard Completion Phase logic.

## 3. Implementation Steps

1. **Update `CaptureSession` Dataclass**: Add a `from_dict` and `to_dict` method to handle serialization between the Python object and MongoDB.
2. **Refactor Callbacks**: Replace local `sessions[sender]` access with `db.sessions.update_one(...)`.
3. **Refactor `save_session`**: Update it to fetch from MongoDB instead of popping from a local dict.
4. **Resiliency**: Ensure that if Claude processing fails, the session remains in the `sessions` collection so the user doesn't lose their data and can try again.

## 4. Technical Debt to Address Simultaneously
- **Event De-duplication**: Move `_processed_events` to a TTL-indexed collection in MongoDB to prevent double-processing across restarts.
- **Model Configuration**: Externalize `VISION_MODEL` and `TEXT_MODEL` to environment variables.
- **Error Handling**: Standardize the reply to users when MongoDB is unreachable.
