#!/bin/sh
# job-gb-trickle.sbatch — daily Google Books verification trickle (M5.5).
# Runs via slurm scrontab. Budget 1000/day (GB default quota is 1k req/day —
# increases are frozen per Google, 2026-09-30). Node-agnostic but NEVER
# wsl-gpu (home IP — Theory directive: don't burn home rate-limit bucket).
# SQLite policy: local-scratch copy, atomic copy-back (single writer).
#SBATCH --export=ALL
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --output=logs/gb_%j.out
#SBATCH --job-name=lc-gb

BUNDLE_DIR="${SLURM_SUBMIT_DIR:-/root/projects/lastcopy/slurm-v2}"
cd "$BUNDLE_DIR" || exit 2
mkdir -p "$BUNDLE_DIR/logs"

SHARED_DB=/root/projects/lastcopy/data/m4.db
SCRATCH=$(mktemp -d /var/tmp/lc-gb.XXXXXX)
# keep scratch on failure so harvested rows survive for salvage/resume (20cfc5e class fix)
gb_ok=0
trap 'if [ "$gb_ok" = 1 ]; then rm -rf "$SCRATCH"; else echo "[trap] FAILURE - keeping scratch for salvage: $SCRATCH"; fi' EXIT

free_gb=$(df -BG --output=avail /var/tmp | tail -1 | tr -dc '0-9')
[ "$free_gb" -lt 15 ] && { echo "refusing: only ${free_gb}G free"; exit 3; }

cp "$SHARED_DB" "$SCRATCH/m4.db" || exit 4

echo "[$(date -Is)] gb-trickle budget=1000 on $(hostname)"
/root/projects/lastcopy/.venv/bin/lastcopy --db "$SCRATCH/m4.db" \
    gb-trickle --budget 1000 || exit 5

sync
if ! dd if="$SCRATCH/m4.db" of="$SHARED_DB.new" bs=8M oflag=direct 2>/dev/null; then
    rsync -a --inplace --bwlimit=60000 "$SCRATCH/m4.db" "$SHARED_DB.new" || exit 7
fi
mv "$SHARED_DB.new" "$SHARED_DB" || exit 8
gb_ok=1
echo "[$(date -Is)] DONE on $(hostname)"
