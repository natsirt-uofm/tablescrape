# tablescrape

Give it a folder of PDFs. It finds **every table on every page** and copies them into:

- **`tables.xlsx`**: an *Index* sheet listing every table (source PDF, page, printed label such as "Table 2",
  caption, size, warnings), linked to **one sheet per table**; a *Documents* sheet with one row per PDF, including
  those with no tables (status: `3 table(s)`, `no tables found`, `not finished: ...`, `cannot open: ...`); and a
  *Problems* sheet for anything that went wrong.
- **`csv/<pdf>/<table id>.csv`**: each table as a plain CSV (header row, then data, cells exactly as read).
- **`index.csv`**: the Index sheet as CSV.

It reads the pages with an open vision-language model (**Qwen3-VL-8B-Instruct**, Apache-2.0) running on your own
GPU. No API key, no data leaves the cluster.

## How it works

```
PDF folder ──> each page: an image (pypdfium2, 200 dpi) + the PDF's own text layer, if it has one
           ──> Qwen3-VL gets both: layout from the image, exact characters from the text; replies with JSON
           ──> the reply is made rectangular; every cell is looked up in the page's text layer
           ──> saved: OUT/pages/<pdf>/p0001.json (+ the page .png)
           ──> build: all saved pages ──> tables.xlsx, csv/, index.csv, documents.csv
```

Why both: on the first real test the model, reading the image alone, changed single letters in DNA primer sequences
(`ATCGACTTGTTCTTTG...` came out as `ATCGACTTGTTCCTTG...`). The text stored in a born-digital PDF is exact, so the
model is told to copy characters from it, and afterwards every cell is checked against it. Cells that are not found
are **highlighted yellow** in the workbook, and the Index counts them (`cells_to_check`). A page that is only a picture
has no text layer; its tables show `cells_to_check = all (no text layer)`.

Every page is saved as soon as it is read, so a job that hits its time limit loses at most one page: **submit the
same command again and it carries on**. Pages without tables cost a few seconds; a dense table can take a minute or
two, because the model has to write out every cell. Measured on iTiger (RTX 5000 Ada): about 1 s for a page without
tables, 5-100 s for a page with one. Loading the model takes about 15 minutes per job (the weights are read from the
network file system), so prefer one job over many small ones.

Table IDs look like `smith2019_p004_t1`: PDF name, page 4, first table on that page. The printed label ("Table 2")
and caption are separate columns, because many tables have no number and some numbers repeat (Table 1 in two papers).

## Using it on iTiger

**The rule from the iTiger admins:** nothing compute-heavy on the login node (`itiger.memphis.edu`). The login node
is for installing, cloning, copying files and submitting jobs. Everything that loads the model goes through Slurm (`sbatch`
or `srun`). `tablescrape run` itself refuses to start when it sees no GPU, so it will not accidentally run there.

Below, `<you>` stands for your own iTiger user name.

### 0. Clone the repository onto the cluster

On the login node (connect with `ssh <you>@itiger.memphis.edu`):

```bash
cd /project/<you>
git clone git@github.com:natsirt-uofm/tablescrape.git tablescrape
```

- `cd /project/<you>` moves to your project folder. Work under `/project/<you>`, not your home folder: home is
  limited to 20 GB, and the model alone is 17 GB or more.
- `git clone git@github.com:natsirt-uofm/tablescrape.git tablescrape` copies the repository. `git@github.com:natsirt-uofm/tablescrape.git` is the repository's address (SSH form; the HTTPS form is on the
  green **Code** button); the last word, `tablescrape`, is the name of the folder to create.
- **Produces**: `/project/<you>/tablescrape/` with all the code and scripts. PDFs and results are deliberately not in the
  repository.
- **If the repository is private**, the login node needs your GitHub access. Either add an SSH key (`ssh-keygen -t ed25519`
  on the cluster, then paste the contents of `~/.ssh/id_ed25519.pub` into GitHub, Settings, SSH keys, and clone with the
  `git@github.com:...` address), or clone the `https://` address and use a personal access token as the password.
- **Later, to get updates**: `cd /project/<you>/tablescrape && git pull`. `git pull` downloads new commits and merges
  them in. Run `setup.sh` again only if `hpc/itiger/environment.yml` changed.

### 1. One-time setup (login node, about 30 minutes, mostly downloading)

```bash
cd /project/<you>/tablescrape
bash hpc/itiger/setup.sh
```

- `cd` moves into the repository; every script here expects to be started from its root.
- `bash hpc/itiger/setup.sh` runs the setup script with bash.
- **Produces**: a conda environment in `/project/<you>/conda-envs/tablescrape` (Python 3.11, PyTorch 2.6 built
  for CUDA 12.4, transformers 4.57.6), and the model weights in `/project/<you>/hf-cache/`. Both deliberately
  live under `/project`: on iTiger Miniconda usually sits in home, and a default environment (about 8 GB) would land
  in the 20 GB home quota. This is installing and downloading, which the admins
  allow on the login node. Nothing runs the model.
