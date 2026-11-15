# Runbook: reproduce two published benchmarks and compare with omega-eval

Written 2026-11-15. Nothing here was run by the author of this file. Every command was copied or adapted from a page fetched that day. The source page is named after each command block. A line marked `CHECK:` is adapted or inferred. Confirm it before you rely on it. Steps the pages do not document are listed under "Not documented" in each part and again in section 7.

Abbreviations: API = application programming interface. GPU = graphics processing unit. BM25 = a keyword-ranking formula (Best Matching 25). CMU = Carnegie Mellon University. JSONL = JSON Lines. HF = Hugging Face. SLM = small language model.

Sources used (short names):
- CMU-README: https://raw.githubusercontent.com/malusamayo/migration-analysis/main/README.md
- CMU-repo: https://github.com/malusamayo/migration-analysis (page, `docker-compose.yml`, `.gitmodules`, `.python-version`, `pyproject.toml`, `CLAUDE.md`)
- CMU-paper: https://arxiv.org/abs/2607.08938
- BCP-README: https://raw.githubusercontent.com/texttron/BrowseComp-Plus/main/README.md
- BCP-oss: https://raw.githubusercontent.com/texttron/BrowseComp-Plus/main/docs/oss.md
- BCP-openai: https://raw.githubusercontent.com/texttron/BrowseComp-Plus/main/docs/openai.md
- BCP-judge: https://raw.githubusercontent.com/texttron/BrowseComp-Plus/main/docs/llm_as_judge.md
- BCP-client: https://raw.githubusercontent.com/texttron/BrowseComp-Plus/main/search_agent/oss_client.py
- BCP-evalopenai: https://raw.githubusercontent.com/texttron/BrowseComp-Plus/main/scripts_evaluation/evaluate_with_openai.py
- BCP-decrypt: https://raw.githubusercontent.com/texttron/BrowseComp-Plus/main/scripts_build_index/decrypt_dataset.py
- BCP-indexes: https://raw.githubusercontent.com/texttron/BrowseComp-Plus/main/scripts_build_index/download_indexes.sh
- HF-card: https://huggingface.co/datasets/Tevatron/browsecomp-plus

Fetch limits: the fetch tool returned summaries, not verbatim files. Commands below are therefore reconstructed from those summaries. Compare each against the real README before running it.

---

## 1. What this reproduces and what it costs

Part A reproduces the default (unadapted) harness numbers from the CMU paper "Better Harnesses, Smaller Models" (CMU-paper). Scope:
- Test split only. The paper uses a 20/20/60 train/validation/test split per task.
- Three runs per configuration.
- Three models: `gemma-4-26b-a4b`, `qwen3-coder-30b-a3b`, `ministral-3-8b`.
- Seven tasks: attendance-auditing, budget-approval, stock-alert, anomaly-detection, playwright-testing, website-management, code-refactoring.

Part B reproduces one BrowseComp-Plus configuration (arXiv 2508.06600): `gpt-oss-20b`, BM25 retrieval, high reasoning effort, all 830 queries, top 5 results per search each cut to 512 tokens, `gpt-4.1` as judge. Published accuracy: 21.08%.

Cost:
- Part A: roughly $280 in model calls across three models, implied by the paper's per-instance costs. The paper priced Gemma on Google Vertex AI and the other two on AWS Bedrock (Amazon Web Services). Your bill depends on the provider you use.
- Part B: not published for open models. Two cost drivers exist. First, serving `gpt-oss-20b` (your GPU, or a hosted endpoint). Second, `gpt-4.1` judge calls for 830 queries on your OpenAI account. Estimate both from your provider's price list before you start.

Stop-and-confirm points are marked **STOP** below. Do not pass one without deciding the spend is acceptable.

---

## 2. Prerequisites

