# tablescrape: team guide

Copy every table in a folder of PDFs into a labeled Excel workbook and CSV files, using an open vision-language
model that runs on the iTiger GPUs. No API key, no data leaves the cluster.

This guide has four parts:

1. [The one-page summary](#1-the-one-page-summary) (for the presentation)
2. [How it works](#2-how-it-works) (the idea, then every file in this directory)
3. [How submitting a job works](#3-how-submitting-a-job-works) (Slurm, and what each script does)
4. [Instructions](#4-instructions-for-a-new-team-member) (step by step, every command explained), then
   [reading the results](#5-reading-the-results), [**can we trust the results, and what the team must do**](#6-can-we-trust-these-results-flaws-and-what-the-team-must-do),
   [measured performance and limits](#7-what-was-measured-and-what-is-not-solved), and
   [troubleshooting](#8-troubleshooting)

---

## 1. The one-page summary

**What it does.** You give it a folder of PDFs. It returns:

- `tables.xlsx`: an *Index* sheet (one row per table: PDF, page, printed label such as "Table 2", caption, size,
  warnings), a *Documents* sheet (one row per PDF, including PDFs with no tables), **one sheet per table**, and a
  *Problems* sheet;
- `csv/`: each table as its own CSV file;
- `pages/`: the page image the model saw for every table it found, so any table can be checked by eye.

**Why this exists.** The team needs tables pulled out of many PDFs without depending on paid API keys. This does the
job on the university cluster, with open models that anyone with an iTiger account can run.

**How it works in one sentence.** Each PDF page is turned into an image, a vision model (Qwen2.5-VL or Qwen3-VL)
reads the image and writes every table as structured text, the exact characters are checked against the text stored
inside the PDF, and the results are written to Excel and CSV.

**The rule from the iTiger admins.** Nothing compute-heavy on the login node (`itiger.memphis.edu`). The login node is
for installing, copying files and submitting jobs. The model only ever runs inside a Slurm job on a GPU node. The tool
itself refuses to load the model if it cannot see a GPU.

**How well it works** (measured, see [section 7](#7-what-was-measured-and-what-is-not-solved)):

| test | result |
|---|---|
| 17 PDFs, 126 pages, compared against an earlier extracted primer table (the reference), 32B model on one H100 | **318 of 323 primers present** (281 letter-for-letter identical, 37 identical but with tag letters the paper prints in front). 54 minutes, 0 errors. |
| Tables pasted into a PDF as a **picture** (no text stored in the PDF) | **Not reliable.** One test table: 10 of 26 primers exactly right. The tool flags these cells in yellow. |

**Can we trust it?** As a fast, checkable first draft, yes. As a finished dataset, no. Details and the required checks
are in [section 6](#6-can-we-trust-these-results-flaws-and-what-the-team-must-do). The short version: values it could
confirm against the PDF's own text are very likely right; anything flagged, anything from a table pasted as a picture,
and anything you will act on (ordering a primer, reporting a number) must be checked against the paper by a person.

**What it does not do.** It copies each table as printed. It does not match papers to a locus list, resolve aliases
or choose between papers. That is a separate step and is not part of this tool.

**Speed.** About 1.5 seconds for a page with no table, 30 to 75 seconds for a page with one. A 126-page set took 54
minutes on one GPU. This is slower than we want; the plan to make it faster is in section 7.

---

## 2. How it works

### 2.1 The idea

```
PDF folder
   |  for every page of every PDF:
   |    1. render the page to an image            (pdfpages.py)
   |    2. take the text stored inside the PDF    (pdfpages.py)
   |    3. ask the vision model to copy the tables, giving it both   (vlm.py)
   |    4. turn the model's reply into clean tables                  (parse.py)
   |    5. look every cell up in the stored text                     (textcheck.py)
   |    6. save the page's result to disk immediately                (pipeline.py)
   v
OUT/pages/<pdf>/p0001.json ...        <- one small file per page: the checkpoint
   |  then, with no GPU needed:
   v
tables.xlsx, csv/, index.csv, documents.csv                          (export.py)
```

Two ideas carry the design.

**Idea 1: give the model both the picture and the text.** A vision model that reads only an image makes typist's
mistakes: in our first test it changed single letters inside DNA primer sequences. Most PDFs also store their text
(you can select and copy it), and that stored text is exact. The model gets both: the *image* tells it the layout (which
cell is in which row and column), the *stored text* gives it exact characters. Afterwards every cell it wrote is looked
up in the stored text, and cells that are not found are highlighted yellow.

**Idea 2: save every page as it is read.** A job has a time limit. Each page's result is written to its own file the
moment it is done, so a job that is cancelled or runs out of time loses at most one page. Running the same command
again skips pages that already have a file. Building the Excel file is a separate, cheap step that reads those files.

### 2.2 Every file in this directory

```
VLM_tablescrapper/
  pyproject.toml            package definition and the `tablescrape` command
  README.md                 shorter usage notes
  TEAM_GUIDE.md             this file
  .gitignore                what git should not track
  src/tablescrape/          the program
    __init__.py               version number
    __main__.py               lets you run `python -m tablescrape`
    cli.py                    the command-line interface (list / run / build)
    pdfpages.py               find PDFs, render pages, read the stored text
    vlm.py                    the prompt and the model wrapper (the only file that needs a GPU)
    parse.py                  model reply -> clean rectangular tables
    textcheck.py              check cells against the PDF's stored text
    pipeline.py               the loop over PDFs and pages, saving and resuming
    export.py                 saved pages -> Excel workbook and CSVs
  hpc/itiger/               everything specific to the cluster
    environment.yml           the exact software versions to install
    env.sh                    sets paths and activates the software (sourced by the others)
    setup.sh                  one-time install + model download (login node)
    scrape.sbatch             the GPU job
    build.sbatch              the CPU job that builds the workbook
    submit.sh                 submits a GPU job and the build job together
  tests/                    automatic checks (no GPU needed)
    test_parse.py, test_textcheck.py, test_pipeline.py
  logs/                     Slurm job logs are written here (the folder must exist)
  results/                  local copies of test results (not tracked by git)
  test_pdfs/                the PDFs used for testing (not tracked by git)
```

Generated folders you can ignore: `src/tablescrape.egg-info/` (created by `pip install -e`), `__pycache__/`,
`.pytest_cache/`.

#### `pyproject.toml`

Python's package description. It names the package (`tablescrape`), lists the libraries it needs (`pypdfium2` to render
PDFs, `Pillow` for images, `openpyxl` for Excel, `json-repair` to rescue cut-off model replies), and defines one
command: `tablescrape = "tablescrape.cli:main"`, which means "the command `tablescrape` runs the function `main` in
`cli.py`". The `gpu` extra (torch, transformers, accelerate) is there for convenience on other machines. On iTiger
those libraries come from `environment.yml` with exact versions instead, because the cluster's GPU driver needs a
specific PyTorch build.

#### `src/tablescrape/cli.py`: the commands

Three commands:

- `tablescrape list PDF_DIR` counts PDFs and pages. Cheap, no model, fine on the login node.
- `tablescrape run PDF_DIR -o OUT` reads every page with the model, then builds the outputs. **Refuses to run (exit
  code 3) if no GPU is visible**, which is what keeps it off the login node. `--allow-cpu` overrides this; do not use
  it on the login node.
- `tablescrape build OUT` rebuilds the workbook and CSVs from the pages already read. No GPU.

`run` options: `--model`, `--dpi`, `--max-pixels`, `--max-new-tokens`, `--shard I/N`, `--max-pages`, `--retry-errors`,
`--no-build`, `--no-numbers`, `--allow-cpu` (see `tablescrape run --help`; the table in section 4.6 explains them).

#### `src/tablescrape/pdfpages.py`: from PDF to images and text

- `find_pdfs(root)` finds every `.pdf` under a folder, at any depth, any capitalisation of the extension, in a stable
  sorted order.
- `pdf_key(pdf, root)` makes a safe, unique name for a PDF from its path (spaces become `_`, folders joined with
  `__`). This names the output folders, so `smith 2019.pdf` becomes `smith_2019`.
- `render_pages(...)` renders each page to an RGB image at the chosen DPI, shrinks it if it exceeds `max_pixels` (the
  model's cost grows with pixel count), and also returns the text stored in that page.

#### `src/tablescrape/vlm.py`: the model

The only file that needs a GPU. It holds:

- **`PROMPT`**: the instructions given to the model. In plain words: find every table, copy every cell exactly as
  printed, answer only in a fixed JSON shape (label, caption, columns, rows, footnotes, continued), join multi-level
  headers with ` | `, leave `columns` empty if the page prints no header, and do not treat figures, trees or diagrams
  as tables. Changing the prompt changes the behaviour of the whole tool; test any change.
- **`TEXT_HINT` and `build_prompt()`**: adds the page's stored text to the prompt when the page has some, with the
  instruction "copy characters from this text, take the layout from the image".
- **`QwenVLReader`**: loads the model once (`bfloat16`, spread over the GPU automatically), then `read(image, text)`
  returns the reply. Decoding is **greedy** (`do_sample=False`), so the same page gives the same answer every run. It
  also reports whether the reply hit the token limit (`truncated`).

#### `src/tablescrape/parse.py`: from the model's reply to clean tables

The model's reply is text that should be JSON. This file makes it dependable:

- strips a ```` ```json ```` fence, accepts a bare list or an empty `{}` (a page with no tables);
- keeps numbers exactly as written (`1.10` stays `1.10`, it does not become `1.1`);
- repairs a reply that was **cut off** at the token limit (closes the brackets so the complete rows are kept);
- pads short rows so every row is as wide as the table, and records a warning when it had to;
- names blank or missing header cells `column_N`;
- warns when a table has a header but **no data rows** (the model probably wrote the whole table into the header);
- `is_figure()` recognises a "table" that is really a figure (label or caption starting with "Fig." or "Figure").

#### `src/tablescrape/textcheck.py`: the accuracy check

Looks up every body cell in the page's stored text. Two flags result:

- **not found** (yellow in the workbook): the cell's characters appear nowhere in the stored text, so the model
  probably misread them. Spaces are ignored (the stored text breaks cells across lines) and look-alike characters
  count as equal (ligatures, the different dash characters).
- **clipped** (orange, with the full value in a cell comment): the cell is only the *start* of a longer word in the
  stored text. This happens when a page is cut off at the edge (a web page printed to PDF can chop the right-hand
  column) but the PDF still stores the full value.

If a page has no stored text (a scanned page, or a table pasted as a picture) nothing can be checked, and the Index
says `all (no text layer)`.

#### `src/tablescrape/pipeline.py`: the loop, and the checkpoints

`extract()` is the main loop. For each PDF it writes `document.json` (name, page count), then for each page not
already done it renders, asks the model, parses, checks, and writes `pNNNN.json` (plus `pNNNN.png`, the page image, if
tables were found). Details that matter:

- **Resume:** a page with a `.json` file is skipped. `--retry-errors` re-reads only pages that failed or were cut off.
- **Safe writes:** a file is written under a temporary name then renamed, so a job killed mid-write never leaves a
  half-written file.
- **Failures are recorded, not fatal:** an unreadable reply or an out-of-memory error on one page is saved with its
  error text, and the loop moves on to the next page.
- **Figures** the model read as tables are left out but named in the page file, and show up in the *Problems* sheet.
- **Sharding:** `--shard I/N` makes this process take every N-th PDF starting at I, so N GPU jobs can split a folder
  without overlap.

#### `src/tablescrape/export.py`: the Excel and CSV files

Reads all the saved page files and writes:

- `tables.xlsx`: *Index*, *Documents*, one sheet per table (with the source, caption, footnotes and warnings above the
  table), and *Problems*. Sheet names are capped at 31 characters (an Excel rule) and made unique with a running number.
- `csv/<pdf>/<table id>.csv`: each table alone, header row then data. Saved as UTF-8 with a byte-order mark so Excel
  shows symbols like ± and µ correctly.
- `index.csv` and `documents.csv`: the two summary sheets as CSV.

Choices worth knowing: a plain number like `81.50` is stored as a real Excel number keeping its printed decimals;
anything else (`007`, `12 ± 3`, `<0.001`) stays text exactly as printed; a cell beginning with `=` is stored as text so
Excel never runs it as a formula. The CSVs are always plain text exactly as read. `--no-numbers` makes the workbook
all text as well.

#### `hpc/itiger/` and `tests/`

The `hpc/itiger/` scripts are explained in [section 3](#3-how-submitting-a-job-works). The `tests/` run in seconds on
any computer without a GPU, using a stand-in for the model. They cover reply parsing, the stored-text check, and a full
run on generated PDFs (resuming, sharding, failures, the workbook contents). They do **not** test the model's reading
itself; that was measured on the cluster (section 7). Run them with `pytest` (section 4.8).

---

## 3. How submitting a job works

### 3.1 The cluster in four ideas

- **Login node vs compute nodes.** You connect to `itiger.memphis.edu`, the *login node*. It is shared by everyone,
  so it is only for light work. The GPUs are on separate *compute nodes* you never log into directly; you ask for one.
- **Slurm** is the program that hands out compute nodes. You describe what you need (1 GPU, 64 GB of memory, 4 hours)
  and what to run. Slurm queues the request and starts it when resources are free.
- **`sbatch`** submits a script to run later, unattended. You can log out; the job keeps running. **`srun`** runs
  something now, interactively, once resources are free. Both go through Slurm.
- **Partition and GRES.** A *partition* is a group of nodes (`bigTiger` is the GPU partition). *GRES* ("generic
  resource") is how you ask for GPUs: `--gres=gpu:1` means any one GPU, `--gres=gpu:h100_80gb:1` means one H100.

iTiger's GPUs seen in our runs: RTX 5000 Ada (32 GB), RTX 6000 Ada (48 GB), H100 (80 GB). Per the iTiger docs your
quotas are 4 GPUs, 20 CPU cores, 20 GB home and 200 GB of project storage.

### 3.2 What happens when you submit

```
you run:   bash hpc/itiger/submit.sh PDFS OUT 1 [options]          (login node, takes a second)
             |
             |--> sbatch scrape.sbatch ...      queued in Slurm as the "GPU job"
             |--> sbatch --dependency=afterany:<GPU job> build.sbatch ...
             |                                  queued, but held until the GPU job ends (any way it ends)
             v
 Slurm finds a free GPU node and starts the GPU job there:
     scrape.sbatch:  source env.sh (activate the software)
                     tablescrape run PDFS -o OUT --no-build ...    <- loads the model, reads every page
             |
 when it ends (done, failed or out of time) Slurm starts the build job on a CPU:
     build.sbatch:   tablescrape build OUT          <- writes tables.xlsx, csv/, index.csv, documents.csv
```

### 3.3 The scripts, one by one

#### `environment.yml`: the software list

A conda environment file: Python 3.11 plus pinned versions of PyTorch (2.6.0 built for CUDA 12.4), transformers
(4.57.6), and the PDF and Excel libraries. **Why PyTorch 2.6 / CUDA 12.4:** the iTiger docs warn that newer default
PyTorch builds need a newer NVIDIA driver than the cluster has and then silently fall back to the CPU, which is about
100 times slower. The pins avoid that. Do not "upgrade" them casually.

#### `env.sh`: paths and activation

Not run on its own; the other scripts `source` it (run it *inside* the calling shell, so what it sets stays set). It:

- points the model download folder (`HF_HOME`), conda's package cache and pip's cache to `/project/$USER/...`, because
  home has only 20 GB and the 32B model alone is 64 GB;
- defines where the environment lives (`/project/$USER/conda-envs/tablescrape`);
- activates the environment, with `set +u` around conda's own scripts (they read variables that may be unset, which
  would otherwise crash a script that uses `set -u`);
- sets `PYTHONNOUSERSITE=1` so packages in `~/.local` cannot override the pinned ones.

#### `setup.sh`: one-time install (login node)

Creates the conda environment from `environment.yml`, installs the `tablescrape` command into it, then **downloads
the model weights** with `huggingface_hub`. It takes the model id as an optional argument; the default is
`Qwen/Qwen3-VL-8B-Instruct` (17 GB). **It downloads only the one model you name**, and jobs run with
`HF_HUB_OFFLINE=1` (below), so the model you run must be the model you downloaded. This is installing and
downloading, which the admins allow on the login node; nothing runs the model.

#### `scrape.sbatch`: the GPU job

A shell script whose first lines, `#SBATCH ...`, are settings for Slurm rather than commands:

| line | meaning |
|---|---|
| `--job-name=tablescrape` | the name shown in `squeue` and used in the log file name |
| `--partition=bigTiger` | run in the GPU partition |
| `--gres=gpu:1` | one GPU (any type) |
| `--cpus-per-task=4` | 4 CPU cores |
| `--mem=64G` | 64 GB of host RAM |
| `--time=12:00:00` | stop the job after 12 hours |
| `--output=logs/%x-%j.log` | write everything it prints to `logs/<job name>-<job id>.log` |

Then it: checks it was given a PDF folder and an output folder, sources `env.sh`, sets `HF_HUB_OFFLINE=1` (use the
downloaded model, never try the internet from a compute node), prints the GPU it got (`nvidia-smi`), and runs
`tablescrape run`. If it was submitted as a Slurm array, it converts the array number into `--shard I/N`.

Anything you put on the `sbatch` command line **overrides** these `#SBATCH` lines. That is how the 32B model gets an
H100 (section 4.5).

#### `build.sbatch`: the CPU job

1 CPU, 8 GB, 30 minutes. Sources `env.sh` and runs `tablescrape build OUT`. No GPU, so it starts quickly.

#### `submit.sh`: both jobs, chained

Takes `PDF_DIR OUT_DIR [N_GPUS] [options]`. It submits the GPU job (as an array of N if `N_GPUS` is above 1, each task
reading a different share of the PDFs), then submits the build job with `--dependency=afterany:<GPU job id>`. `afterany`
means "start when the GPU job has ended, however it ended", so a job that timed out still produces a workbook of what
it read, and the *Documents* sheet says which PDFs are unfinished. Set `SBATCH_OPTS` to pass extra Slurm options to
the GPU job. It also creates `logs/` (Slurm does not create the log folder itself; if it is missing, the log is lost).

---

## 4. Instructions for a new team member

Replace `<you>` with your iTiger user name (the examples use `jtbass1`). **You need the university VPN (or campus
network) for anything that touches iTiger.** `itiger.memphis.edu` is a private address.

### 4.1 Connect

```bash
ssh <you>@itiger.memphis.edu
```

- `ssh` is the secure-shell client. `<you>@itiger.memphis.edu` is user `@` host. It asks for your password.
- **Produces:** a shell prompt on the login node.

If this hangs with no password prompt, the VPN is not connected.

### 4.2 Clone the repository onto the cluster

On the login node:

```bash
cd /project/<you>
git clone git@github.com:natsirt-uofm/tablescrape.git tablescrape
```

- `cd /project/<you>` moves to your project folder. Work in `/project/<you>/`, not your home folder (20 GB limit).
- `git clone git@github.com:natsirt-uofm/tablescrape.git tablescrape` copies the repository from GitHub. `git@github.com:natsirt-uofm/tablescrape.git` is the repository's address (SSH form; the HTTPS form is on the
  green **Code** button); the last word is the name of the folder to create.
- **Produces:** `/project/<you>/tablescrape/` with the code and scripts. PDFs and results are not in the repository.
- **If the repository is private**, the login node needs your GitHub access. Either add an SSH key (`ssh-keygen -t
  ed25519` on the cluster, paste `~/.ssh/id_ed25519.pub` into GitHub under Settings, SSH keys, and clone the
  `git@github.com:...` address) or clone the `https://` address and use a personal access token as the password.
- **To get updates later:** `cd /project/<you>/tablescrape && git pull` (downloads new commits and merges them in).
  Run `setup.sh` again only if `hpc/itiger/environment.yml` changed.

### 4.3 One-time setup (login node, 30+ minutes, mostly downloading)

On the login node:

```bash
cd /project/<you>/tablescrape
bash hpc/itiger/setup.sh Qwen/Qwen2.5-VL-32B-Instruct
```

- `cd` moves into the repository; every script expects to be started from its root.
- `bash hpc/itiger/setup.sh` runs the setup script. The last argument is the **model to download**.
- **Produces:** the conda environment in `/project/<you>/conda-envs/tablescrape`, and the model in
  `/project/<you>/hf-cache/`. The script ends by printing `torch 2.6.0+cu124 | built for CUDA 12.4`. If it prints a
  different CUDA version, the pins were overridden; stop and ask.
- **Which model?** `Qwen/Qwen2.5-VL-32B-Instruct` (64 GB) is the one that gave the best results and needs an
  **H100**. `Qwen/Qwen3-VL-8B-Instruct` (17 GB, the default if you give no argument) fits any GPU but was clearly
  worse on hard tables (section 7). Check storage first: the 32B model is a third of your 200 GB project quota.

### 4.4 Quick test on a small folder (do this first)

Put 2 or 3 PDFs in `/project/<you>/test_pdfs/`. Then submit a test that reads only the first 2 pages of each:

```bash
cd /project/<you>/tablescrape
SBATCH_OPTS="--gres=gpu:h100_80gb:1 --mem=96G --time=01:00:00" \
  bash hpc/itiger/submit.sh /project/<you>/test_pdfs /project/<you>/test_out 1 \
  --model Qwen/Qwen2.5-VL-32B-Instruct --max-pages 2
```

- `SBATCH_OPTS="..."` placed before the command sets an environment variable for that one command. `submit.sh` passes
  its contents to `sbatch`. `--gres=gpu:h100_80gb:1` requests one H100, `--mem=96G` asks for 96 GB of host RAM (the
  32B model is read through host memory while loading), `--time=01:00:00` limits the job to one hour.
- the trailing `\` continues the command on the next line.
- `bash hpc/itiger/submit.sh` runs the submit script. Its arguments: the PDF folder, the output folder, the number of
  GPUs (`1`), and then anything else is passed on to `tablescrape run`.
- `--model ...` picks the model (it must be the one `setup.sh` downloaded); `--max-pages 2` reads only the first 2
  pages of each PDF.
- **Produces:** two job numbers printed to the screen, and later `/project/<you>/test_out/tables.xlsx`.

For the 8B model skip `SBATCH_OPTS` and `--model` (the defaults fit any GPU).

### 4.5 Run a whole folder

```bash
cd /project/<you>/tablescrape
SBATCH_OPTS="--gres=gpu:h100_80gb:1 --mem=96G --time=04:00:00" \
  bash hpc/itiger/submit.sh /project/<you>/papers /project/<you>/papers_tables 1 \
  --model Qwen/Qwen2.5-VL-32B-Instruct
```

Same as 4.4 without `--max-pages`. Plan the time from `tablescrape list` (section 4.6) and the speeds in section 7:
roughly 25 seconds per page on average for table-heavy papers, plus loading. You can log out; the jobs keep running.

To use more than one GPU, change the `1` to the number of GPUs (at most 4 for your quota). Each GPU loads its own copy
of the model, so splitting only pays off on large folders.

**If the job runs out of time:** run the same `submit.sh` command again. Finished pages are skipped. Add
`--retry-errors` to re-read pages that failed.

### 4.6 Useful commands

```bash
tablescrape list /project/<you>/papers
```
Counts PDFs and pages (activate the environment first: `source hpc/itiger/env.sh`). Cheap; fine on the login node.

```bash
squeue -u $USER
```
Lists your jobs. `-u` filters by user; `$USER` is a variable holding your user name. `ST` is `PD` (pending, waiting for
a GPU) or `R` (running); the last column is the node, or the reason it is waiting.

```bash
tail -f logs/tablescrape-<job id>.log
```
Prints a log and keeps printing new lines as they arrive (`-f` is follow). Ctrl+C stops watching, not the job. Each
page prints a line like `p3: 1 table(s), 42.0s`.

```bash
scancel <job id>
```
Cancels a job. Safe: every page read so far is kept.

```bash
sacct -j <job id> --format=JobID,State,Elapsed,ExitCode -X
```
Shows how a finished job ended. `-j` selects the job, `--format` picks columns, `-X` hides the internal sub-steps.
`COMPLETED` with `0:0` is success; `TIMEOUT` means it ran out of time.

`tablescrape run` options:

| option | default | when to change it |
|---|---|---|
| `--model` | `Qwen/Qwen3-VL-8B-Instruct` | to use another downloaded model |
| `--dpi` | 200 | 250 to 300 for tiny print (page is still capped by `--max-pixels`) |
| `--max-pixels` | 3200000 | raise for dense pages with small fonts (slower, more GPU memory) |
| `--max-new-tokens` | 8192 | raise if the Problems sheet says a reply was cut off (very long tables) |
| `--max-pages N` | all | first N pages of each PDF, for tests |
| `--retry-errors` | off | re-read only pages that failed or were cut off |
| `--shard I/N` | 0/1 | set for you by the array job |
| `--no-build` | off | skip building the workbook at the end |
| `--no-numbers` | off | keep every workbook cell as text |
| `--allow-cpu` | off | run without a GPU (very slow; never on the login node) |

### 4.7 Get the results to your computer

Run this **on your own machine**:

```bash
rsync -av <you>@itiger.memphis.edu:/project/<you>/papers_tables/ ./papers_tables/
```

- source `user@host:path/` (with the trailing `/`: the contents of that folder); destination `./papers_tables/`.
- **Produces:** a local copy. Add `--exclude 'pages'` to skip the page images (they are most of the size); you then
  lose the "check against the page" step, so only do that for a quick look.

To fetch only the workbook: `scp <you>@itiger.memphis.edu:/project/<you>/papers_tables/tables.xlsx .`
(`scp` copies one file over SSH; the final `.` means "into the current folder").

### 4.8 Run the tests (any computer, no GPU)

```bash
python -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/pytest
```

- `python -m venv .venv` creates an isolated Python environment in `.venv/`.
- `pip install -e ".[test]"` installs the package in *editable* mode (`-e`: code changes take effect without
  reinstalling) plus the test tools.
- `pytest` finds and runs everything in `tests/`. Expect 28 passing tests in under a second.

---

## 5. Reading the results

Output folder layout:

```
OUT/
  tables.xlsx          Index, Documents, one sheet per table, Problems
  index.csv            the Index sheet as CSV
  documents.csv        the Documents sheet as CSV
  csv/<pdf>/<table id>.csv
  pages/<pdf>/document.json     one per PDF
  pages/<pdf>/p0003.json        one per page: the model's raw reply, the parsed tables, flags, timing  (the checkpoint)
  pages/<pdf>/p0003.png         the page image, kept when the page has a table
```

A table id looks like `smith2019_p004_t1`: PDF `smith2019`, page 4, first table on that page.

**Check in this order:**

1. **Documents sheet.** Every PDF is listed. Statuses: `N table(s)`, `no tables found`, `not finished: 4 of 10 pages
   read; run again to continue`, `cannot open: ...`.
2. **Problems sheet.** Pages that failed, replies that were cut off, and figures left out.
3. **Index sheet, `cells_to_check` and `warnings` columns.** `cells_to_check` counts the cells the stored-text check
   could not confirm. `all (no text layer)` means nothing could be checked (a picture table or a scan): treat every
   cell as unverified.
4. **Table sheets.** **Yellow** cells: not found in the PDF's text, compare with the page image. **Orange** cells: only
   the start of a longer value stored in the PDF; the comment shows the full value (the page image may be cut off).
5. **Shape, by eye.** The text check confirms characters, not structure. A value can be correct and still sit in the
   wrong row or column. Compare the table's shape with `pages/<pdf>/pNNNN.png`.

Things that are normal, not errors:

- A table that continues across pages comes out as separate tables; the later ones have `continued = yes`. Join them
  yourself.
- A paper that prints a forward/reverse pair in one cell comes out as one cell with two lines; a paper that prints tag
  letters in front of primers (a dye code, a CAG tag, a GTTT pigtail) comes out with those letters, exactly as printed.
  A later step that matches papers to a locus list needs to normalise both.
- A **figure** the model read as a table, if its caption does not start with "Fig." or "Figure", still appears as a
  table. It usually shows many flagged cells.

---

## 6. Can we trust these results? Flaws, and what the team must do

### The honest answer

**Treat the output as a first draft that saves typing, not as a finished dataset.** It does the bulk copying and
shows where it is unsure; a person still decides what is correct.

- **What supports trusting it:** on 17 papers, 318 of 323 primers matched the reference table, an earlier extraction of the same papers (281 letter for letter);
  where the PDF stores its text, the characters are checked against that text; cells that fail the check are
  highlighted rather than hidden.
- **What stops us trusting it blindly:** the checks cover only part of what can go wrong (below), the accuracy was
  measured on one kind of table in one research area, and the comparison reference is itself an earlier extraction by a different method, not a hand-verified answer.

### Trust levels

| what you see | how far to trust it | what to do |
|---|---|---|
| No highlight, table has stored text, no warnings | **High** for the characters. Not a guarantee of structure. | Spot-check (below). |
| **Yellow** cell (not found in the PDF's text) | **Low**. The model probably misread it. | Check it against the paper. Correct it by hand. |
| **Orange** cell (start of a longer stored value) | **Medium**. The value is probably cut short; the comment shows the full one. | Check, then use the full value if the paper agrees. |
| `cells_to_check` = `all (no text layer)` | **Do not trust.** Nothing could be verified. Picture tables were wrong in about half the test cells. | Check every cell, or type the table by hand. |
| A warning in the Index (`no data rows`, `padded`, `header has N names for M columns`) | **Low** for the structure. | Compare the shape with the page image. |
| `continued = yes` | The table is split across pages. | Join the pieces by hand and check the join. |
| A PDF listed as `no tables found` | Unknown. It may truly have none, or the model may have missed one. | Open it and look (see the checklist). |

### Known flaws

1. **Picture tables are unreliable.** When a table is pasted into the PDF as an image there is no stored text, so the
   model reads pixels and makes letter and digit errors. One test table: 10 of 26 primers exactly right. The tool flags
   these as unverified; it cannot fix them.
2. **The check sees characters, not structure.** A value can be correct yet sit in the wrong row or column, a row can
   be skipped, or two columns merged. The stored-text check cannot detect any of these. In testing, smaller models
   wrote a whole table into the header row, or split one table into 14 tiny ones; the largest model did not on our
   test pages, but nothing guarantees that on other layouts.
3. **A missed table gives no signal.** If the model fails to notice a table, that page simply reports "no tables". We
   found one primer missing from a Sanderlin table, and we do not know the miss rate across all papers.
4. **A wrong table can look right.** A vision model can write a plausible value that is not on the page. The stored-text
   check catches this where the PDF has text; it cannot where it does not.
5. **Figures can be read as tables.** The tool leaves out anything labeled "Fig." or "Figure", but an unlabeled
   figure (a phylogeny, a diagram) can still come out as a table, usually with many flagged cells.
6. **Narrow testing.** The accuracy numbers come from 17 papers of one kind (microsatellite primer tables). Numeric
   results tables, tables with merged cells, rotated tables and scanned papers were barely tested, so do not carry
   these numbers over to them.
7. **The reference is not ground truth.** "98.5% of the reference primers present" means agreement with an earlier
   extraction made by a different method. Only the 5 disagreements were checked by hand. Both could share an error.
8. **Papers print things the way they print them.** Dye codes, tags, pigtails and two primers in one cell come out
   exactly as printed (this is deliberate, the tool never "corrects" a value). Data that should be cleaned for analysis
   needs a separate cleaning step.
9. **Results depend on the settings.** The same PDF can give a different table with a different model, a different
   prompt, or a different resolution. Changing any of these changes every result downstream, so results from different
   settings must not be mixed in one dataset without noting it.
10. **Not yet run on the whole collection.** The largest run was 17 PDFs. Expect surprises across 145.
11. **No matching to your locus list.** This tool copies tables; deciding which row belongs to which locus, and which
    paper to believe when two disagree, is not done here.

### What the team must do

**Before using any result:**

- [ ] Open the **Documents** and **Problems** sheets first. Resolve every `cannot open`, `not finished` and
      `unreadable reply`.
- [ ] Check **every yellow and orange cell** against the paper.
- [ ] Check **every table marked `all (no text layer)`** completely, or enter it by hand.
- [ ] Check the **shape** of every table with a warning, and of at least a sample of the others, against
      `pages/<pdf>/pNNNN.png`: right number of rows, right number of columns, columns under the right headers.
- [ ] **Open every PDF listed as `no tables found`** and search it for "Table". One quick look catches a missed table.
- [ ] **Spot-check a random sample** of the tables with no flags: for each batch, pick at least 5 tables (or 10% if the
      batch is large) and compare whole rows with the paper, not just one cell. If you find an error here, stop trusting
      the unflagged tables for that batch and check more.
- [ ] **Anything you will act on gets verified against the paper by a person.** Examples: a primer you will order (one
      wrong letter is a different primer), a number that goes into a published analysis. This applies even to cells with
      no flag.

**For every batch:**

- [ ] **Keep the evidence.** Keep the whole output folder, including `pages/` (the raw model replies and the page
      images). Without it, nobody can check or reproduce a value later.
- [ ] **Record the settings:** the model name, the date, the options used, and the repository version. Put them in the
      methods of anything you publish.
- [ ] **Do not edit `tables.xlsx` as the master copy.** Copy corrected tables into your own curated file, and note which
      cells were changed by hand and why.
- [ ] **Say it was machine-read.** Anyone using the data should know it came from a model plus a human check.

**Before changing anything or using a new kind of document:**

- [ ] Changing the model, the prompt (`vlm.py`), the resolution or the checking code means **rerunning the
      comparison** (the 17-PDF test) before trusting new results. The 28 automatic tests do not cover the model's reading.
- [ ] For a **new type of document or table**, run 5 or so PDFs first and check them completely by hand before
      starting a large batch. Do not assume the accuracy above applies.

**What the tool will not do for you:** decide what the right answer is. A value that is highlighted, or missing, or
wrong without any flag, is a person's call.

---

## 7. What was measured, and what is not solved

All numbers are from real runs on iTiger. Reference = a primer table extracted earlier, by a different method, from the same papers.

### What has actually been run on the cluster

| piece | status |
|---|---|
| `setup.sh`, `env.sh`, `environment.yml` | **Run.** Environment built, 8B model downloaded; CUDA 12.4 build confirmed. The 32B model was already cached on one account; `setup.sh` with the 32B id has not been run by a second person. |
| `scrape.sbatch` with the 8B model | **Run** (several jobs). |
| `scrape.sbatch` with the 32B model on an H100 (`--gres=gpu:h100_80gb:1 --mem=96G`) | **Run** (two jobs: 61 pages and 126 pages). Submitted with `sbatch` directly. |
| `submit.sh` + `build.sbatch` (the chained pair) | **Run once**, with the 8B model and 1 GPU: GPU job 6 min, build job started by itself after it and wrote `tables.xlsx`. |
| `submit.sh` with `SBATCH_OPTS` (the 32B command in 4.4 and 4.5) | **Not run exactly as written.** It does the same as the direct `sbatch` runs above, but run the small test in 4.4 first. |
| `submit.sh` with more than 1 GPU (Slurm array and `--shard`) | **Not run on the cluster.** The sharding logic is covered by an automatic test; the array submission is not. |
| `--retry-errors`, resuming after a time-out | Resuming is covered by an automatic test, not by a real time-out. |
| The full 145-PDF collection | **Not run.** |

### Accuracy against the reference table

17 PDFs (126 pages, 71 tables found, 0 errors), Qwen2.5-VL-32B on one H100, matched by primer sequence:

| outcome | primers | meaning |
|---|---|---|
| exact | 281 (87%) | identical letter for letter |
| present, extra letters | 37 | same primer; the paper prints tag letters in front and the tool kept them (Sanderlin) |
| missing or different | 5 | 1 real miss (Sanderlin `UABM3P1F04U` reverse); 2 not in the PDF's text so the two readings disagree; 1 glued forward+reverse string in the reference; 1 unclear |

Several papers were fully exact: Kleven (68 of 68), Brandt (38/38), Meredith (28/28), Wu (30/30), Poissant (20/20),
Shih (20/20), Kitahara (16/16), Huang (14/14).

The reference itself is an earlier extraction, not a verified truth. Only the 5 differences were checked by hand.

### Models compared on a 10-PDF, 61-page test

| model | time | result |
|---|---|---|
| Qwen3-VL-8B | 26 min | sequences exact, but table structure failed on cropped, header-less pages (a whole table written into the header) |
| Qwen2.5-VL-7B | 19 min | sequences exact, but split one page into 14 tiny tables and missed a picture table |
| **Qwen2.5-VL-32B** (H100) | 36 min | structure correct on all of those hard pages |

Adding the PDF's stored text to the prompt fixed the changed-letter problem seen in the first run.

### Speed

| part | time |
|---|---|
| model load | 1 to 17 minutes (about 1 minute when the node still had the files cached; 17 on a cold node) |
| page with no table | about 1.5 seconds |
| page with a table | 30 to 75 seconds (up to 160 for a dense one) |
| 126 pages, 17 PDFs, 32B on one H100 | 54 minutes |

**Why it is slow.** The time is the model writing out each table (about 2,500 characters) one piece at a time, one page
at a time, on a GPU that spends most of that time waiting on memory. That is how plain Hugging Face `generate()` works;
it is the slowest standard way to serve a model.

### Not solved yet

- **Tables pasted as pictures** (no stored text). One test table: 10 of 26 primers exactly right, about 15 wrong by a
  letter or more. The yellow flags catch them, but the values are not usable without a person or a second reader.
- **Speed.** The goal is under 30 minutes for the full 145-PDF set. The plan: a serving engine that reads many pages at
  once (vLLM), plus several GPUs. Not built; the cluster's GPU driver (CUDA 12.6 at most) limits which vLLM version
  can run, and that has not been tested.
- **One table per page stays one table per page.** A table that continues across pages is not merged.
- **Not yet run on all 145 PDFs.** The largest run was 17 PDFs. Expect some PDFs to need a human look.
- **No consolidation step.** Matching to a locus list, aliases and choosing between papers is not part of this tool.
- **Disk.** Each person who runs the 32B model downloads their own 64 GB copy.

---

## 8. Troubleshooting

| symptom | cause and fix |
|---|---|
| `ssh` hangs with no password prompt | not on the VPN or campus network. Connect, then test with `nc -vz -w 5 itiger.memphis.edu 22` (`-z` check only, `-w 5` wait 5 s). |
| `tablescrape run` says `no GPU visible` | you are on the login node or a CPU job. Run through `sbatch`/`submit.sh`. This is the guard working as designed. |
| job stays `PD` with reason `(Priority)` | normal queueing; GPUs are busy. The H100 partition is smaller, so requests for it can wait longer. |
| job ends at once, log says model not found / offline | `HF_HUB_OFFLINE=1` is set, and the model you asked for was not downloaded. Run `setup.sh <that model id>`. |
| `CUDA out of memory` in the Problems sheet, or the job fails loading | the 32B model needs the 80 GB H100 and `--mem=96G`. Request `--gres=gpu:h100_80gb:1`. |
| job `TIMEOUT` | rerun the same `submit.sh`; finished pages are skipped. |
| a page in Problems: `unreadable reply` | the model did not return valid JSON. `--retry-errors` tries again; the raw reply is in `pages/<pdf>/pNNNN.json` (field `raw`). |
| a page in Problems: `reply cut off` | a very long table hit the token limit. Rerun with `--max-new-tokens 12000 --retry-errors`. |
| no log file appeared | the `logs/` folder did not exist when the job started. `submit.sh` creates it; if you used `sbatch` directly, `mkdir -p logs` first. |
| setup prints `built for CUDA` something other than 12.4 | the pinned PyTorch was overridden; inference would silently run on the CPU. Stop and ask. |
| a table is wrong but nothing is flagged | the stored-text check cannot see wrong *structure* (right value, wrong row or column) or tables with no stored text. Compare against `pNNNN.png`. |
