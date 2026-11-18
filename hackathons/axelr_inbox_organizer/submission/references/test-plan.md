# Test plan for the finalist NAS

These are the only things that cannot be verified without the provided
UGREEN environment. Validate them first.

1. **Cross-device `cpmv`** — does the platform's move handle a source and
   destination on different volumes? If it copies-then-deletes, confirm the
   source is only removed after the copy succeeds.
2. **Scanned / encrypted PDFs** — does `document-agent extract` return a
   low-confidence result or a clear error? The skill must treat both as
   "leave in place", never as "file with empty metadata".
3. **`sha256sum` on large files** — confirm it streams rather than loading
   the whole file into memory.
4. **Permission scoping** — confirm the skill cannot read a folder the user
   did not name in the invocation.
5. **Manifest write atomicity** — confirm a crash mid-`cpmv` does not leave
   a manifest line for a move that did not complete.