#!/bin/bash
# Submit a whole folder in one go: N GPU jobs share the PDFs, then one small CPU job builds the workbook.
# Run from the repository root, on the login node (it only submits jobs):
#
#     bash hpc/itiger/submit.sh PDF_DIR OUT_DIR [N_GPUS] [more tablescrape run options]
#
# N_GPUS defaults to 1. iTiger allows 4 GPUs per user, and other jobs of yours count toward that.
#
# SBATCH_OPTS adds or overrides Slurm options for the GPU job (anything given on the sbatch command line beats the
# #SBATCH lines inside scrape.sbatch). The 64 GB 32B model needs the 80 GB H100 and more host memory:
#
#     SBATCH_OPTS="--gres=gpu:h100_80gb:1 --mem=96G" bash hpc/itiger/submit.sh PDFS OUT 1 --model Qwen/Qwen2.5-VL-32B-Instruct
set -euo pipefail
[ $# -ge 2 ] || { echo "usage: bash hpc/itiger/submit.sh PDF_DIR OUT_DIR [N_GPUS] [options]"; exit 2; }
PDF_DIR=$(realpath "$1"); OUT_DIR=$(realpath -m "$2"); shift 2
N=1
if [ $# -ge 1 ] && [[ $1 =~ ^[0-9]+$ ]]; then N=$1; shift; fi

export TABLESCRAPE_HOME=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
mkdir -p "$TABLESCRAPE_HOME/logs" "$OUT_DIR"
cd "$TABLESCRAPE_HOME"

# $SBATCH_OPTS is deliberately unquoted: it must split into separate words ("--gres=... --mem=...").
if [ "$N" -gt 1 ]; then
    JOB=$(sbatch --parsable --array=0-$((N - 1)) ${SBATCH_OPTS:-} hpc/itiger/scrape.sbatch "$PDF_DIR" "$OUT_DIR" --no-build "$@")
else
    JOB=$(sbatch --parsable ${SBATCH_OPTS:-} hpc/itiger/scrape.sbatch "$PDF_DIR" "$OUT_DIR" --no-build "$@")
fi
# afterany: build even if a GPU job failed or timed out, so whatever was read is in the workbook (and the gaps are in
# its Problems sheet). Rerun this script to fill the gaps; finished pages are skipped.
BUILD=$(sbatch --parsable --dependency=afterany:"$JOB" hpc/itiger/build.sbatch "$OUT_DIR")

echo "GPU job:   $JOB  ($N task(s))   logs: logs/tablescrape-<job id>.log (one per task)"
echo "build job: $BUILD (starts when the GPU job ends)"
echo "watch:     squeue -u $USER"
echo "result:    $OUT_DIR/tables.xlsx"
