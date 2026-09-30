#!/bin/bash
# m4-resume.sh — resume M4 after the 2026-09-29 root-disk full stop.
# Phase 1 (works+authors, DONE: 41.6M / 15.4M) is skipped; editions re-stream
# (idempotent upserts over the surviving ~35M rows), then candidates + export.
# DB lives on the volume; /root/lastcopy-data/m4.db symlinks there.
set -uo pipefail
cd /root/projects/lastcopy
export LASTCOPY_FORCE_IPV4=1
LOG=/tmp/lc-m4-real.log
DB=/root/lastcopy-data/m4.db

echo "$(date -Is) [m4-resume] editions re-stream start (56.7M records)" | tee -a "$LOG"
.venv/bin/lastcopy --db $DB ingest-editions --stream-url https://openlibrary.org/data/ol_dump_editions_latest.txt.gz --retries 5 >> "$LOG" 2>&1 || { echo "EDITIONS FAILED"; exit 4; }
echo "$(date -Is) [m4-resume] editions done" | tee -a "$LOG"

echo "$(date -Is) [m4-resume] gen-candidates start" | tee -a "$LOG"
.venv/bin/lastcopy --db $DB gen-candidates >> "$LOG" 2>&1 || { echo "CANDIDATES FAILED"; exit 5; }
echo "$(date -Is) [m4-resume] candidates done" | tee -a "$LOG"

echo "$(date -Is) [m4-resume] export top 10k" | tee -a "$LOG"
.venv/bin/lastcopy --db $DB export-list --top 10000 --csv docs/m4-list-top10k.csv --md docs/m4-list.md >> "$LOG" 2>&1 || { echo "EXPORT FAILED"; exit 6; }
echo "$(date -Is) [m4-resume] COMPLETE — counts:" | tee -a "$LOG"
sqlite3 -readonly $DB "SELECT 'editions_ref', count(*) FROM editions_ref UNION ALL SELECT 'works_ref', count(*) FROM works_ref UNION ALL SELECT 'candidates', count(*) FROM candidates;" | tee -a "$LOG"
df -h /mnt/HC_Volume_105940927 / | tee -a "$LOG"
