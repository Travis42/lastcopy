# SPEC — M5.8: parallel IA phase via per-slice result files + merge (Theory doctrine: decomposable or parallel)

## Problem
The IA enrichment is the long pole of every re-derivation (~2-3h serial). It runs as ONE
job executing all ~1,800 plan elements against the DB directly — serial because SQLite
single-writer. Per the 2026-10-02 doctrine, it should decompose into parallel array
elements with no shared-DB writes during execution.

## Design
### 1. Executor result-file mode (`enrich-ia --execute IDX... --results-dir DIR`)
- When --results-dir is set, `execute_ia_element` writes per-element files
  `DIR/slice_<idx>.jsonl` (one JSON object per resolved ISBN:
  `{"isbn13": ..., "ia_identifier": ...}`) and a completion marker `DIR/done_<idx>`
  instead of touching enrich_status. No DB connection needed for writing (still reads
  ia_plan from the DB read-only: `--db` stays required, opened mode=ro).
- Element skip-if-done = `done_<idx>` file exists (resume across reruns).
- Element slice selection unchanged (element_idx list from CLI).

### 2. Merge CLI (`merge-ia-results --results-dir DIR`)
- Single writer: streams all `slice_*.jsonl`, applies `UPDATE enrich_status SET
  ia_identifier=? WHERE isbn13=? AND ia_identifier IS NULL`, reports counts
  (applied/skipped-already-set/unknown-isbn), idempotent.

### 3. Array runner (`slurm-v2/job-ia-array.sbatch`, I build)
- `#SBATCH --array=0-5` (6 slices, aggregate ≤6 req/s); element k executes strided
  plan indexes k, k+6, k+12... via ia_max + seq stride; --results-dir on the shared
  tree. After the array: merge job (dependency=afterok) + assign-status + export.

## Tests
- execute with --results-dir: writes slice+done files, DB unchanged (ia_identifier stays NULL)
- rerun skips via done marker (no file rewrite)
- merge applies results exactly once; second merge = 0 applied; unknown ISBN skipped
- real-transport tests unchanged (injectable get)

## Out of scope
- status/custody rule changes; other stages; HT/WD parallelization (already fast).
