#!/bin/sh
# job-m8-lobid.sbatch — M8.2 lab import, chained after lc-m8 (k10plus job,
# currently 20502; originally 20150).
# Why chained: lc-m8 stage 5/5 atomically REPLACES data/m4.db from its
# scratch snapshot — any lobid rows folded into live DB before that
# write-back lands would be clobbered. afterok fires only after the
# write-back completed, so this job sees k10plus+lobid.
# Steps: lobid-import (INSERT OR IGNORE holdings + m5.ensure_schema
# self-migration, fix a5c9ac6) -> custody-report + union-holdings-report
# + export refresh so shipped lists include lobid custody effects.
# 20cfc5e class fix: export-list WITHOUT --editions-dump (titles are
# already 50k/50k in candidates.title; M6.1 guard rejects
# --editions-dump + --workset).
#SBATCH --export=ALL
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=02:00:00
#SBATCH -N1 -w lab
#SBATCH --output=logs/m8_lobid_%j.out
#SBATCH --job-name=lc-m8l

BUNDLE_DIR="${SLURM_SUBMIT_DIR:-/root/projects/lastcopy/slurm-v2}"
cd "$BUNDLE_DIR" || exit 2; mkdir -p "$BUNDLE_DIR/logs"
VENV=/root/projects/lastcopy/.venv/bin
SHARED_DB=/root/projects/lastcopy/data/m4.db
DATA=/root/projects/lastcopy/data

step() { echo "[$(date -Is)] $1"; }
fail() { echo "[$(date -Is)] FAILED rc=$? at: $1"; exit 4; }

step "1/3 guard: lc-m8 write-back landed (k10plus rows present)"
K=$("$VENV/python3" -c "import sqlite3; db=sqlite3.connect('file:'+'$SHARED_DB'+'?mode=ro',uri=True); print(db.execute(\"SELECT COUNT(*) FROM holdings WHERE institution='k10plus'\").fetchone()[0])")
echo "k10plus rows in live db: $K"
[ "$K" -gt 0 ] || { echo "refusing: live db has no k10plus rows — lc-m8 write-back missing?"; exit 3; }

step "2/3 lobid-import (7,325 matches, resumable INSERT OR IGNORE)"
"$VENV/lobid-import" --matches "$DATA/scanner/lobid_matches.db" --db "$SHARED_DB" || fail lobid-import

step "3/3 reports + export refresh (lobid-inclusive)"
cd /root/projects/lastcopy
"$VENV/lastcopy" --db "$SHARED_DB" custody-report || fail custody-report
"$VENV/lastcopy" --db "$SHARED_DB" union-holdings-report || fail union-holdings-report
"$VENV/lastcopy" --db "$SHARED_DB" export-list --top 10000 \
    --csv docs/m4-list-top10k.csv --md docs/m4-list.md \
    --workset || fail export
echo "[$(date -Is)] M8 LOBID IMPORT COMPLETE on $(hostname)"
