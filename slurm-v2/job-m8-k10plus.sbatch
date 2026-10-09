#!/bin/sh
# job-m8-k10plus.sbatch — M8 union-catalog holdings harvest (SPEC-M8).
# K10plus GVK SRU (sru.k10plus.de/gvk, free/keyless, verified live
# 2026-10-09): one pica.isb= query per workset ISBN without a k10plus
# holdings row, <=2 rps sustained, budget-capped (default 50000 = full
# ~50k workset, ~7h at 0.5s/req), resumable (held rows skipped on rerun).
# Then: holdings summary (custody_physical re-derivation now counts
# k10plus as an institution) -> custody-report + union-holdings-report
# (wild->held rescues) -> full export+lists -> atomic write-back.
# Pin lab,lab3 — lab2 is down (2026-10-09).
#SBATCH --export=ALL
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=1-00:00:00
#SBATCH -w lab,lab3
#SBATCH --output=logs/m8_k10plus_%j.out
#SBATCH --job-name=lc-m8

BUNDLE_DIR="${SLURM_SUBMIT_DIR:-/root/projects/lastcopy/slurm-v2}"
cd "$BUNDLE_DIR" || exit 2; mkdir -p "$BUNDLE_DIR/logs"
LC=/root/projects/lastcopy/.venv/bin/lastcopy
SHARED_DB=/root/projects/lastcopy/data/m4.db
DATA=/root/projects/lastcopy/data
BUDGET="${BUDGET:-50000}"

step() { echo "[$(date -Is)] $1"; }
fail() { echo "[$(date -Is)] FAILED rc=$? at: $1"; exit 4; }

step "1/5 scratch-copy"
SCRATCH=$(mktemp -d /var/tmp/lc-m8.XXXXXX); trap 'rm -rf "$SCRATCH"' EXIT
free_gb=$(df -BG --output=avail /var/tmp | tail -1 | tr -dc '0-9')
[ "$free_gb" -lt 5 ] && { echo "refusing: ${free_gb}G free"; exit 3; }
cp "$SHARED_DB" "$SCRATCH/m4.db" || exit 4
DB="$SCRATCH/m4.db"

step "2/5 k10plus SRU harvest (budget $BUDGET, resumable)"
"$LC" --db "$DB" enrich-holdings --institution k10plus --budget "$BUDGET" || fail "sru-k10plus"

step "3/5 custody re-derivation (k10plus counts as an institution)"
"$LC" --db "$DB" assign-holdings-summary || fail summary

step "4/5 reports + export+lists"
cd /root/projects/lastcopy
"$LC" --db "$DB" custody-report || fail custody-report
"$LC" --db "$DB" union-holdings-report || fail union-holdings-report
"$LC" --db "$DB" export-list --top 10000 \
    --csv docs/m4-list-top10k.csv --md docs/m4-list.md \
    --editions-dump "$DATA/ol_dump_editions_latest.txt.gz" --workset || fail export
cd "$BUNDLE_DIR"

step "5/5 write-back (atomic, direct-I/O)"
sync
if ! dd if="$DB" of="$SHARED_DB.new" bs=8M oflag=direct 2>/dev/null; then
    rsync -a --inplace --bwlimit=60000 "$DB" "$SHARED_DB.new" || exit 7
fi
mv "$SHARED_DB.new" "$SHARED_DB" || exit 8
echo "[$(date -Is)] M8 K10PLUS COMPLETE on $(hostname)"
