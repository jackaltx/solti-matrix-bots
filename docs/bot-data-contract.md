# Bot Data Contract

Defines what each bot reads and writes across every service it touches.
This is the authoritative reference for:

- Vault path structure (what to provision)
- MongoDB user permissions (what collections need read/write)
- S3 IAM policies (what buckets need access)
- Redis ACL design (when Redis is added)
- Ansible role defaults (what env vars get injected)

**Rule:** Before adding a new bot, fill in the table below. Before adding a new
service (Redis, etc.), add a column and annotate every existing bot.

---

## Service Map

| Bot | Matrix | Anthropic | MongoDB | S3 | Redis | Vault path |
|-----|--------|-----------|---------|-----|-------|------------|
| matrix-watcher | read | — | — | — | — | — |
| claude-code-bot | read | claude-haiku (analysis) | — | — | — | `infrastructure/mylab/anthropic` |
| brain2-bot | read | claude-haiku (classify) | write: `ideas`, `replay_log` | — | — | `runtime/<host>/brain2-bot/{mongodb,anthropic}` |
| card-capture-bot | read | claude-sonnet (vision) | write: `inbox`, `people`, `bot_state`, `replay_log` | write: `card-capture/` | — | `runtime/<host>/card-capture-bot/{mongodb,rustfs,anthropic}` + `infrastructure/matrix-web/synapse/bots/card-capture` |
| salty-bot | read | claude-sonnet (vision) + claude-haiku (text cleanup) | write: `ideas`, `sessions`, `processed_events` | write: `salty-captures/` | **planned** | `runtime/<host>/salty-bot/{mongodb,rustfs,anthropic}` + `infrastructure/matrix-web/synapse/bots/salty` |

---

## Per-Bot Detail

### matrix-watcher

**Purpose:** Validates Matrix events — logs what it sees, no persistent state.

| Service | Access | Detail |
|---------|--------|--------|
| Matrix | read | Listens on `#solti-verify`, logs all event types |
| Anthropic | none | — |
| MongoDB | none | — |
| S3 | none | — |
| Redis | none | — |

**Vault paths:** None. Token injected directly via `MATRIX_SOLTI_WATCHER_TOKEN` env var (not yet Vault-sourced).

**Scoped access needed:** Matrix token only.

---

### claude-code-bot

**Purpose:** AI code analysis assistant on Matrix.

| Service | Access | Detail |
|---------|--------|--------|
| Matrix | read | Listens on `#solti-dev`, posts analysis replies |
| Anthropic | claude-haiku | Code analysis; API key read from env |
| MongoDB | none | — |
| S3 | none | — |
| Redis | none | — |

**Vault paths:** `infrastructure/mylab/anthropic` → `api_key`

**Scoped access needed:** Matrix token + Anthropic API key.

**Note:** Not yet wired to AppRole auth — still reads `ANTHROPIC_API_KEY` from env.

---

### brain2-bot

**Purpose:** Second-brain classifier — routes Matrix messages to MongoDB collections.

| Service | Access | Detail |
|---------|--------|--------|
| Matrix | read | Listens on `#SecondBrain`, classifies all messages |
| Anthropic | claude-haiku | Classification prompt; low-cost model intentional |
| MongoDB | write | `second_brain.ideas` (classified content), `second_brain.replay_log` (audit trail) |
| S3 | none | Text-only; no binary storage |
| Redis | none | — |

**Vault paths:**
- `runtime/<host>/brain2-bot/mongodb` → `uri`, `username`, `password`
- `runtime/<host>/brain2-bot/anthropic` → `api_key` *(or shared `infrastructure/mylab/anthropic`)*

**MongoDB user permissions:** `readWrite` on `second_brain` db — `ideas` + `replay_log` collections.

**Scoped access needed:** Matrix token + Anthropic API key + MongoDB write.

---

### card-capture-bot

**Purpose:** Business card OCR — image in, structured contact data out.

| Service | Access | Detail |
|---------|--------|--------|
| Matrix | read | Listens on `#CardCapture`, processes `RoomMessageImage` events |
| Anthropic | claude-sonnet (vision) | Extracts contact fields from card image; higher model intentional |
| MongoDB | write | `second_brain.inbox` (staging), `second_brain.people` (committed), `second_brain.bot_state` (event context), `second_brain.replay_log` (audit) |
| S3 | write | `card-capture/<year>/<month>/<id>.jpg` — raw card image stored before extraction |
| Redis | none | — |

**Vault paths:**
- `runtime/<host>/card-capture-bot/mongodb` → `uri`, `username`, `password`
- `runtime/<host>/card-capture-bot/rustfs` → `access_key`, `secret_key`
- `infrastructure/matrix-web/synapse/bots/card-capture` → `password` (Matrix login)
- `infrastructure/mylab/anthropic` → `api_key`