- Variation: `bash hpc/itiger/setup.sh Qwen/Qwen2.5-VL-7B-Instruct` downloads the older model instead (then pass
  `--model Qwen/Qwen2.5-VL-7B-Instruct` to runs).

Why PyTorch 2.6 / CUDA 12.4: the iTiger docs warn that the default (newer) PyTorch needs a newer NVIDIA driver than
the cluster has, and then quietly runs on the CPU, which is about 100x slower. The setup script ends by printing
`built for CUDA 12.4`. If it prints something else, the pins in `hpc/itiger/environment.yml` were overridden.

### 2. Quick test on a GPU node (do this once before a big run)

Put two or three PDFs in a folder, e.g. `/project/<you>/test_pdfs/`, then ask Slurm for an interactive GPU
session:

```bash
srun --pty --partition=bigTiger --gres=gpu:1 --cpus-per-task=4 --mem=64G --time=00:30:00 bash
```

- `srun` asks Slurm for resources and runs a command on a compute node, here an interactive shell.
- `--pty` attaches your terminal to it, so you can type commands.
- `--partition=bigTiger` is the queue (group of nodes) to run on; this is the GPU partition the iTiger tutorials use.
- `--gres=gpu:1` ("generic resource") asks for one GPU.
- `--cpus-per-task=4` asks for 4 CPU cores. `--mem=64G` asks for 64 GB of RAM.
- `--time=00:30:00` is the wall-time limit (HH:MM:SS). The session is killed after this, so keep tests short.
- `bash` is the command to run on the node: a shell.
- **Produces**: a prompt on a compute node (e.g. `[<you>@itiger06 ...]`). It may wait in the queue first.

Then, inside that session:

```bash
cd /project/<you>/tablescrape
source hpc/itiger/env.sh
nvidia-smi
tablescrape list /project/<you>/test_pdfs
tablescrape run /project/<you>/test_pdfs -o /project/<you>/test_out --max-pages 3
exit
```

- `source hpc/itiger/env.sh` runs the file *in your current shell* (unlike `bash file`, which runs in a child
  shell), so the conda environment it activates and the `HF_HOME` variable it sets stay active for the next commands.
- `nvidia-smi` shows the GPU you were given and its memory. If it errors, you are not on a GPU node.
- `tablescrape list DIR` counts PDFs and pages (it does not load the model). Use it to estimate run time.
- `tablescrape run DIR -o OUT` reads the pages. `-o` (`--output`) is the output folder. `--max-pages 3` reads only the
  first 3 pages of each PDF, for a fast check.
- `exit` ends the session and gives the GPU back. Do not leave idle sessions holding a GPU.
- **Produces**: `/project/<you>/test_out/tables.xlsx`, `csv/`, `index.csv`, `pages/`. Each page prints its time,
  e.g. `p2: 1 table(s), 48.3s`. Note those times: they tell you how long a full run needs.

Copy the workbook to your computer to look at it (run this **on your own machine**, not the cluster):

```bash
scp <you>@itiger.memphis.edu:/project/<you>/test_out/tables.xlsx .
```

- `scp` copies a file over SSH. The source is `user@host:path`. The destination `.` is your current folder.

### 3. A whole folder (batch jobs)

```bash
cd /project/<you>/tablescrape
bash hpc/itiger/submit.sh /project/<you>/papers /project/<you>/papers_tables 2
```

- `submit.sh PDF_DIR OUT_DIR N_GPUS`: submits a GPU job split into `N_GPUS` parts (here 2; each part takes every
  second PDF), and a small CPU job that builds `tables.xlsx` once they have all ended. iTiger allows 4 GPUs per user.
- Anything after `N_GPUS` is passed to `tablescrape run`, e.g. `... 2 --max-new-tokens 12000`.
- **Produces**: two job numbers printed on screen, logs in `logs/`, and the results in `OUT_DIR`. You can log out;
  the jobs keep running.

What `submit.sh` does, in case you want to submit by hand:

```bash
mkdir -p logs
sbatch --array=0-1 hpc/itiger/scrape.sbatch /project/<you>/papers /project/<you>/papers_tables --no-build
sbatch --dependency=afterany:123456 hpc/itiger/build.sbatch /project/<you>/papers_tables
```

- `mkdir -p logs`: the job logs go to `logs/`. Slurm does not create that folder, and without it the log is lost.
- `sbatch FILE ARGS` queues a batch job. The `#SBATCH` lines at the top of `scrape.sbatch` hold its settings
  (partition, 1 GPU, 4 CPUs, 64 GB, 12 hours). Flags given on the command line override them.
