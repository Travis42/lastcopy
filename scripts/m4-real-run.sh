#!/bin/bash
# m4-real-run.sh — the real M4 pipeline over live Open Library dumps.
# Phase 1 (works+authors ingests) then phase 2 (editions stream), candidates, export.
# Logs progress; every phase resumable per M4 design (stream restarts from zero).
set -uo pipefail
cd /root/projects/lastcopy
export LASTCOPY_FORCE_IPV4=1
LOG=/tmp/lc-m4-real.log
mkdir -p /root/lastcopy-data
DB=/root/lastcopy-data/m4.db

echo "$(date -Is) [m4] works+authors ingest start" | tee -a "$LOG"
.venv/bin/lastcopy --db $DB ingest-works --data-dir data >> "$LOG" 2>&1 || { echo "WORKS FAILED"; exit 3; }
echo "$(date -Is) [m4] works ingest done" | tee -a "$LOG"

echo "$(date -Is) [m4] editions stream start (9.2G, never stored)" | tee -a "$LOG"
.venv/bin/lastcopy --db $DB ingest-editions --stream-url https://openlibrary.org/data/ol_dump_editions_latest.txt.gz --retries 5 >> "$LOG" 2>&1 || { echo "EDITIONS FAILED"; exit 4; }
echo "$(date -Is) [m4] editions stream done" | tee -a "$LOG"

echo "$(date -Is) [m4] gen-candidates start" | tee -a "$LOG"
.venv/bin/lastcopy --db $DB gen-candidates >> "$LOG" 2>&1 || { echo "CANDIDATES FAILED"; exit 5; }
echo "$(date -Is) [m4] candidates done" | tee -a "$LOG"

echo "$(date -Is) [m4] export top 10k" | tee -a "$LOG"
.venv/bin/lastcopy --db $DB export-list --top 10000 --csv docs/m4-list-top10k.csv --md docs/m4-list.md >> "$LOG" 2>&1 || { echo "EXPORT FAILED"; exit 6; }
echo "$(date -Is) [m4] COMPLETE — counts:" | tee -a "$LOG"
sqlite3 -readonly $DB "SELECT 'editions_ref', count(*) FROM editions_ref UNION ALL SELECT 'works_ref', count(*) FROM works_ref UNION ALL SELECT 'candidates', count(*) FROM candidates;" | tee -a "$LOG"