1. A machine with `git`, `docker` and `docker compose`. Source: CMU-README says the task-server Docker image is required; CMU-repo shows `docker-compose.yml`.
2. `uv` (Python package manager). Source: CMU-README and CMU-repo `CLAUDE.md` use `uv run`. The page gives no `uv` install steps. CHECK: install `uv` from its own documentation.
3. Python 3.14 for Part A. Source: CMU-repo `.python-version` contains `3.14`, and `pyproject.toml` has `requires-python >=3.14`. `uv` can fetch it.
4. SSH (Secure Shell) access to GitHub for Part A. Source: CMU-repo `.gitmodules` lists `software-agent-sdk` with the URL `git@github.com:malusamayo/software-agent-sdk.git`. CHECK: that submodule may need an SSH key on your GitHub account, and may be private or fail to clone. Confirm in step A1.
5. Model access for Part A. Source: CMU-paper abstract does not name providers; the task description says Vertex AI for Gemma and Bedrock for the other two. `pyproject.toml` includes `google-cloud-aiplatform`, `google-auth` and `boto3`. Credential variable names are not documented (see "Not documented", Part A).
6. For Part B: Python 3.10 with `uv`, and Java 21. Source: BCP-README.
7. For Part B: `gpt-oss-20b` served on a GPU with Docker (`--gpus all`), or an OpenAI-compatible endpoint you trust. Source: BCP-oss (Docker serving). CHECK: GPU memory needed is not stated; check the `gpt-oss-20b` model card.
8. An OpenAI API key with access to `gpt-4.1`. Source: BCP-judge.
9. A Hugging Face account. Source: HF-card and BCP-README say you may need `huggingface-cli login` or an `hf_token`.
10. Disk: not stated by any fetched page. CHECK: the BM25 index, corpus, Docker images and results sizes are unknown. Reserve tens of GB and check the Hugging Face repository file sizes first.
11. omega-eval installed, from the agentbus repository:

```bash
cd evals && pip install -e ".[dev]"
```

---

## 3. Part A: CMU business-task suite

### A1. Clone and pin a commit

```bash
git clone https://github.com/malusamayo/migration-analysis
cd migration-analysis
git rev-parse HEAD
```

Source: repository URL from CMU-repo. `git rev-parse HEAD` is standard git. CHECK: the fetched page did not show the latest commit hash. Record the hash printed above. The paper's artifacts may correspond to a specific commit; the README does not say which. Ask the authors if exact match matters.

Submodules (CMU-repo shows `LOCA-bench` pinned at `387b24e` and `software-agent-sdk` at `89c2196`):

```bash
git submodule update --init --recursive
```

CHECK: this command is standard git, not copied from the README. Source of the need: `.gitmodules` in CMU-repo. Confirm it succeeds for `software-agent-sdk` (see prerequisite 4). Record `git submodule status` output.

### A2. Environment setup

The README shows no install step. It shows only these run forms (CMU-README):

```bash
uv run python run.py run-baseline
```

CHECK: `uv run` is expected to create the environment from `pyproject.toml` on first use. The README does not say so. If it fails, tell the person who owns this runbook; do not guess a fix.

Credentials: see "Not documented", Part A.

### A3. Per-task external setup

From CMU-README: "Task datasets are the root `data/*.json` files. Each task's `tasks/<task_id>/run*.yaml` points to its raw data through `data_path`."

External setup named by the README:
1. RefactorBench (code-refactoring): clone its source repository and point the data file to that path. Repository: https://github.com/microsoft/RefactorBench (CMU-repo links).

```bash
git clone https://github.com/microsoft/RefactorBench
```

CHECK: the clone command is adapted from the repository URL. The README only says to clone and point the data file at it. Open the task's `run*.yaml` and the matching `data/*.json` to see which key holds the path. This is `data_path` per the README; confirm.