**MongoDB user permissions:** `readWrite` on `second_brain` — `inbox`, `people`, `bot_state`, `replay_log`.

**S3 IAM policy:** `card-capture-bot` user, scoped to `card-capture` bucket only.

**Write order on image receipt:** S3 upload → Claude extraction → MongoDB inbox write. S3 failure is hard — card not saved. MongoDB failure after S3 upload leaves orphaned S3 object (known gap).

---

### salty-bot

**Purpose:** Voice-friendly session capture — text/image/audio in, structured `ideas` out.

| Service | Access | Detail |
|---------|--------|--------|
| Matrix | read | Listens on `#SecondBrain`, trigger word `salty` |
| Anthropic | claude-sonnet (vision) for images, claude-haiku (text cleanup) for transcription | Two models intentional — vision needs sonnet, text cleanup doesn't |
| MongoDB | write | `second_brain.ideas` (committed sessions), `second_brain.sessions` (in-progress, keyed by sender), `second_brain.processed_events` (dedup) |
| S3 | write | `salty-captures/<type>/<year>/<month>/<id>.<ext>` — images, audio, video |
| Redis | **planned** | Session state caching — `sessions` collection currently in MongoDB but a hot read/write target; Redis would reduce Mongo churn and enable cross-instance session sharing |

**Vault paths:**
- `runtime/<host>/salty-bot/mongodb` → `uri`, `username`, `password`
- `runtime/<host>/salty-bot/rustfs` → `access_key`, `secret_key`
- `infrastructure/matrix-web/synapse/bots/salty` → `password` (Matrix login)
- `infrastructure/mylab/anthropic` → `api_key`

**MongoDB user permissions:** `readWrite` on `second_brain` — `ideas`, `sessions`, `processed_events`.

**S3 IAM policy:** `salty-bot` user, scoped to `salty-captures` bucket only.

**Redis design (when added):**
- Key: `session:<matrix_user_id>` → serialized `CaptureSession` JSON
- TTL: `AUTOSAVE_MINUTES` (default 10 min) + grace period
- Access: `salty-bot` ACL user, commands: `GET SET DEL EXPIRE TTL`
- Vault path: `runtime/<host>/salty-bot/redis` → `password` (or ACL username+password)
- MongoDB `sessions` collection becomes the durable fallback / audit log, not the hot store

---

## Shared Infrastructure

### Anthropic API Key

Currently: `infrastructure/mylab/anthropic` → `api_key` (one key for all bots).

Brain2-bot references a separate `anthropic` path under `runtime/` but in practice
the same key is used. **Decision deferred:** per-bot API keys would allow per-bot
spend tracking but add key management overhead. Revisit when spend visibility matters.

### MongoDB Database

All bots share `second_brain` database. Per-bot MongoDB users are scoped to this
database with `readWrite`. Collections are namespaced by purpose:

| Collection | Owner | Purpose |
|------------|-------|---------|
| `ideas` | brain2-bot, salty-bot | Committed classified content |
| `inbox` | card-capture-bot | Staged cards awaiting review |
| `people` | card-capture-bot | Committed contact records |
| `bot_state` | card-capture-bot | Event context (active conference/event tag) |
| `sessions` | salty-bot | In-progress capture sessions |
| `processed_events` | salty-bot | Event dedup (short TTL) |
| `replay_log` | brain2-bot, card-capture-bot | Audit trail |

**Gap:** No per-collection access restriction today — each bot's MongoDB user has
`readWrite` on the entire `second_brain` db. Acceptable for now; revisit if
collection isolation becomes a requirement.

### S3 / rustfs

Each bot gets its own IAM user and bucket. No cross-bot bucket access.
Defined in `solti-docker/inventory/group_vars/rustfs_svc.yml`.

### Vault AppRole

Each bot has its own AppRole role and policy. Policy scopes read to exactly the
Vault paths that bot needs. Defined by `solti-docker/roles/hashivault/tasks/configure_bot_approle.yml`.

---

## Adding a New Bot — Checklist

1. **Fill in this table** — what does the bot read/write on each service?
2. **MongoDB:** add to `configure.yml` loop in `solti-docker/roles/mongodb/`; create collections
3. **S3 (if needed):** add to `rustfs_iam_users` in `group_vars/rustfs_svc.yml`; run `rustfs configure`
4. **Redis (if needed):** add ACL entry; define key namespace + TTL; Vault path at `runtime/<host>/<bot>/redis`
5. **Vault AppRole:** add to `vault_bot_roles` in inventory; run `hashivault configure_bot_approle`
6. **Matrix:** provision room + bot user via `matrix-manage.sh provision`
7. **Ansible role:** add `bot_properties` + `secrets` list; `_bot_base` handles the rest
8. **bot-data-contract.md:** update this file
