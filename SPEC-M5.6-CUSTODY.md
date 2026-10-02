# SPEC — M5.6: custody-quality tag (Theory-approved 2026-10-02, v2.2)

## Goal
Split digital presence by CUSTODY QUALITY, derived purely from existing evidence
columns — no re-derivation, no network. The acquisition queue will rank
restricted-custody books above open-custody ones ("exists at Google's pleasure
is one policy change from extinction").

## Rule (pure SQL/Python, new function `assign_custody(conn)`)
New column `enrich_status.custody TEXT` (nullable):
- `'open'` — an open, mirrored, preservation-mandated digital copy exists:
  `ht_access='allow'` OR `ia_identifier IS NOT NULL` (IA counts as open by
  default; lending-collection distinction is a future refinement — note in
  code comment)
- `'restricted'` — readable digital exists ONLY in restricted custody:
  NOT open per above AND `gb_status='full'`
- `'none'` — no readable digital found anywhere we checked: no open signal,
  gb_status IS NULL or in ('none','metadata')
- `'unknown'` — checks incomplete (e.g., ia never ran); expected zero rows
  in the current workset

## CLI + export
- New subcommand `assign-custody` (idempotent, recomputes all rows).
- `export_workset`: add `custody` column after `status`; ordering unchanged
  (status severity, then score DESC) — custody informs downstream ranking,
  not list order.
- `assign-status` chains custody automatically at the end (single entry point).

## Tests
- Table-driven: open-via-ht, open-via-ia, open-beats-restricted (both signals →
  open), restricted-only-gb-full, none-via-gb-metadata, none-via-nothing,
  unknown-when-ia-unchecked.
- Export: header + column present; assign-status chain assigns custody too.
