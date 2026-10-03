#!/bin/sh
# job-m6-rescue.sbatch — M6 digital-rescue stages (SPEC-M6-RESCUE).
# STAGE=num  : OAI-NUM harvest  (oai.bnf.fr set gallica, ~35h sequential,
#              checkpointed per page; resumable — rerun continues from ckpt)
# STAGE=cat  : OAI-CAT harvest  (catoai.bnf.fr set catalogue:edition:livres)
# STAGE=join : parse-gallica offline ark-chain join + assign-status reclass
# Browser UA is MANDATORY for BnF (default UA -> 403); <=1 rps in-code.
#SBATCH --export=ALL
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=14-00:00:00
#SBATCH --output=logs/m6_%j.out
#SBATCH --job-name=lc-m6

BUNDLE_DIR="${SLURM_SUBMIT_DIR:-/root/projects/lastcopy/slurm-v2}"
cd "$BUNDLE_DIR" || exit 2
mkdir -p "$BUNDLE_DIR/logs"
. /root/projects/lastcopy/slurm-ia/env.sh

LC=/root/projects/lastcopy/.venv/bin/lastcopy
DATA=/root/projects/lastcopy/data/gallica
STAGE="${STAGE:-num}"
HARVEST_ARGS="${HARVEST_ARGS:-}"   # e.g. "--max-pages 5" canary runs

case "$STAGE" in
num|cat)
    echo "[$(date -Is)] harvest stage=$STAGE $HARVEST_ARGS"
    "$LC" fetch-gallica --stage "$STAGE" --data-dir "$DATA" $HARVEST_ARGS
    rc=$?
    # incomplete-but-checkpointed exits 1 only when not max-pages-capped;
    # resubmit this job to resume from the checkpoint token
    [ $rc -eq 0 ] && exit 0
    [ $rc -eq 1 ] && { echo "checkpointed, incomplete — resubmit to resume"; exit 0; }
    exit $rc
    ;;
join)
    SHARED_DB=/root/projects/lastcopy/data/m4.db
    SCRATCH=$(mktemp -d /var/tmp/lc-m6.XXXXXX)
    trap 'rm -rf "$SCRATCH"' EXIT
    cp "$SHARED_DB" "$SCRATCH/m4.db" || exit 4
    DB="$SCRATCH/m4.db"
    echo "[$(date -Is)] gallica join + status reclass"
    "$LC" --db "$DB" parse-gallica \
        --num-file "$DATA/num_pairs.tsv.gz" \
        --cat-file "$DATA/cat_pairs.tsv.gz" || exit 5
    "$LC" --db "$DB" assign-status || exit 6
    sync
    if ! dd if="$DB" of="$SHARED_DB.new" bs=8M oflag=direct 2>/dev/null; then
        rsync -a --inplace --bwlimit=60000 "$DB" "$SHARED_DB.new" || exit 7
    fi
    mv "$SHARED_DB.new" "$SHARED_DB" || exit 8
    echo "[$(date -Is)] join DONE on $(hostname)"
    ;;
*)
    echo "unknown STAGE=$STAGE (want num|cat|join)" >&2; exit 2;;
esac
