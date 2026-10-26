#!/bin/bash
# One-time setup. Run from the repository root, on the login node:
#
#     bash hpc/itiger/setup.sh [MODEL_ID]
#
# Login-node work only: it installs packages and downloads the model weights (file transfer, which the iTiger admins
# allow there). It does not load the model or run anything on it; that happens only in Slurm jobs.
set -euo pipefail
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
MODEL=${1:-Qwen/Qwen3-VL-8B-Instruct}

source "$REPO/hpc/itiger/env.sh"   # sets the /project paths (activates the environment if it already exists)
# -p (--prefix) puts the environment in a folder of our choosing under /project instead of inside Miniconda,
# which on iTiger usually lives in the 20 GB home.
if [ -d "$TABLESCRAPE_ENV" ]; then
    conda env update -p "$TABLESCRAPE_ENV" -f "$REPO/hpc/itiger/environment.yml"
else
    conda env create -p "$TABLESCRAPE_ENV" -f "$REPO/hpc/itiger/environment.yml"
fi

source "$REPO/hpc/itiger/env.sh"   # again, now that the environment exists, to activate it
pip install --no-deps -e "$REPO"   # the tablescrape command itself; its dependencies came from environment.yml

# Download the weights now so the GPU jobs never wait on (or need) the internet.
python - "$MODEL" <<'PY'
import sys
from huggingface_hub import snapshot_download
path = snapshot_download(sys.argv[1])
print("model downloaded to", path)
PY

python -c "import torch, transformers; print('torch', torch.__version__, '| built for CUDA', torch.version.cuda, '| transformers', transformers.__version__)"
echo "Setup done. Next: a short test on a GPU node (see README, step 2)."
