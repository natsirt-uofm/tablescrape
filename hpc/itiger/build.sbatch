#!/bin/bash
# CPU job (no GPU): build tables.xlsx, the CSVs and index.csv from the pages read so far.
#
#     sbatch hpc/itiger/build.sbatch OUT_DIR
#SBATCH --job-name=tablescrape-build
#SBATCH --partition=bigTiger
#SBATCH --cpus-per-task=1
#SBATCH --mem=8G
#SBATCH --time=00:30:00
#SBATCH --output=logs/%x-%j.log

set -euo pipefail
[ $# -ge 1 ] || { echo "usage: sbatch hpc/itiger/build.sbatch OUT_DIR"; exit 2; }
source "${TABLESCRAPE_HOME:-$SLURM_SUBMIT_DIR}/hpc/itiger/env.sh"
tablescrape build "$@"
