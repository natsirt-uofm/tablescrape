# Sourced (not run) by the other scripts here:  source hpc/itiger/env.sh
# Keeps everything big under /project (home is limited to 20 GB) and activates the conda environment.

export HF_HOME=${HF_HOME:-/project/$USER/hf-cache}                 # model weights; the 8B model is about 17 GB
export CONDA_PKGS_DIRS=${CONDA_PKGS_DIRS:-/project/$USER/conda-pkgs} # conda's download cache (default is in home)
export PIP_CACHE_DIR=${PIP_CACHE_DIR:-/project/$USER/pip-cache}      # pip's download cache (default is ~/.cache)
export TABLESCRAPE_ENV=${TABLESCRAPE_ENV:-/project/$USER/conda-envs/tablescrape}  # the environment itself
mkdir -p "$HF_HOME" "$CONDA_PKGS_DIRS" "$PIP_CACHE_DIR"

# `conda activate` is a shell function that batch jobs do not get automatically; this defines it.
# conda's own scripts read variables that may be unset, which `set -u` in the calling script would treat as an error.
_was_u=$-; set +u
source "$(conda info --base)/etc/profile.d/conda.sh"
if [ -d "$TABLESCRAPE_ENV" ]; then
    conda activate "$TABLESCRAPE_ENV"
    export PYTHONNOUSERSITE=1   # packages in ~/.local must not override the pinned ones in the environment
fi
[[ $_was_u == *u* ]] && set -u; unset _was_u
