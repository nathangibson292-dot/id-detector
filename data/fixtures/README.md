# Fixtures

Committed fixtures are **synthetic by default**. Raw research dumps (SoundCloud/YouTube comment corpora containing public usernames, IDs and profile URLs) live in `data/raw/` which is git-ignored and never committed.

Where a real snippet is indispensable for parser behaviour, `scripts/derive_fixtures.py` (Stage 0) produces it from `data/raw/comments/**` into `data/fixtures/hints/`:

- **deterministic fixture-local identities** (`author_001`, `author_002`, … in first-seen order;
  never a stable hash of the handle)
- user IDs, comment IDs, profile URLs and any URLs in text removed
- `@mentions`, personal names and incidental personal text redacted
- comment timestamps rounded to whole seconds
- only the text needed for parser behaviour retained

`scripts/audit_fixtures.py` runs in CI/tests and **fails** if any committed fixture contains a
handle pattern (`@…`), a URL, a contextual raw identifier (including opaque identifiers), or text
that matches a raw-dump line verbatim. Derived hint fixtures also have a closed record grammar,
safe vocabulary, filename convention, and sequential-author check that work in a clean clone where
the optional raw corpus is absent.

Authored synthetic cases for each parsing trap live in `data/fixtures/hints/synthetic/`: title-first lines, bare hyphens in names, minute-only cues, `H:MM:SS` in multi-hour sets, dotted decimals, ranges, malformed hours, Unicode, standalone `ID - ID`, single-line mega-tracklists, `@`-replies without a resolvable parent, conflicting corrections, non-track questions (negative cases).

Provenance of the raw dumps is recorded locally in `data/raw/comments/README.md` (not committed).
