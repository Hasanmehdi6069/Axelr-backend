# Data formats

## Undo manifest

Path: `_axelr/inbox-organizer/undo.jsonl`
Format: one JSON object per line.
Cap: 50 lines. When the log exceeds 50, rewrite the file without the oldest
line — never edit in place.

| Field | Type | Meaning |
|---|---|---|
| `op_id` | UUID | unique operation id |
| `timestamp` | epoch seconds | when the move happened |
| `src` | absolute path | original location |
| `dst` | absolute path | filed location |
| `sha256` | hex string | hash of source before move |
| `category` | string | extracted category |
| `document_type` | string | extracted type |

Undo reads the last line, moves `dst` back to `src` with overwrite off,
then rewrites the manifest without that line.

## Daily summary

Path: `_axelr/inbox-organizer/summary/<YYYY-MM-DD>.md`
Sidecar: `_axelr/inbox-organizer/summary/<YYYY-MM-DD>.jsonl`

The Markdown is derived from the JSONL, not appended in place. Totals are
grouped by currency; amounts are summed only when `document-agent` returned
a numeric `total_amount`. Files due within 7 days are counted, not listed.