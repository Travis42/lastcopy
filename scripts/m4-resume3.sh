#!/bin/bash
# m4-resume3.sh — file-based M4 completion (2026-09-30 morning).
# Lesson from overnight: streams over HTTP/2 to the OL node are fragile; the dump
# is now a verified local file. This gates on gzip -t, then ingests from --file.
set -uo pipefail
cd /root/projects/lastcopy
export LASTCOPY_FORCE_IPV4=1
LOG=/tmp/lc-m4-real.log
DB=/root/lastcopy-data/m4.db
DUMP=data/ol_dump_editions_latest.txt.gz

echo "$(date -Is) [m4-resume3] verifying dump integrity" | tee -a "$LOG"
gzip -t "$DUMP" || { echo "DUMP CORRUPT"; exit 7; }
echo "$(date -Is) [m4-resume3] dump verified — ingest from file (56.7M records)" | tee -a "$LOG"
.venv/bin/lastcopy --db $DB ingest-editions --file "$DUMP" >> "$LOG" 2>&1 || { echo "EDITIONS FAILED"; exit 4; }
echo "$(date -Is) [m4-resume3] editions done" | tee -a "$LOG"

echo "$(date -Is) [m4-resume3] gen-candidates start" | tee -a "$LOG"
.venv/bin/lastcopy --db $DB gen-candidates >> "$LOG" 2>&1 || { echo "CANDIDATES FAILED"; exit 5; }
echo "$(date -Is) [m4-resume3] candidates done" | tee -a "$LOG"

echo "$(date -Is) [m4-resume3] export top 10k" | tee -a "$LOG"
.venv/bin/lastcopy --db $DB export-list --top 10000 --csv docs/m4-list-top10k.csv --md docs/m4-list.md >> "$LOG" 2>&1 || { echo "EXPORT FAILED"; exit 6; }
echo "$(date -Is) [m4-resume3] COMPLETE — counts:" | tee -a "$LOG"
sqlite3 -readonly $DB "SELECT 'editions_ref', count(*) FROM editions_ref UNION ALL SELECT 'works_ref', count(*) FROM works_ref UNION ALL SELECT 'candidates', count(*) FROM candidates;" | tee -a "$LOG"
df -h /mnt/HC_Volume_105940927 / | tee -a "$LOG"
