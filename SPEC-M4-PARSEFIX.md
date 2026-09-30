# SPEC — M4 real-dump parsing fixes (Theory-greenlit 2026-09-30)

## Context
The first real-dump run (2026-09-30, lab, 34.6M editions / 21.9M candidates) exposed two
parser defects the string-based fixtures could not catch. The Open Library REAL dumps store
languages and authors as dicts (`{"key": "/languages/eng"}`, `{"key": "/authors/OL1A"}`);
the fixture generator emits plain strings, so golden tests passed while real data corrupted.

## Defect 1 — languages dict-repr leak (RANKING DISTORTION, not cosmetic)
- `extract_edition` in `lastcopy/m4.py`: `langs = [str(l).rsplit("/", 1)[-1] for l in _as_list(obj.get("languages"))]`
- Real shape: `[{"key": "/languages/eng"}]` → `str(dict)` → `"{'key': '/languages/eng'}"` → rsplit → `"eng'}"`.
- Scoring impact: `W_LANG_ENG=0, W_LANG_UNKNOWN=1, W_LANG_OTHER=2`. Artifact `"eng'}"` falls into
  `W_LANG_OTHER` (+2). 84% of the top-10k carried this artifact — English rows were inflated by +2.
- Fix: dict-aware extraction: `l.get("key") if isinstance(l, dict) else l`, then the existing
  `rsplit("/", 1)[-1]`, then `str(...)`. Must yield clean codes: `eng`, `por`, etc. Also strip
  whitespace. Empty list → language None (unchanged).

## Defect 2 — works author_keys collapse to '[]'
- After `ingest-works` on the real dump, `works_ref.author_keys` is `'[]'` for the vast majority
  of works, so `resolve_authors` returns "" (top-10k CSV author column 84% empty).
- The works dump stores `"authors": [{"key": "/authors/OL123A"}, ...]`.
- Inspect `ingest_works` in `lastcopy/m4.py`; fix the author_keys extraction to handle the dict
  shape (same pattern as Defect 1). authors_ref ingestion already stores `/authors/OL…` keys
  correctly (verified against the real DB) — keys must MATCH that form for `resolve_authors`.
- Verify against the real dump-derived expectation: the author fill-rate on candidates should
  land near the works-with-authors rate in the dump, not ~0.

## Fixture + tests (must prevent recurrence)
- Extend the fixture generator (tests/, cf13791) to emit REAL dump shapes: languages as dicts,
  works authors as dicts — at least one record each of: dict-language eng, dict-language other,
  string-language (back-compat), dict-authors list of 1 and 2, authors absent.
- Regression tests named for the incident:
  - `test_real_dump_language_dict_shape` — parse `[{"key": "/languages/eng"}]` → `"eng"`; no `'} ` substring anywhere in stored rows.
  - `test_real_dump_works_author_dict_shape` — works authors → `["/authors/OL123A"]`-style keys; `resolve_authors` returns names, not "".
- Update golden fixtures/expectations that assumed string shapes (keep golden CSV equivalence
  test green: regenerate the golden artifact through the fat-equivalent path if needed).
- Full suite: `.venv/bin/python -m pytest tests/ -q` — all green.

## Out of scope (must not change)
- Scoring weights, filters, SQL, ordering, schema, slim design, export backfill. The top-10k
  output is EXPECTED to reorder after this fix — that is correctness, not regression.

## Acceptance (real re-run on lab, after merge)
- Top-10k language column: zero `'} ` artifacts; distribution dominated by clean codes.
- Author column fill-rate ≫ 16%.
