# AXELR AHE — Anti-Hallucination Guarantee (v2.0)

## What this engine GUARANTEES

Every claim emitted has passed a **deterministic** verifier for its declared
tier. Unverified claims are **dropped** or cause `UnverifiableDataError` —
never softened, never guessed, never invented.

## What this engine DOES NOT CLAIM

The following are impossible for a non-oracle to guarantee. The engine
refuses to pretend otherwise:

- **Source truthfulness** — if the input document lies, we output its lie
  verbatim, marked verified against that source.
- **Omnipotent world-truth** — no system can verify every true fact.
- **Source fetch** — research verifier does not fetch URLs.
- **Semantic misattribution** — a claim can paraphrase a source while
  keeping the citation correct.
- **Visual appearance** — design verifier does not render.
- **Actual WCAG conformance** — only primitive presence (alt, label, ARIA role).

Every tier's `does_not_catch` list is machine-readable via
`REGISTRY.inventory()`.

## Open-world refusal

A query with no source exits via `UnverifiableDataError`. The engine does
not speculate. **This is the anti-hallucination behavior, not a bug.**

## The honest claim

> **0% unverified output across every declared tier, with every tier's
> blind spot documented and machine-readable.**

That is achievable. "0% world-truth error across all possible things"
is not — and any system claiming it is hallucinating about its own
capabilities. This engine refuses to do that.