- `--array=0-1` runs the script as two tasks numbered 0 and 1. The script turns that into
  `--shard 0/2` / `--shard 1/2`, so each task reads a different half of the PDFs.
- `--no-build` skips building the workbook inside each task. Two tasks writing one file at once would clash.
- `--dependency=afterany:123456` holds the build job until job 123456 (the number the first `sbatch` printed) has
  ended, whether it succeeded or not. That way whatever was read gets into the workbook, and the gaps are listed.

Watching and managing jobs:

```bash
squeue -u $USER
tail -f logs/tablescrape-123456.log
scancel 123456
```

- `squeue -u $USER` lists your jobs. `-u` filters by user, and `$USER` is a shell variable holding your user name.
  The `ST` column shows `PD` (pending, waiting for a GPU) or `R` (running).
- `tail -f FILE` prints a log and keeps printing new lines as they arrive (`-f` = follow). Ctrl+C stops watching,
  not the job.
- `scancel JOBID` cancels a job. Safe here: every page read so far is kept.

**If a job runs out of time**, run the same `submit.sh` command again: finished pages are skipped. Pages that failed
or were cut off are listed in the *Problems* sheet. Add `--retry-errors` to read only those again.

## Checking the results

A vision model reads like a fast, tireless, occasionally careless typist. It is usually right, but it can misread a
digit, merge two columns, or (rarely) write a plausible value that is not on the page. Before you use the numbers:

1. In the *Index* sheet, look at `cells_to_check`, the `warnings` column, and the *Problems* sheet first.
2. Compare the yellow cells with the page image, `pages/<pdf>/p0004.png`, which shows exactly what the model saw.
   Also look at the table's *shape*: a cell can be read correctly and still land in the wrong row or column, and the
   text check cannot see that.
3. Figures the model mistook for tables (phylogenies, diagrams; anything labeled "Fig.") are left out and named in
   the *Problems* sheet.
4. A table split across pages comes out as separate tables. The later parts have `continued = yes`. Join them
   yourself if needed.

In the workbook, plain numbers (`12`, `81.50`) are stored as real Excel numbers, keeping their printed decimals.
Anything else stays text exactly as printed: `007`, `−3.2` with a typographic minus, `12 ± 3`, `<0.001`. The CSV
files are always text exactly as read. `--no-numbers` (on `run` or `build`) keeps the workbook as text too.

## Options (`tablescrape run --help`)

| option | default | when to change it |
|---|---|---|
| `--model` | `Qwen/Qwen3-VL-8B-Instruct` | a Hugging Face id (downloaded by `setup.sh`) or a local folder |
| `--dpi` | 200 | 250–300 for tiny print; the page is still capped by `--max-pixels` |
| `--max-pixels` | 3,200,000 | raise for dense pages with small fonts (slower, more GPU memory) |
| `--max-new-tokens` | 8192 | raise if the Problems sheet says replies were cut off (very long tables) |
| `--max-pages N` | all | first N pages of each PDF, for tests |
| `--retry-errors` | off | re-read only pages that failed or were cut off |
| `--shard I/N` | 0/1 | set for you by the array job |
| `--no-build` | off | skip building the workbook at the end |

`tablescrape build OUT` rebuilds the workbook and CSVs from what is saved, without the GPU. It is cheap, but on
iTiger run it through `build.sbatch` anyway.

## Running the tests (any machine, no GPU)

```bash
python -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/pytest
```

- `python -m venv .venv` creates an isolated Python environment in `.venv/` (`-m venv` runs Python's built-in
  `venv` module).
- `pip install -e ".[test]"` installs this package in *editable* mode (`-e`: code changes take effect without
  reinstalling), plus the `test` extras (pytest).
- `pytest` finds and runs `tests/`. The tests use a stand-in for the model, so they check everything except the
  model's reading itself.

## Layout

```
src/tablescrape/
  cli.py        the tablescrape command (list, run, build)
  pdfpages.py   find PDFs, render pages to images
  vlm.py        the prompt and the Qwen-VL model wrapper (the only part that needs a GPU)
  parse.py      model reply -> clean rectangular tables; figures told apart from tables
  textcheck.py  every cell looked up in the PDF's text layer
  pipeline.py   read every page once, save each result, resume
  export.py     saved results -> tables.xlsx, csv/, index.csv
hpc/itiger/     environment.yml, setup.sh, env.sh, scrape.sbatch, build.sbatch, submit.sh
tests/
```

## Status

The pipeline around the model (rendering, parsing, resuming, sharding, workbook and CSV output) is tested here.
**The model call has not yet been run on iTiger.** It was written against the transformers 4.57.6 source, and the
first run in step 2 is the real test of it. If something fails there, the job log has the full error.