2. WebArena tasks: set up the environment using webarena-verified (https://github.com/ServiceNow/webarena-verified). The README gives no commands. Follow that project's own README.
3. Other tasks: set up the environment using the custom LOCA-bench build (https://github.com/malusamayo/LOCA-bench), which is the `LOCA-bench` submodule. The README gives no commands.
4. Task-server Docker image. Source: `docker-compose.yml` in CMU-repo. It defines services named `webtest`, `refactorbench`, `webarena`, `docbench`, `browsecomplongcontext`, `machine_operating_s2l`, `woocommerce_stock_alert_s2l`, `budget_approval_s2l`, `attendance_payroll_audit_s2l`, `tau2_airline`. Every service has a build argument `USER_UID` that fails the build unless set. The error text names this command form:

```bash
UID=$(id -u) docker compose build
```

CHECK: the compose file's own message says `run with UID=$(id -u) docker compose build`. This builds all active services. You may build one service by name; confirm which services your seven tasks need.

Mapping from the seven task names to `tasks/<task_id>` directories and compose services is not documented. Likely matches from the compose service names (all CHECK): attendance-auditing = `attendance_payroll_audit_s2l`; budget-approval = `budget_approval_s2l`; stock-alert = `woocommerce_stock_alert_s2l`; anomaly-detection = `machine_operating_s2l`; playwright-testing = `webtest`; code-refactoring = `refactorbench`; website-management = unknown (possibly `webarena`). Open `tasks/` in your clone and match each task name to its directory before any run.

### A4. Smoke test: one model, a handful of instances

**STOP.** This spends money on one provider. Pick the cheapest model path (the paper's own example uses `gemma-4-26b-a4b`).

The README's example baseline run uses task `machine_operating_s2l` and model `gemma-4-26b-a4b`. It passes a tab-separated manifest on standard input with columns `task_id`, `model_name`, `max_examples`, `n_responses`, plus `--manifest -`, `--batch-dir` and `--yes`. The exact command text was not preserved by the fetch. Adapted form:

```bash
printf 'machine_operating_s2l\tgemma-4-26b-a4b\t5\t1\n' | uv run python run.py run-baseline --manifest - --batch-dir generated/baseline_batches/smoke --yes
```

CHECK: reconstructed from a summary. Whether the manifest needs a header row is unknown. The value of `--batch-dir` is a guess based on the documented output folder `generated/baseline_batches/<batch>/`. The model name `gemma-4-26b-a4b` is the README's own example. CHECK: whether it routes to Vertex AI or another provider, and what environment variables it needs, is not documented. Run `uv run python run.py run-baseline --help` and read it. Source for the paper-scale columns: CMU-README ("`max_examples=100` and `n_responses=3` for 100-example tasks; WebArena uses 50 examples").

After the run, look at `generated/baseline_batches/smoke/` (`batch.json`, `configs/`, `logs/`) and `results/<task>/gemma-4-26b-a4b_default/rollouts/baseline/`. Confirm: no errors in `logs/`, the cost per instance is near the paper's, and you can find a pass/fail value per instance. Do not go on until you can.

### A5. Full default-harness runs

**STOP.** About $280 across three models. Confirm budget and provider limits first.

The README documents the paper-scale baseline settings as `max_examples=100` and `n_responses=3` (WebArena: 50 examples). Build one manifest with a row per task and model. Example for one model (task directory names are CHECK, see A3):

```bash
printf 'attendance_payroll_audit_s2l\tgemma-4-26b-a4b\t100\t3\nbudget_approval_s2l\tgemma-4-26b-a4b\t100\t3\n' | uv run python run.py run-baseline --manifest - --batch-dir generated/baseline_batches/gemma_full --yes
```

CHECK: columns and the batch directory are as in A4. Repeat for `qwen3-coder-30b-a3b` and `ministral-3-8b` using the exact model strings that `run.py` accepts. Those strings are not documented (see "Not documented").

Important unknown: the README does not say whether `run-baseline` runs only the 60% test split or all examples. If it runs all 100, filter to test-split instances when you map results. The paper's 20/20/60 split assignment is not described in any fetched page. CHECK: inspect `data/*.json` and the task `run*.yaml` for a split field or seed before you trust a pass rate.

Run one model at a time. Check cost after each.

### A6. Where results land

Source: CMU-README.
- Batch folders: `generated/baseline_batches/<batch>/` containing `batch.json`, `configs/`, `logs/`.
- Per-task results: `results/<task>/<model>_default/rollouts/baseline/`.

Per-instance file format inside `rollouts/baseline/`: not documented. Which fields hold pass/fail, tokens, cost and wall time is unknown. Do this by hand: open one rollout file from the smoke test, list its keys, and write the mapping on paper. Then write a converter yourself. Fields you need for omega-eval: `task_id`, `passed`, `cost_usd`, `input_tokens`, `output_tokens`, `wall_seconds`, `trajectory`.

The replication package (`replication_package/scripts/collect_run_data.py`) also processes run data, but its output format was not fetched. Raw trace files are not included in the figshare archives (CMU-README), so it will not help with your new runs.

### Part A: Not documented
- Environment variable names and credential setup for Vertex AI and Bedrock.
- The accepted model-name strings in `run.py` for the three models and how they route to a provider.
- Python install and dependency step beyond `uv run`.
- Mapping from the seven task names to `tasks/<task_id>` directories and compose services.
- Whether `run-baseline` uses the test split only, and how splits are assigned.
- The exact contents of `tasks/<task_id>/run*.yaml` and `data/*.json` (the directory listings were blocked by the site's robots rules).
- Per-instance output file format, token counts, cost fields.
- Docker build/run steps for the tasks beyond `docker compose build`.
- The commit corresponding to the paper's runs.
- Contents of the repository's `docs/` folder (not fetchable).

---

## 4. Part B: BrowseComp-Plus

### B1. Clone and pin

```bash
git clone https://github.com/texttron/BrowseComp-Plus
cd BrowseComp-Plus
git rev-parse HEAD
```

Source: repository URL given in the task; `git rev-parse HEAD` is standard git. CHECK: record the printed hash. The fetched pages did not show a commit hash.

Environment. BCP-README says: use `uv` with Python 3.10, run `uv sync`, activate `.venv`, then install `flash-attn` with `--no-build-isolation` (needed for FAISS, Facebook AI Similarity Search; not needed for BM25 but listed in the README). Java 21 is required, via conda (`openjdk=21` from conda-forge) or apt.

```bash
uv sync
source .venv/bin/activate
```

CHECK: the exact commands were summarised, not quoted. `source .venv/bin/activate` is the standard form. Copy the exact install lines from the README, including the `flash-attn` line and the Java 21 line. Confirm `java -version` shows 21.

### B2. Get the data

The dataset card (HF-card) says fields other than `query_id` are obfuscated, and that you may need `huggingface-cli login` or an `hf_token`. The README says to run a decrypt script that writes `data/browsecomp_plus_decrypted.jsonl` and `topics-qrels/queries.tsv`.

```bash
huggingface-cli login
python scripts_build_index/decrypt_dataset.py --output data/browsecomp_plus_decrypted.jsonl --generate-tsv topics-qrels/queries.tsv
```

Source: BCP-README (script path, flags, output paths); flags confirmed in BCP-decrypt (`--output`, `--generate-tsv`). CHECK: the exact command line is assembled from those parts. The dataset script has a default canary (password) built in, so no key is needed. The corpus is not obfuscated and loads from `Tevatron/browsecomp-plus-corpus` (BCP-README).

Check: the TSV should have 830 lines.

```bash
wc -l topics-qrels/queries.tsv
```

CHECK: `wc -l` is a standard command, not from a page.

### B3. Keyword (BM25) index

```bash
huggingface-cli download Tevatron/browsecomp-plus-indexes --repo-type=dataset \
  --include="bm25/*" --local-dir ./indexes
```

Source: BCP-indexes (the BM25 line of `scripts_build_index/download_indexes.sh`). This places the index at `indexes/bm25/`, the path used by the run commands in BCP-oss. Building the index yourself is not covered by the fetched pages; use the download. Pyserini (the search library) is used by the BM25 searcher; the README's `uv sync` is expected to install it. CHECK: confirm `pyserini` imports after setup.

### B4. Serve `gpt-oss-20b` with high reasoning effort

**STOP** if you use a hosted endpoint (it spends money). Serving on your own GPU costs only your hardware time.

Run in a separate terminal (BCP-oss):

```bash
docker run --gpus all \
    -p 8000:8000 \
    --ipc=host \
    vllm/vllm-openai:v0.10.1 \
    --model openai/gpt-oss-20b
```

Source: BCP-oss ("Serving gpt-oss-20b (BM25 pairing)"). The page uses `vllm/vllm-openai:v0.10.1`. Record the image tag.

Reasoning effort is set by the client, not the server. In `oss_client.py` the `--reasoning-effort` flag accepts `low`, `medium`, `high`, defaults to `high`, and is sent in each request as `"reasoning": {"effort": <value>, "summary": "detailed"}` (BCP-client). Pass it explicitly anyway so the setting is recorded.

The client talks to `http://localhost:8000/v1` by default (`--model-url`) and sends a placeholder in place of an API key, the literal word EMPTY (BCP-client). To use a hosted OpenAI-compatible endpoint, set `--model-url`. CHECK: BCP-client has no flag for an API key, so hosted endpoints that need a key may not work without code changes. CHECK: some providers reject `"summary": "detailed"`.

### B5. Smoke test: about 20 queries

The client has no limit or subset flag (BCP-client). A `--query` value ending in `.tsv` that exists is treated as a dataset. So make a 20-line TSV:

```bash
head -n 20 topics-qrels/queries.tsv > /tmp/smoke_queries.tsv
python search_agent/oss_client.py --model openai/gpt-oss-20b --reasoning-effort high --output-dir runs/smoke/oss-20b --searcher-type bm25 --index-path indexes/bm25/ --num-threads 10 --query /tmp/smoke_queries.tsv --snippet-max-tokens 512 --k 5
```

Sources: command form from BCP-oss (BM25 run command, with `--model openai/gpt-oss-20b` as the page says to use for the smaller model); `--reasoning-effort`, `--query`, `--snippet-max-tokens`, `--k` and their defaults from BCP-client. CHECK: `head` and the TSV-as-`--query` approach are inferred from BCP-client's description, not documented as a smoke-test method. CHECK: `--snippet-max-tokens 512` and `--k 5` are already the defaults; they are set here only so the run is explicit. The first 20 queries may not be representative.

Then judge them (B7) with a separate `--input_dir`. Check: each query has a run file, `status` is `completed` for most, and the judge parses. Use a separate output directory for smoke runs; the client skips queries whose IDs are already in saved `run_*.json` files in that directory (BCP-client).

### B6. Full run

**STOP.** Confirm the serving cost and that the smoke test looked sane.

```bash
python search_agent/oss_client.py --model openai/gpt-oss-20b --reasoning-effort high --output-dir runs/bm25/oss-20b --searcher-type bm25 --index-path indexes/bm25/ --num-threads 10 --snippet-max-tokens 512 --k 5
```

Source: BCP-oss BM25 run command, with the model changed from 120b to 20b as the page instructs. Defaults from BCP-client: `--max-iterations 100` (maximum function-calling rounds), `--max-tokens 10000` (sent as `max_output_tokens`), `--query` default `topics-qrels/queries.tsv`, query template `QUERY_TEMPLATE_NO_GET_DOCUMENT`. Temperature is not set in the client, so the server default applies. Record all of these as your settings. CHECK: BCP-openai says verbose models need `--max_tokens` about `40000` for some OpenAI models; it names `gpt-5` and `o4-mini`, not `gpt-oss-20b`. Decide whether to keep `10000`. The paper does not report this.

The run resumes if interrupted: rerun the same command and it skips finished query IDs (BCP-client).

### B7. Judge with `gpt-4.1`

**STOP.** 830 judge calls on your OpenAI account.

```bash
export OPENAI_API_KEY={your_openai_api_key}
python scripts_evaluation/evaluate_with_openai.py --input_dir runs/bm25/oss-20b
```

Source: BCP-judge. Defaults from BCP-evalopenai: `--model gpt-4.1`, `--max_output_tokens 1024`, `--ground_truth data/browsecomp_plus_decrypted.jsonl`, `--eval_dir ./evals`. Do not confuse the BrowseComp-Plus `./evals` output folder with the agentbus `evals` folder.

Note: the repository's current default judge is Qwen3-32B (`scripts_evaluation/evaluate_run.py`, per BCP-judge and BCP-README). The paper's number used `gpt-4.1`, so use `evaluate_with_openai.py`. BCP-judge says the two judges differ by under 1% on average.

### B8. Where results land

Run files (BCP-client, BCP-README): `runs/bm25/oss-20b/run_<UTC timestamp>.json`, one per query. README fields include `query_id`, `tool_call_counts`, `status` (`"completed"` means success), `retrieved_docids`, `result`.

Judge outputs (BCP-evalopenai), mirrored under `--eval_dir` (default `./evals`; the mirrored path is the parts of the input path after `runs`):
- `<stem>_eval.json` per query. Fields include `query_id`, `is_completed`, `judge_result`, `judge_response`, `tool_call_counts`, `model_info`.
- `evaluation_summary.json`: `Accuracy (%)`, `Recall (%)`, `per_query_metrics` (`query_id`, `correct`, `recall`).
- `detailed_judge_results.csv`: one row per query with `query_id`, `judge_correct`, `is_completed`, `parse_error`.

Mapping to omega-eval result fields:

| omega-eval field | Source |
|---|---|
| `task_id` | `query_id` |
| `passed` | `correct` in `evaluation_summary.json` `per_query_metrics` (or `judge_correct` in the CSV) |
| `attempt` | `0` for the single run |
| `trajectory` | path to the run file for that `query_id` |
| `source` | the run directory or eval path |
| `input_tokens`, `output_tokens`, `cost_usd`, `wall_seconds` | not documented; see below |

Fixed values: `arm` = `solo`, `model` = `gpt-oss-20b`, `harness` = `browsecomp-plus-search-agent`, `harness_version` = your B1 commit hash, `run_set` = `screening`, `settings` = `{"retriever": "bm25", "reasoning_effort": "high", "max_iterations": 100, "max_tokens": 10000, "snippet_max_tokens": 512, "k": 5, "temperature": "server default"}`. The two keys the reference match needs are `retriever` and `reasoning_effort`; the others record what you ran.

The fetched pages do not say whether run files store token counts, cost or wall time. Open one run file, list its keys, and write the mapping by hand. If tokens are absent, leave `cost_usd` and tokens out and note that.

### Part B: Not documented
- The exact decrypt command line (flags known, no example in the script).
- Disk and GPU memory requirements.
- Step limit, temperature, serving stack and number of runs used in the paper (the paper does not report them; your values are the defaults above).
- Whether run files contain token counts, cost or wall time.
- How to select the first N queries (no flag; the TSV workaround is inferred).
- Whether the hosted-endpoint path works (no API key flag).
- How to build the BM25 index from the corpus yourself (download is documented instead).
- The commit that matches the paper.

---

## 5. Load results into omega-eval and read the report

Work in the `evals` folder of the agentbus repository. Write your task records and results as JSON Lines (one object per line), using the field lists below.

Task records, one per task (for CMU: `benchmark` = `cmu-business-tasks`, `family` = task name such as `stock-alert`; for BrowseComp-Plus: `benchmark` = `browsecomp-plus`, `family` = empty):

```json
{"task_id": "q-0001", "benchmark": "browsecomp-plus", "benchmark_version": "<commit or date>", "family": "", "structure": "unknown", "grader": "model_judge", "split": "unassigned", "vetted": false, "notes": ""}
```

CHECK: the values in this example are illustrative. `structure` is `unknown` unless you have a reason. Use `grader` = `tests` or `state_check` for CMU tasks only after you read how each is graded. Choose `split` yourself from `unassigned`, `screening`, `development`, `test`; the CMU numbers are test-split numbers.

Result records, one per attempt:

```json
{"task_id": "q-0001", "arm": "solo", "model": "gpt-oss-20b", "harness": "browsecomp-plus-search-agent", "harness_version": "<commit>", "run_set": "screening", "attempt": 0, "passed": false, "settings": {"retriever": "bm25", "reasoning_effort": "high"}, "trajectory": "runs/bm25/oss-20b/run_<timestamp>.json", "source": "runs/bm25/oss-20b"}
```

CHECK: illustrative values. For CMU records, `harness` = `openhands-sdk-default`, `attempt` = 0, 1, 2 for the three runs, and `model` is one of the three CMU model names.

Unknown values: leave `cost_usd` out (or set it to `null`) when you do not know the cost. It is stored as unknown, not as zero, and the `validate` report does not need it. Leave `input_tokens`, `output_tokens` and `wall_seconds` out when unknown; do not set them to `null`. Extra keys inside `settings` are accepted as long as their values are strings, numbers or booleans.

Load:

```bash
omega-eval registry add tasks.jsonl --registry tasks.jsonl
omega-eval results import-jsonl results.jsonl --db results.sqlite --prices prices.yaml
```

Prices are needed only when `cost_usd` is missing and tokens are present. Copy `prices.example.yaml` to `prices.yaml` and fill in each model's `input_per_mtok`, `output_per_mtok`, `as_of` and `provider`. Null values are rejected on load, so fill all four models or remove those you do not need. Record the date you read the prices.

Compare:

```bash
omega-eval report validate --db results.sqlite --registry tasks.jsonl --references references.yaml
```

`references.yaml` already holds the published rates (read 2026-11-15): 21 CMU rows (7 tasks times 3 models) and BrowseComp-Plus rows for `gpt-oss-20b` at low, medium and high reasoning effort. Your BrowseComp-Plus results must carry `"retriever": "bm25"` and `"reasoning_effort": "high"` in `settings` to match the 21.08 row. CMU rates in that file are in percent.

Reading the report:
- `consistent`: the published rate falls inside the 95% Wilson interval of your measured rate. The interval treats tasks as independent trials and stays informative at 0% and 100%.
- `differs`: the published rate falls outside that interval.
- `no_data`: there are no measured results that match that reference (check `benchmark`, `family`, `model`, `harness` and `settings`).

A `differs` result does not by itself mean the paper is wrong. Check, in order: settings mismatch, split mismatch (A5), judge differences, model identifier or provider differences, sample size. For BrowseComp-Plus the paper does not give step limit, temperature or run count, so treat 21.08 as a loose reference.

---

## 6. What to record for every run

1. Repository commit hashes: `migration-analysis` HEAD, its two submodule hashes, `BrowseComp-Plus` HEAD.
2. Exact model identifier strings as sent to the provider, and the provider (Vertex AI, Bedrock, own GPU, other).
3. For Part B: the vLLM image tag, `gpt-oss-20b` weights revision, the judge model (`gpt-4.1`) and its date.
4. Run dates and times.
5. All settings: for Part B the flags in B6; for Part A the manifest rows (`max_examples`, `n_responses`) and any edits to `run*.yaml`.
6. Prices used, with the provider, per-million-token input and output values, and the date read.
7. The task-name to directory mapping you chose (A3), and the split you evaluated.
8. Total spend per model, from the provider's billing page.
9. The hand-written output-format mappings (A6, B8), kept with the converter you wrote.

---

## 7. Known gaps

Items marked CHECK in this file:
1. `uv` install and first-run environment creation (A2, prerequisite 2).
2. SSH access for the `software-agent-sdk` submodule (prerequisite 4).
3. Exact model identifiers and provider routing for Part A (A4, A5).
4. The `git submodule update` and `git clone` command forms (A1, A3).
5. Task name to directory and compose service mapping (A3).
6. `docker compose build` scope (A3).
7. Smoke-test and full-run manifest format, header row, and `--batch-dir` values (A4, A5).
8. Whether `run-baseline` runs the test split only (A5).
9. Disk size and GPU memory (prerequisites 7, 10).
10. Exact install lines and Java 21 setup for Part B (B1).
11. Exact decrypt command line (B2).
12. `pyserini` import check (B3).
13. Hosted endpoint support and `summary: detailed` (B4).
14. Smoke-test via a 20-line TSV (B5).
15. `--max-tokens` choice (B6).
16. Task-record field choices for CMU tasks (section 5).

Not documented in any fetched page:
- Part A: credentials and environment variables; model-name strings; per-instance output format; split handling; paper commit; `docs/` and `tasks/` contents.
- Part B: step limit, temperature, serving stack and run count in the paper; token, cost and time fields in run files; index building from scratch; first-N-queries flag.
- Both: disk requirements; actual spend.
