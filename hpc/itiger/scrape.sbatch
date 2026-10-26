#!/bin/bash
# GPU job: read every page of every PDF in a folder. Submit from the repository root:
#
#     sbatch hpc/itiger/scrape.sbatch PDF_DIR OUT_DIR [more tablescrape run options]
#
# Submitted as an array (sbatch --array=0-3 ...), each task takes a quarter of the PDFs; submit.sh does that and builds
# the workbook afterwards. Rerunning the same command resumes: pages already read are skipped.
#SBATCH --job-name=tablescrape
#SBATCH --partition=bigTiger
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=4
#SBATCH --mem=64G
#SBATCH --time=12:00:00
#SBATCH --output=logs/%x-%j.log

set -euo pipefail
[ $# -ge 2 ] || { echo "usage: sbatch hpc/itiger/scrape.sbatch PDF_DIR OUT_DIR [options]"; exit 2; }
PDF_DIR=$1; OUT_DIR=$2; shift 2

# Slurm runs a copy of this file, so env.sh is found from the directory the job was submitted from.
source "${TABLESCRAPE_HOME:-$SLURM_SUBMIT_DIR}/hpc/itiger/env.sh"
export HF_HUB_OFFLINE=1   # use the weights setup.sh downloaded; never try the network from the compute node

echo "node $(hostname), job ${SLURM_JOB_ID}, array task ${SLURM_ARRAY_TASK_ID:-none}"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv

SHARD=()
if [ -n "${SLURM_ARRAY_TASK_COUNT:-}" ]; then
    SHARD=(--shard "${SLURM_ARRAY_TASK_ID}/${SLURM_ARRAY_TASK_COUNT}")
fi
tablescrape run "$PDF_DIR" -o "$OUT_DIR" "${SHARD[@]}" "$@"
