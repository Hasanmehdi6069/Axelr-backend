---
name: axelr-inbox-organizer
category: automate-your-workflow
description: >
  File the documents in a user-named folder into a Year/Month/Category tree,
  extract invoice/receipt/email metadata with document-agent, and write a
  dated "What's new" summary. Every move is shown before it happens and is
  recorded in an undo manifest. All processing is on-device.
user-invocable: true
version: 2.0.0
---

# Goal

Turn a messy inbox folder on the NAS into a structured, auditable document
library — without ever moving a file the user has not approved.

# Required input

- One folder path the user authorizes (for example, `Inbox`).
- One confirmation after the plan table is shown.

# Capability Evidence

| Step | Expert | Capability | Action | Purpose |
|---|---|---|---|---|
| Inventory | file-agent | `ug-filesystem` | `find` | list files in scope |
| Stat | file-agent | `ug-filesystem` | `stat` | size, mtime, path |
| Hash | file-agent | `ug-filesystem` | `sha256sum` | dedupe + undo proof |
| Extract | document-agent | `ug-document` | `extract` | date, sender, amount, due, type |
| Create folder | file-agent | `ug-filesystem` | `mkdir` | Year/Month/Category |
| Move | file-agent | `ug-filesystem` | `cpmv` | overwrite **off** |
| Write manifest | file-agent | `ug-filesystem` | `cpmv` | append to `undo.jsonl` |
| Read summary | file-agent | `ug-filesystem` | `cat` | render daily summary |

No other capability is used. No script, no network call, no background
process, no credentials.

# Workflow

## Phase 1 — Inventory (reads only)

1. `file-agent` → `ug-filesystem find` on the authorized folder.
2. For each file, `document-agent` → `ug-document extract` with the fields:
   `document_type`, `date`, `sender`, `total_amount`, `currency`, `due_date`.
3. For each file, `file-agent` → `ug-filesystem sha256sum`.
4. Build the plan table:

   | # | Source | Proposed name | Proposed folder | Confidence | Duplicate? |
   |---|---|---|---|---|---|

5. Mark any file with confidence below `0.5` or unreadable content as
   **"leave in place"**. Do not guess.

**Writes: none.**

## Phase 2 — Plan review (one confirmation)

Show the complete plan table. Ask exactly once:

> "Approve all, approve except item numbers (e.g. `except 3,5`), or cancel?"

- `approve` → Phase 3.
- `approve except N` → Phase 3 for approved rows only; skipped rows stay put.
- `cancel` → stop. Confirm that nothing was written.

**Writes: none.**

## Phase 3 — Execute (only approved rows)

For each approved file, in order:

1. `mkdir` the `Year/Month/Category` folder if missing.
2. `cpmv` the source to the destination with **overwrite off**.
3. If the destination exists, append `-1`, `-2`, … before the extension
   until the name is free.
4. Append one line to `_axelr/inbox-organizer/undo.jsonl`:

   ```json
   {"op_id":"<uuid>","timestamp":<epoch>,"src":"<abs>","dst":"<abs>",
    "sha256":"<hash>","category":"<cat>","document_type":"<type>"}