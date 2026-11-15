# Runbook: reproduce two published benchmarks and compare with omega-eval

Written 2026-11-15. Nothing in this file has been run, except section 0. Commands come from files in the two repositories at the pinned commits listed below. Each command block says where it came from as `path:lines`. A command that was changed is marked "Adapted:" and the change is stated. A line marked `CHECK:` is something that cannot be settled by reading files. It needs a run or an account. Each `CHECK:` is listed again in section 7.

Abbreviations: API = application programming interface. CMU = Carnegie Mellon University. BM25 = a keyword-ranking formula (Best Matching 25). JSON = JavaScript Object Notation. JSONL = JSON Lines (one JSON object per line). CSV = comma-separated values. TSV = tab-separated values. YAML = a configuration file format. LLM = large language model. LM = language model. SDK = software development kit. HF = Hugging Face. RPM = requests per minute. TPM = tokens per minute. GEPA = the optimizer the CMU paper uses to adapt harnesses (not needed here). `uv` = a Python package manager. LiteLLM = a Python library that sends one request format to many model providers. DSPy = a Python library for LLM programs (the CMU code builds its language models with it).

Decided on 2026-11-15: run all three CMU models; use OpenRouter for `gpt-oss-20b`; keep `gpt-4.1` as the BrowseComp-Plus judge. The commands below already follow these choices. Groq is kept in the B5 table for reference only.

## 0. Free check, already run: the authors' published records

This is the one part of the file that has been run. It makes no model calls.

The CMU replication package holds the authors' own per-example results in `CMU:replication_package/outputs/data/summary.json` (103 runs, 28,950 records). Loading their default-harness test records for the three models and comparing with the paper:

```bash
omega-eval results import-cmu <clone>/replication_package/outputs/data/summary.json --db authors.sqlite --registry authors_tasks.jsonl --model gemma-4-26b-a4b --model qwen3-coder-30b-a3b --model ministral-3-8b
omega-eval report validate --db authors.sqlite --registry authors_tasks.jsonl --references references.yaml
```

Result on 2026-11-15: 3,510 attempts over 390 test examples were imported, and all 21 CMU reference rows came out `consistent`, each within 0.05 percentage points of the published number. That confirms the importer, the split and the mean-score calculation. It says nothing about whether your own runs through a hosted API will match; only Part A answers that.

Keep the authors' records and your own runs in separate databases. They share model and harness names, so in one database the reports would mix them.

`scripts/cmu_headroom.py` reads the same file and counts, per task, how many test examples each model and the three-model pool solve in the default and the adapted harness.

## Sources used

Repositories (read locally, nothing executed):
- CMU suite: https://github.com/malusamayo/migration-analysis at commit `2d31e1791a68f89554aa81c97d58fd62a636f4f4` (2026-09-04). Paper: "Better Harnesses, Smaller Models", arXiv 2607.08938. Cited below as `CMU`. Its submodules `software-agent-sdk` (`89c21968922afc746bca8a653712038d5a38d6aa`) and `LOCA-bench` (`387b24e9511f1adfad51c7592be48b3fe1f74aa2`) were not fetched, so their source was not read.
- BrowseComp-Plus: https://github.com/texttron/BrowseComp-Plus at commit `046949032b0328319cc9a02663a759ec601d9402` (2026-05-28). Cited below as `BCP`. The clone includes the paper as `BrowseCompPlus_paper.pdf` (arXiv 2508.06600), which was read for Table 4.

Provider and library pages fetched on 2026-11-15. The fetch tool returns an extract of each page, so check a price on the page before you buy credits:
- https://openrouter.ai/google/gemma-4-26b-a4b-it
- https://openrouter.ai/qwen/qwen3-coder-30b-a3b-instruct
- https://openrouter.ai/mistralai/ministral-8b-2512
- https://openrouter.ai/google/gemini-3-flash-preview
- https://openrouter.ai/openai/gpt-oss-20b
- https://openrouter.ai/openai/gpt-4.1
- https://openrouter.ai/docs/api/reference/responses/overview
- https://openrouter.ai/docs/api_reference/responses/basic-usage.md
- https://openrouter.ai/docs/api_reference/responses/reasoning.md
- https://openrouter.ai/docs/api_reference/responses/error-handling.md
- https://openrouter.ai/docs/api_reference/limits.md
- https://console.groq.com/docs/models
- https://console.groq.com/docs/model/openai/gpt-oss-20b
- https://console.groq.com/docs/responses-api
- https://console.groq.com/docs/rate-limits
- https://docs.litellm.ai/docs/providers/openrouter
- https://docs.litellm.ai/docs/providers/gemini
- https://docs.litellm.ai/docs/providers/mistral
- https://ai.google.dev/gemini-api/docs/pricing
- https://docs.mistral.ai/getting-started/models/models_overview/
- https://developers.openai.com/api/docs/models/gpt-4.1
- https://github.com/openai/openai-python

Commands in this file come from the files at those commits. Nothing has been run.

---

## 1. What this reproduces and what it costs

Part A reproduces the default (unadapted) harness numbers from the CMU paper. Scope:
- Test split only. The split is 20/20/60 train/validation/test, by example position (section 3, step A7).
- Three runs per configuration (`n_responses=3`).
- Three models: `gemma-4-26b-a4b`, `qwen3-coder-30b-a3b`, `ministral-3-8b`.
- Seven tasks: attendance-auditing, budget-approval, stock-alert, anomaly-detection, playwright-testing, website-management, code-refactoring.
- Every model call goes to a hosted API. No model is served on your own hardware. The authors served `gpt-oss` with vLLM (`BCP:docs/oss.md:11-16`). That path is not used here.

Part B reproduces one BrowseComp-Plus configuration (arXiv 2508.06600): `gpt-oss-20b`, BM25 retrieval, high reasoning effort, all 830 queries, top 5 results per search each cut to 512 tokens, `gpt-4.1` as judge. Published accuracy: 21.08% (`BCP:BrowseCompPlus_paper.pdf`, Table 4, row oss-20B-high, BM25; the same row gives recall 31.98% and 26.87 search calls per query).

Cost, Part A. The command in section 3 runs all 100 examples per task (50 for website-management), not only the 60% test share. `run-baseline` has no option to skip the train and validation examples. The numbers below are the authors' own token counts (`CMU:replication_package/outputs/data/summary.json`, baseline rows, `full` split) multiplied by a price per million tokens. I summed them with a short Python script that only read the JSON. Your agents will use different token counts.

| Model | Price used (input / output per million tokens) | All examples | Test share only |
|---|---|---|---|
| gemma-4-26b-a4b | $0.15 / $0.60 (the paper's price, `CMU:replication_package/scripts/derive_tableau_data.py:48-51`) | $80.26 | $47.17 |
| gemma-4-26b-a4b | $0.014 / $0.041 (OpenRouter headline price, 2026-11-15) | $7.40 | not computed |
| qwen3-coder-30b-a3b | $0.15 / $0.60 | $158.93 | $97.71 |
| ministral-3-8b | $0.15 / $0.15 | $223.90 | $134.10 |
| Total at the paper's prices | | $463.09 | $278.98 |

The earlier figure of about $280 is the test share only. The full run costs about $463 at the paper's prices. The paper's cost column is token counts times these list prices, not a provider bill (`derive_tableau_data.py:81-94`).

Wall-clock time, Part A: `run-baseline` runs its manifest rows one after another (`CMU:run.py:2372-2374`), with 8 parallel workers inside each row (`agent_batch_size: 8` in the task `run*.yaml` files). At the authors' recorded latencies this is about 43 hours for all three models. Hosted-API speed will differ.

Cost, Part B. The repository holds no token counts for `gpt-oss-20b`. Rough bound from the code and the paper (my arithmetic, not a measurement): the paper reports a mean of 26.87 search calls per query. Each search can add up to 5 x 512 = 2,560 snippet tokens (`BCP:search_agent/oss_client.py:482-488`). The whole history is sent again on every turn (`oss_client.py:150-154`). That gives roughly 0.9 million input tokens per query, or about 0.75 billion for 830 queries. At $0.018 to $0.075 per million input tokens (section 4, step B4) that is about $13 to $56. Output tokens come on top; each request is capped at 10,000 (`oss_client.py:427-431`). The `gpt-4.1` judge adds at most 830 x 1,024 output tokens, which is $6.80 at $8 per million. Judge input tokens are not bounded by the code.

Stop-and-confirm points are marked **STOP** below. Do not pass one without deciding the spend is acceptable.

---

## 2. Prerequisites

1. `git`, `docker` and `docker compose`. Docker is needed because each CMU task runs its agent in a task container (`CMU:src/runner.py:759-790`) and webtest scoring runs `docker run` (`CMU:src/task_evals/webtest.py:201-214`).
2. `uv`. Install steps are on the page the BCP README links: https://docs.astral.sh/uv/getting-started/installation/ (`BCP:README.md:41-45`).
3. Python 3.14 for Part A (`CMU:.python-version`, `CMU:pyproject.toml` `requires-python = ">=3.14"`). `uv` can fetch it. The task images also use `python:3.14-slim` (`CMU:tasks/Dockerfile:3`).
4. Python 3.10 for Part B (`BCP:.python-version`), and Java 21 for the BM25 library (`BCP:README.md:55-65`).
5. For Part A, `uv` must also be able to find Python 3.11 when you score code-refactoring. `CMU:src/task_evals/refactorbench.py:6` runs `uv python find 3.11` when that module is imported. Adapted: `uv python install 3.11` is a standard `uv` command, not from the repository. It installs a Python that `uv python find` can then locate.
6. One OpenRouter account with credits. It covers all four CMU models and the evaluation model (section 3, step A3), and `gpt-oss-20b` for Part B. Sections 3 and 4 show the verified alternatives. Keep the key out of the repository. Put it in `.env` (see A3).
7. An OpenAI API key with access to `gpt-4.1` for the Part B judge (`BCP:docs/llm_as_judge.md:7-11`). The OpenAI model page marks `gpt-4.1` as Deprecated and gives no shutdown date (https://developers.openai.com/api/docs/models/gpt-4.1, fetched 2026-11-15).
8. A Hugging Face account. `BCP:README.md:25` says you may need `huggingface-cli login`. The search client also downloads the `Qwen/Qwen3-0.6B` tokenizer (`BCP:search_agent/oss_client.py:40`).
9. Disk space. Neither repository states sizes. Check the Hugging Face file sizes for `Tevatron/browsecomp-plus-indexes` before the download in step B4.
10. omega-eval installed, from the agentbus repository:

```bash
cd evals && pip install -e ".[dev]"
```

---

## 3. Part A: CMU business-task suite

### Task names used in this runbook

The paper's seven tasks map to these `task_id` values. The evidence is the table-building script, `CMU:replication_package/scripts/derive_tableau_data.py:11-28`. Its `TASK_ORDER` list holds these seven ids and its `TASK_LABELS` gives the column names (Attn., Budget, Stock, Anom., Playwr., Web Mgmt., Refact.). The same ids are the only baseline rows in `CMU:replication_package/data/curated_run_eval_inputs.json`. I checked that the baseline gemma values in `replication_package/outputs/data/selected_test_rows.json` (91.7, 75.0, 5.0, 5.0, 9.9, 1.1, 32.2) equal the published table `replication_package/outputs/tables/results_table.md`.

| Paper task (`family` in omega-eval) | `task_id` | Data file (100 rows, 50 for webarena) | `prompt_name` in the paper's runs | `max_examples` |
|---|---|---|---|---|
| attendance-auditing | `attendance_payroll_audit_s2l_high` | `data/attendance_payroll_audit_s2l_high.json` | `default` | 100 |
| budget-approval | `budget_approval_s2l_high` | `data/budget_approval_s2l_high.json` | `default` | 100 |
| stock-alert | `woocommerce_stock_alert_s2l` | `data/woocommerce_stock_alert_s2l.json` | `default` | 100 |
| anomaly-detection | `machine_operating_s2l` | `data/machine_operating_s2l.json` | `default` | 100 |
| playwright-testing | `webtest` | `data/webtest.json` | `static` | 100 |
| website-management | `webarena` | `data/webarena_shopping_admin_easy.json` | `shopping_admin` | 50 |
| code-refactoring | `refactorbench` | `data/refactorbench.json` | `default` | 100 |

Attendance and budget use the `_high` variant. The `_medium` and `_extra_high` variants appear in `CMU:replication_package/outputs/data/included_task_model_seed_triplets.csv` only as an extra check for one model (`coverage_group` = `extra_variant_seed_pool`, `qwen3-coder-30b-a3b`). Prompt names and counts come from the curated baseline runs (`curated_run_eval_inputs.json`, `run_meta.prompt_name`, `max_examples`, `n_responses`). Each baseline run in that file has `agent_file` = null and `rollout_version` = `baseline`. That is the default harness. `run-baseline` removes any `agent_file` from the task config unless the manifest names one (`CMU:run.py:730-733`), so the `agent_file` lines in some `run.yaml` files (for example `tasks/woocommerce_stock_alert_s2l/run.yaml`) do not apply.

### A1. Clone and pin

```bash
git clone https://github.com/malusamayo/migration-analysis
cd migration-analysis
git checkout 2d31e1791a68f89554aa81c97d58fd62a636f4f4
git -c url."https://github.com/".insteadOf="git@github.com:" submodule update --init --recursive
```

The clone and checkout are standard git. The submodule line is standard git, not from the repository. The repository lists the `software-agent-sdk` submodule with an SSH address (`CMU:.gitmodules:1-3`). The `-c url...insteadOf` option makes git use HTTPS instead. `LOCA-bench` already uses HTTPS (`CMU:.gitmodules:4-6`). The two submodule commits should be `89c2196...` and `387b24e...`. `git submodule status` shows them.

### A2. Install

```bash
uv sync
```

Adapted: `uv sync` is a standard `uv` command, not in the README. The README only shows `uv run` forms (`CMU:README.md:43,74,109`). Run `uv sync` first because the task Dockerfiles copy `uv.lock` (`CMU:tasks/Dockerfile:31`, `tasks/Dockerfile.loca:31`) and `.gitignore` excludes `uv.lock` (`CMU:.gitignore`). `uv sync` creates it. It needs the submodules from A1, because `pyproject.toml` installs the SDK from `software-agent-sdk/` (`CMU:pyproject.toml`, `[tool.uv.sources]`).

CHECK: I did not run the install. Whether all dependencies resolve on Python 3.14 can only be settled by running `uv sync`.

### A3. Model configuration (hosted APIs only)

How the code finds models. `CMU:src/utils.py:152` loads `configs/models.yaml` when `src.utils` is imported. `configs/` is in `.gitignore` and is not in the repository, so you write the file. `load_lmdict` (`utils.py:100-129`) reads a `models:` list. Each entry has `name` (the model name used in manifests), `model` (passed to `dspy.LM`, which uses LiteLLM model identifiers) and any extra keyword fields. A string value written as `${VAR}` is read from the environment variable `VAR` (`utils.py:118-120`). An unset variable becomes `None` without an error. `load_dotenv(override=True)` runs just before (`utils.py:151`), so a `.env` file works. `build_sdk_llm` (`utils.py:139-148`) passes `model`, `api_base`, `api_key`, `input_cost_per_token` and `output_cost_per_token` to the OpenHands SDK `LLM`. Every other SDK setting keeps its default. `CMU:docs/sdk_reference_details/01_llm.md:7-43` lists those defaults, among them `temperature = None`, `reasoning_effort = "high"`, `native_tool_calling = True` and `drop_params = True`. The SDK wraps LiteLLM (`01_llm.md:5`).

Every task needs an evaluation model entry. `CMU:src/evaluate.py:94-97` raises an error if a task config names an `eval_lm` that is not in `models.yaml`, and all seven `run*.yaml` files name `gemini-3-flash-preview`. Only webtest strictly requires one (`CMU:src/task_setups/__init__.py:430-432`). Where it matters for scores: webarena uses it for fuzzy answer matching (`CMU:src/task_evals/webarena.py:152-177,203-208`); budget-approval uses it only as a fallback when a string match fails (`CMU:src/task_setups/budget_atoms.py:584-596`); webtest and refactorbench use it only to shorten failure text (`task_evals/webtest.py:105-114`, `task_evals/refactorbench.py:62-84`).

Verified hosted routes (all fetched 2026-11-15). LiteLLM names OpenRouter models with the prefix `openrouter/` and reads `OPENROUTER_API_KEY` (https://docs.litellm.ai/docs/providers/openrouter).

| Model name | LiteLLM `model` value | What the page showed (per million tokens, input / output) |
|---|---|---|
| `gemma-4-26b-a4b` | `openrouter/google/gemma-4-26b-a4b-it` | OpenRouter page for slug `google/gemma-4-26b-a4b-it`: headline $0.014 / $0.041. Provider rows run from $0.018 / $0.053 (Prime Intellect) to $0.15 / $0.60 (Google Vertex); 14 providers. The Gemini API pricing page lists Gemma 4 as free of charge with no paid tier (https://ai.google.dev/gemini-api/docs/pricing). |
| `qwen3-coder-30b-a3b` | `openrouter/qwen/qwen3-coder-30b-a3b-instruct` | One provider, Amazon Bedrock: $0.15 / $0.60. |
| `ministral-3-8b` | `openrouter/mistralai/ministral-8b-2512` | Page title "Mistral: Ministral 3 8B 2512". Provider Mistral: $0.150 / $0.150. The EU in-region row is $0.165 / $0.165. |
| `gemini-3-flash-preview` (evaluation model) | `openrouter/google/gemini-3-flash-preview` | Google AI Studio and Google Vertex rows: $0.50 / $3.00. The Gemini API page shows the same standard price. |

Not verified:
- Groq. Its model list shows none of the three CMU models (https://console.groq.com/docs/models). It lists `openai/gpt-oss-20b`, which matters for Part B only.
- Mistral's own API for Ministral 3 8B. The Mistral models page gives no API model name for it, and the LiteLLM Mistral page lists no Ministral identifier. The OpenRouter row for Mistral shows the model exists on that API. I did not find its API name.
- The paper's own providers. The repository names none. Its prices equal the OpenRouter rows for Google Vertex (Gemma) and Amazon Bedrock (Qwen).

Put the key in `.env` at the repository root (`.env` is in `.gitignore`). The code forwards the names found in `.env` into task containers (`CMU:src/docker_utils.py:16-27`).

```text
OPENROUTER_API_KEY=<your OpenRouter key>
```

Create `configs/models.yaml`. This is a new file you write. The schema is `load_lmdict` (`utils.py:100-129`) and the fields `build_sdk_llm` reads. The cost values are the paper's prices (`derive_tableau_data.py:35-52`), so the `cost` field in your results compares with the paper's cost column. Gemma is cheaper on OpenRouter; use its row price instead if you want the cost field to match your bill. Per-token cost is dollars per million tokens divided by 1,000,000.

```yaml
models:
  - name: gemma-4-26b-a4b
    model: openrouter/google/gemma-4-26b-a4b-it
    api_key: ${OPENROUTER_API_KEY}
    input_cost_per_token: 0.00000015
    output_cost_per_token: 0.0000006
  - name: qwen3-coder-30b-a3b
    model: openrouter/qwen/qwen3-coder-30b-a3b-instruct
    api_key: ${OPENROUTER_API_KEY}
    input_cost_per_token: 0.00000015
    output_cost_per_token: 0.0000006
  - name: ministral-3-8b
    model: openrouter/mistralai/ministral-8b-2512
    api_key: ${OPENROUTER_API_KEY}
    input_cost_per_token: 0.00000015
    output_cost_per_token: 0.00000015
  - name: gemini-3-flash-preview
    model: openrouter/google/gemini-3-flash-preview
    api_key: ${OPENROUTER_API_KEY}
    input_cost_per_token: 0.0000005
    output_cost_per_token: 0.000003
```

Set the cost keys. If they are left out, the cost that the SDK records is not defined by anything in this repository. Do not load a cost of 0 into omega-eval as if it were known.

The authors' own `models.yaml` is not in the repository, so their temperature, token limits and chosen backend providers are unknown (section 7). OpenRouter routes each request to one of several backends and this file does not pin one.

CHECK: whether tool calling works for these three models through OpenRouter. The Gemma page says "native function calling". The Qwen page implies it. The Ministral page does not say. The smoke test in A5 settles it.

### A4. Task containers and per-task setup

What is needed per task. Images are built from `CMU:docker-compose.yml`. The compose file builds `tasks/Dockerfile` for `webtest`, `refactorbench`, `webarena` and `attendance_payroll_audit_s2l`. It builds `tasks/Dockerfile.loca` for `machine_operating_s2l`, `woocommerce_stock_alert_s2l` and `budget_approval_s2l`. `Dockerfile.loca` also copies `LOCA-bench/mcp_convert` into the image (`tasks/Dockerfile.loca:33`). The image tag is `<service>:latest`, which is the `server_image` in each task `run*.yaml`.

| Task | Compose service | Dockerfile | Extra setup |
|---|---|---|---|
| attendance-auditing | `attendance_payroll_audit_s2l` | `tasks/Dockerfile` | none documented |
| budget-approval | `budget_approval_s2l` | `tasks/Dockerfile.loca` | LOCA-bench submodule |
| stock-alert | `woocommerce_stock_alert_s2l` | `tasks/Dockerfile.loca` | LOCA-bench submodule |
| anomaly-detection | `machine_operating_s2l` | `tasks/Dockerfile.loca` | LOCA-bench submodule |
| playwright-testing | `webtest` | `tasks/Dockerfile` | scoring runs `docker run ... webtest:latest` (`task_evals/webtest.py:201-214`) |
| website-management | `webarena` | `tasks/Dockerfile` | a running WebArena shopping-admin site (below) |
| code-refactoring | `refactorbench` | `tasks/Dockerfile` | RefactorBench clone, path rewrite (below), Python 3.11 for scoring |

Build the images. The compose file's own error text suggests `UID=$(id -u) docker compose build` (`CMU:docker-compose.yml`, the `USER_UID` lines). In bash that assignment fails, because bash treats `UID` as read-only; I tried it in a scratch bash shell and got "UID: readonly variable". Adapted: export it instead. I only checked bash.

```bash
export UID
docker compose build webtest refactorbench webarena machine_operating_s2l woocommerce_stock_alert_s2l budget_approval_s2l attendance_payroll_audit_s2l
```

Adapted: `docker compose build` with seven service names. The names are from `docker-compose.yml`; naming services is standard Docker Compose syntax. Without names it also builds `docbench`, `browsecomplongcontext` and `tau2_airline`, which the seven tasks do not use.

Mounting `.vertex-ai.json`. `CMU:src/runner.py:198` adds a bind mount of `.vertex-ai.json` to every task container with no check that the file exists. The same happens in `task_evals/webtest.py:198`. Other code does check (`runner.py:82-85`). I use no Google credentials file.

CHECK: whether a task container starts when `.vertex-ai.json` is missing. It depends on how the SDK's `DockerWorkspace` passes volumes to Docker, and that source was not read. If it fails, create an empty file named `.vertex-ai.json` in the repository root (it is in `.gitignore`). `CMU:src/utils.py:15-22` would then set `GOOGLE_APPLICATION_CREDENTIALS` to it. That only matters for Google sign-in, which the routes above do not use.

LOCA-bench tasks (budget, stock, anomaly). The README says: "For other tasks, set up environment following the custom build of LOCA-bench" (`CMU:README.md:102`). The submodule is `https://github.com/malusamayo/LOCA-bench`. The scoring code imports from it on the host (`CMU:src/task_evals/woocommerce_stock_alert_s2l.py:11-17,56-61`). The repository gives no further steps and the submodule was not fetched. CHECK: what the LOCA-bench custom build needs on the host. Read its own README after A1.

WebArena (website-management). `CMU:README.md:101` says to set up webarena-verified first (https://github.com/ServiceNow/webarena-verified). The task config says the servers must already run, and gives the start command:

```bash
uvx webarena-verified env start --site shopping_admin
```

Source: `CMU:tasks/webarena/run.yaml:21-23`. The config sets `start_servers: false` (line 27), and says the agent reaches the site at `localhost:7780/admin` (lines 24-26; ports in `src/task_setups/webarena_servers.py:12-19`). The container images the code expects are named in `webarena_servers.py:34-39`.

RefactorBench (code-refactoring). `CMU:README.md:100` says to clone the source repository and point the data file at it.

```bash
git clone https://github.com/microsoft/RefactorBench
```

Adapted: the clone command is built from the link in the README. Each row of `data/refactorbench.json` has `repo_path` and `problem_path` values on the authors' disk, for example `/mnt/data_4tb/datasets/RefactorBench/repositories/salt_refactor`. `CMU:src/task_setups/refactorbench.py:11-14` copies `repo_path` into the workspace. Nothing in the repository rewrites these. Edit your copy of `data/refactorbench.json` so the prefix `/mnt/data_4tb/datasets/RefactorBench` becomes the path of your clone. The data also gives `repo_url` for each repository, for example `https://github.com/dhruvji/salt_refactor`.

CHECK: whether your RefactorBench clone has the folders `repositories/<repo_name>` and `problems/...` that the data paths assume. The RefactorBench repository was not read.

### A5. Smoke test

**STOP.** This spends money on OpenRouter. At the paper's per-query cost for this task and model ($0.058, table row "Anom.", `results_table.md`), 10 queries cost under $1.

`run-baseline` reads a manifest table. The README example is `CMU:README.md:104-116`. Its arguments are (`CMU:run.py:2372-2379`):
- `--manifest`: path to the table, or `-` for standard input. Default `-`.
- `--batch-dir`: folder for this batch. It must not exist yet (`run.py:551-556`). Default is a timestamp under `generated/baseline_batches/`.
- `--yes`: skip the confirmation question "Launch these baseline runs now?" (`run.py:2172`).

Manifest columns (`CMU:run.py:40-51`, header names normalised at `run.py:63-100,230-232`):
- Required: `task_id`, `model_name`, `max_examples`.
- Optional: `rollout_version`, `n_responses`, `prompt_name`, `agent_file`.
- `task_id`: a folder name under `tasks/`, or a folder name plus a variant suffix such as `_high` (lookup in `run.py:478-501`). The file `tasks/<task>/run.yaml` or `run_<variant>.yaml` supplies every other setting.
- `model_name`: the `name:` of an entry in `configs/models.yaml`.
- `max_examples`: the first N rows of the data file are used, in order, with no shuffling (`CMU:src/dataloader.py:44`).
- `n_responses`: runs per example (the paper's three runs). If the column is missing, the value in `run.yaml` is used (`run.py:710-712`).
- `rollout_version`: names the output folder. Default `baseline` (`run.py:677-687`).
- Always include the header row. Without it, the columns are read by position, and a fourth column would be taken as `rollout_version`, not `n_responses` (`run.py:379-389`).

Separators: tabs, or commas when a line has at least two commas (`CMU:run.py:244-252`). The README uses tabs.

```bash
mkdir -p manifests
cat > manifests/smoke.csv <<'EOF'
task_id,model_name,max_examples,n_responses,rollout_version
machine_operating_s2l,gemma-4-26b-a4b,10,1,smoke
EOF
uv run python run.py run-baseline --manifest manifests/smoke.csv --batch-dir generated/baseline_batches/smoke
```

Adapted from `CMU:README.md:109-115`. Changes: (1) the manifest is a CSV file with commas instead of a tab-separated heredoc, which `run.py:250-251` accepts and which survives copy and paste; (2) a `rollout_version` column of `smoke`, so the output goes to `rollouts/smoke` and does not mix with the later `baseline` run; (3) `--yes` is dropped so you see the prepared batch and confirm; (4) the batch folder is named `smoke`. The task, model, 10 examples and 1 response are the README's.

What happens: for each row the launcher runs `uv run python -m src.collect --config <config>` and then `uv run python -m src.evaluate --config <config>`, one row at a time (`CMU:run.py:1789-1853,1856-1875`).

Check afterwards:
- `generated/baseline_batches/smoke/batch.json`, `configs/`, `logs/` and `launch_results.json`. Every row in `launch_results.json` should have `"status": "completed"`.
- `results/machine_operating_s2l/gemma-4-26b-a4b_default/rollouts/smoke/run.json` and `eval_results.yaml` (section 3, step A8).
- The `*_collect.log` files in `logs/`: look for provider or tool-calling errors.
- Per-query cost close to the paper's, and a `score` for every example.

Do not go on until all four hold.

### A6. Full default-harness runs

**STOP.** About $463 at the paper's prices across the three models (section 1), about 43 hours. Confirm the budget and your OpenRouter credit first.

One manifest per model. Gemma first:

```bash
cat > manifests/gemma_full.csv <<'EOF'
task_id,model_name,max_examples,n_responses
attendance_payroll_audit_s2l_high,gemma-4-26b-a4b,100,3
budget_approval_s2l_high,gemma-4-26b-a4b,100,3
woocommerce_stock_alert_s2l,gemma-4-26b-a4b,100,3
machine_operating_s2l,gemma-4-26b-a4b,100,3
webtest,gemma-4-26b-a4b,100,3
webarena,gemma-4-26b-a4b,50,3
refactorbench,gemma-4-26b-a4b,100,3
EOF
uv run python run.py run-baseline --manifest manifests/gemma_full.csv --batch-dir generated/baseline_batches/gemma_full
```

Adapted from `CMU:README.md:109-115,127-130` (100 examples and 3 responses for 100-example tasks, 50 examples for WebArena). Changes: CSV file manifest, no `--yes` so you confirm after the table is printed, and one row per task. Repeat with `qwen3-coder-30b-a3b` and `ministral-3-8b` in the model column, with new `--batch-dir` names. Do one model at a time and check your OpenRouter spend after each.

Notes:
- Output goes to `results/<task_id>/<model_name>_<prompt_name>/rollouts/baseline/` (`CMU:src/collect.py:167`; `run.py:715-722`). Prompt names come from the task config (`static` for webtest, `shopping_admin` for webarena).
- Re-running. A batch folder cannot be reused. For `machine_operating_s2l`, `attendance` and the other tasks with `resume: true`, a repeat run reuses traces already saved (`CMU:src/dataloader.py:405-430`). `webtest` and `webarena` have `resume: false` (`tasks/webtest/run.yaml:16`, `tasks/webarena/run.yaml:12`) and start over.
- Do not run `run-baseline` twice for the same task and model with the same `rollout_version` and expect two samples. Both write to the same folder.

### A7. How the 20/20/60 split works, and getting test-split results

The split is not stored in the data files and `run-baseline` knows nothing about it. It is applied later, by position, in `CMU:replication_package/scripts/collect_run_data.py:17,85-112`:
- Weights are `{"train": 2, "val": 2, "test": 6}`.
- For N examples: train = `N*2//10`, val = `N*2//10`, test = the rest.
- Ranges by `example_id` (the row's position in the data file): train `0 .. train-1`, val next, test last.
- N = 100: train 0-19, val 20-39, test 40-99 (60 examples). N = 50 (website-management): train 0-9, val 10-19, test 20-49 (30 examples).
- Each test example has three runs, so 180 records per task (90 for website-management). `replication_package/outputs/data/selected_test_rows.json` shows `records` = 180 and `examples` = 60 for the baseline rows, and 90 and 30 for webarena.

So `run-baseline` always runs the train and validation examples too (they are the first rows). The paper's number for a task and model is the mean `score` over its test records, times 100 (`collect_run_data.py:170,181`; `derive_tableau_data.py:119`). If several baseline versions existed for one task and model, the paper keeps the one with the best validation score (`derive_tableau_data.py:61-78`). The curated list has one baseline per cell.

To get test-split results from your runs, use the replication script on your batch folders.

```bash
printf '%s\n' generated/baseline_batches/gemma_full > generated/source_batches.txt
uv run --project replication_package python replication_package/scripts/collect_run_data.py --source-batches generated/source_batches.txt --output-dir generated/summary_gemma
```

Adapted from `CMU:README.md:74` and the arguments at `collect_run_data.py:491-503` (`--source-batches` is a text file with one batch folder per line, inside the repository; `--output-dir` keeps the shipped `summary.json` safe). The `printf` line is my own. List one line per batch folder you want in the summary.

It writes `summary.json`, `summary.tsv` and `records.tsv` (`collect_run_data.py:535-543`). In `summary.json`, `rows[].test.score` is the test-split mean (a fraction, multiply by 100), and `rows[].val.score` and `rows[].train.score` are the other two splits. `records.tsv` has one line per example and run, with a `split` column. It needs `batch.json`, `launch_results.json`, `run.json` and `eval_results.yaml` for each run, all of which `run-baseline` writes.

CHECK: the script reads `run_row["run_result"]["metrics"]` for every row (`collect_run_data.py:248-251`). The runner returns a row without `run_result` when an agent run fails before a trace is saved (`CMU:src/runner.py:623-625`). I expect the script to stop with a `KeyError` for such a row. That can only be seen with a failing run.

### A8. Output format and mapping to omega-eval

Per run folder `results/<task_id>/<model>_<prompt>/rollouts/<rollout_version>/`:
- `run.json`: a JSON list written by `src/collect.py:209-233`. Each item is one data row plus `example_id` and `rollout_id` (`src/dataloader.py`), plus `run_result` (`src/runner.py:553-630`). `run_result` holds `conversation_id`, `eval_output`, `events`, `metrics`, `metrics_breakdown`, `subagents` and `error`.
- Inside `run_result.metrics`: `accumulated_cost` (dollars, computed from `input_cost_per_token` and `output_cost_per_token`), `accumulated_token_usage` (`prompt_tokens`, `completion_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`) and `response_latencies` (a list, each with `latency`). Source: how `collect_run_data.py:248-268` reads them.
- Time per run: last event timestamp minus first event timestamp (`collect_run_data.py:143-144`).
- `eval_results.yaml`: a YAML list written by `src/evaluate.py:107-127`. Each item has `workspace_dir` (ends with `example<i>_rollout<j>`) and `score`. Other fields depend on the task: `feedback` always; `success`, `tests_passed`, `tests_failed`, `tests_total` for webtest; `success` for refactorbench (`task_evals/*.py`).
- Per-run trace: `example<i>_rollout<j>_logs/trace_<conversation_id>.json` next to the workspace (`src/dataloader.py:405-412`, `src/runner.py:526-535`).

Scores are fractions between 0 and 1, not pass or fail:
- Fractional: attendance-auditing (weighted checkpoints, `task_evals/attendance_payroll_audit_s2l.py:42-57`), budget-approval (weighted checkpoints, `task_evals/budget_approval_s2l.py:60-96`) and playwright-testing (tests passed over expected tests, `task_evals/webtest.py:72-86`).
- 0 or 1: stock-alert (`task_evals/woocommerce_stock_alert_s2l.py:73,84`), anomaly-detection (1 only when precision and recall are both at least 0.95, `task_evals/machine_operating_s2l.py:169`), code-refactoring (`task_evals/refactorbench.py:168`) and website-management (string match, `task_evals/webarena.py:186-217`).
- The paper's percentage is the mean score over test records. It is not a pass rate.

omega-eval stores both. Load `score` as the record's score and `passed` as `score == 1.0`. `report success` and `report validate` average `score`, which is the paper's quantity for all seven tasks (for the four 0-or-1 tasks it equals the pass rate). `report compare` uses `passed` only, so on attendance, budget and playwright a partial score counts as not solved there.

Field mapping from `records.tsv` (columns at `collect_run_data.py:252-275,452-475`) to omega-eval result fields:

| omega-eval field | Source |
|---|---|
| `task_id` | your own id, `<family>-ex<example_id padded to 3 digits>`, for example `stock-alert-ex057`. `data/*.json` row ids repeat across variants, so do not use them. |
| `attempt` | `rollout_id` (0, 1, 2) |
| `score` | `score` (a fraction from 0 to 1) |
| `passed` | `score == 1.0` |
| `cost_usd` | `cost` (leave out if the cost keys were not set in `models.yaml`) |
| `input_tokens` | `prompt_tokens` |
| `output_tokens` | `completion_tokens` |
| `wall_seconds` | `end_to_end_latency_sec` |
| `trajectory` | `<workspace_dir without trailing slash>_logs/trace_<conversation_id>.json` |
| `source` | the run folder (`.../rollouts/baseline`) |
| `harness_version` | `2d31e1791a68f89554aa81c97d58fd62a636f4f4` |

Fixed values: `arm` = `solo`, `harness` = `openhands-sdk-default`, `run_set` = `screening`, `model` = the model name, `settings` = `{"max_examples": 100, "n_responses": 3, "prompt_name": "<prompt>", "rollout_version": "baseline", "split": "test"}`. Load test-split rows only (`split == test`).

Do not write your own converter. Run `omega-eval results import-cmu` (Carnegie Mellon University, CMU) on the `summary.json` that step A7 produces. It reads the per-example records inside `summary.json`, applies the mapping above (with three differences: `trajectory` is the workspace folder, `source` is the path of `summary.json`, and `harness_version` and the `max_examples` and `n_responses` settings are not set), writes the task records into the registry and adds the attempts to the results database. Section 5 shows the command. `summary.json` has no `cache_read_tokens` column or conversation id, so those are not imported; omega-eval has no field for them either.

---

## 4. Part B: BrowseComp-Plus

### B1. Clone and pin

```bash
git clone https://github.com/texttron/BrowseComp-Plus
cd BrowseComp-Plus
git checkout 046949032b0328319cc9a02663a759ec601d9402
```

Standard git, with the repository address from `BCP:README.md:3` and the commit pinned above. The licence is MIT (`BCP:LICENSE:1`).

### B2. Install

From `BCP:README.md:47-53`:

```bash
uv sync
source .venv/bin/activate
uv pip install --no-build-isolation flash-attn # Needed for faiss
```

The README's comment says the `flash-attn` line is needed for FAISS (a vector-search library), which the BM25 path does not use. But `search_agent/oss_client.py:21` imports `searcher.searchers`, and its `__init__.py:8-12` imports the FAISS module at import time. That module imports `faiss`, `torch` and `tevatron` (`searcher/searchers/faiss_searcher.py:9-17`). `uv sync` installs them. No file in the repository imports `flash-attn`. `uv sync` also installs `vllm` and `deepspeed` (`BCP:pyproject.toml`), which is a large install even though no model is served here. CHECK: whether the `flash-attn` line is needed for BM25. Try without it first. It needs a GPU toolchain to build.

Java 21 (`BCP:README.md:57-65`), either of:

```bash
conda install -c conda-forge openjdk=21
```

```bash
sudo apt update
sudo apt install -y openjdk-21-jdk
```

### B3. Data

From `BCP:README.md:20-25`:

```bash
pip install datasets # ensure you have datasets
huggingface-cli login
python scripts_build_index/decrypt_dataset.py --output data/browsecomp_plus_decrypted.jsonl --generate-tsv topics-qrels/queries.tsv
```

The script has the decryption password built in (`BCP:scripts_build_index/decrypt_dataset.py:35`), so no key is needed. It loads `Tevatron/browsecomp-plus` (`decrypt_dataset.py:58`) and writes the JSONL and a TSV with no header and two columns, `query_id` and `query` (`decrypt_dataset.py:44-48`). The corpus is not encrypted and the search code loads it itself (`BCP:README.md:27-33`). Check that the TSV has 830 lines:

```bash
wc -l topics-qrels/queries.tsv
```

Standard shell command. Both output files are in `BCP:.gitignore`.

### B4. Keyword (BM25) index

From `BCP:scripts_build_index/download_indexes.sh:1` (the BM25 line only):

```bash
huggingface-cli download Tevatron/browsecomp-plus-indexes --repo-type=dataset --include="bm25/*" --local-dir ./indexes
```

This puts the index at `indexes/bm25/`, the `--index-path` used in `BCP:docs/oss.md:21`. The BM25 searcher opens it with Pyserini's `LuceneSearcher` and sets no BM25 parameters (`BCP:searcher/searchers/bm25_searcher.py:12-35`).

### B5. Hosted route for `gpt-oss-20b`

The client is `search_agent/oss_client.py`. `BCP:docs/oss.md:19-23` names it for the OSS models, with a vLLM server. This runbook points it at a hosted API instead.

What the client sends. It calls the OpenAI Responses API: `client.responses.create(**request)` (`oss_client.py:152`). The request is built at `oss_client.py:364-373` (and again at `542-549` for a single query):

```python
initial_request = {
    "model": args.model,
    "max_output_tokens": args.max_tokens,
    "input": [{"role": "user", "content": format_query(qtext, args.query_template)}],
    "tools": tool_handler.get_tool_definitions(),
    "truncation": "auto",
    "reasoning": {"effort": args.reasoning_effort, "summary": "detailed"},
}
```

The tools are flat function tools with `"strict": True` (`oss_client.py:51-69`). No temperature, top-p or tool choice is sent.

Verified hosted routes (fetched 2026-11-15). Both serve `gpt-oss-20b` and both offer a Responses API.

| | OpenRouter | Groq |
|---|---|---|
| Base URL for `--model-url` | `https://openrouter.ai/api/v1` (Responses endpoint `/api/v1/responses`) | `https://api.groq.com/openai/v1` (endpoint `/responses`) |
| Value for `--model` | `openai/gpt-oss-20b` | `openai/gpt-oss-20b` |
| Price per million tokens (input / output) | headline $0.018 / $0.09. Rows: Darkbloom and AkashML $0.018 / $0.090; Groq $0.075 / $0.300; Amazon Bedrock $0.070 / $0.150; 11 rows | $0.075 (cached $0.037) / $0.30 |
| Reasoning effort | `reasoning.effort` documented with `minimal`, `low`, `medium`, `high` | `reasoning: { effort: ... }` documented; page lists low, medium, high for this model |
| State | stateless; `store: true` and `previous_response_id` are rejected with HTTP 400 | stateless; `previous_response_id`, `store`, `truncation`, `include`, `safety_identifier`, `prompt_cache_key`, `prompt` are listed as not yet supported; the API is in beta |
| Request limits | paid models: no platform-level request cap | listed for this model: 30 requests per minute, 1,000 per day, 8,000 tokens per minute, 200,000 tokens per day (one row for "Free Plan Limits / Developer Plan Limits") |

The client sends `truncation` and `summary: "detailed"` and flat `strict` tools. Neither provider documents `summary`. OpenRouter does not document `truncation` or `strict`. Groq lists `truncation` as unsupported and does not document `strict`. CHECK: whether either provider accepts, ignores or rejects these three fields. This needs a call with a key.

On the fetched limits, Groq's 8,000 tokens per minute is smaller than one request late in a long search (each search can add 2,560 snippet tokens). Use OpenRouter unless you have a higher Groq tier, which this runbook did not verify.

The client hard-codes the API key. `BCP:search_agent/oss_client.py:508-511` builds `openai.OpenAI(...)` with `base_url=args.model_url` and, on line 510, a fixed placeholder string (the word EMPTY) as the key. There is no flag for a key.

The smallest local change is to line 510, applied in your own clone (`os` is already imported at `oss_client.py:6`). Replace the quoted placeholder with a read from the environment, so the line's value becomes:

```python
os.environ.get("MODEL_API_KEY", "EMPTY")
```

`MODEL_API_KEY` is a name I chose. With the variable unset the client behaves as before.

I used a separate variable and not `OPENAI_API_KEY`, because the judge in step B8 needs a real OpenAI key in `OPENAI_API_KEY`.

Can `search_agent/openai_client.py` be pointed at another base URL without code changes? It builds `openai.OpenAI(api_key=api_key)` with no base URL (`openai_client.py:616-620`) and requires `OPENAI_API_KEY`. The `openai-python` README names the `OPENAI_BASE_URL` environment variable (https://github.com/openai/openai-python, fetched 2026-11-15). So setting `OPENAI_API_KEY` to the provider key and `OPENAI_BASE_URL` to the base URL should work. CHECK: that the installed `openai` version reads `OPENAI_BASE_URL` for the default client. The README text I fetched showed it only in examples.

How the two clients differ, to decide which is closer to the paper's open-model runs:
- `BCP:docs/oss.md:19-23` documents `oss_client.py` for the OSS models, not `openai_client.py` (`docs/openai.md` covers `o3` and similar). I take `oss_client.py` as the closer one.
- Output budget. `oss_client.py` sends `max_output_tokens` (default 10,000) on every request. `openai_client.py` treats it as one budget for the whole conversation, subtracts the output used so far, and stops when it is spent (`openai_client.py:171-199`). `docs/openai.md` says verbose models need 40,000.
- Extra fields. `openai_client.py` also sends `"tool_choice": "auto"`, and optional `temperature`, `top_p` and `instructions` (`openai_client.py:140-169`). `oss_client.py` sends none of these.
- Errors. `openai_client.py` lets an API error stop the query. `oss_client.py` hides errors (see the warning below).
- Saved fields. `openai_client.py` saves a `usage` block with token counts (`openai_client.py:353-380`). `oss_client.py` does not (step B9).

Client defaults (argparse, `BCP:search_agent/oss_client.py:418-494`). The paper does not report them. These are the values a run without extra flags uses:

| Setting | Flag | Default | Lines |
|---|---|---|---|
| Maximum conversation rounds | `--max-iterations` | 100 | 449-453 |
| Maximum output tokens per request | `--max-tokens` | 10000 | 426-431 |
| Search results per query (k) | `--k` | 5 | 487-489 |
| Snippet length in tokens (Qwen3-0.6B tokenizer) | `--snippet-max-tokens` | 512 | 481-486; tokenizer at 40 |
| Threads | `--num-threads` | 1 | 445-447 |
| Query template | `--query-template` | `QUERY_TEMPLATE_NO_GET_DOCUMENT` | 435-444 |
| `get_document` tool | `--get-document` | off | 490-494 |
| Reasoning effort | `--reasoning-effort` | `high` | 455-460 |
| Server URL | `--model-url` | `http://localhost:8000/v1` | 461-463 |
| Model | `--model` | `openai/gpt-oss-20b` | 423-425 |
| Query file | `--query` | `topics-qrels/queries.tsv` | 419-422 |
| Temperature, top-p | none | not sent; the server's default applies | request at 364-373 |

**Warning, from `oss_client.py:155-160`.** If a request fails for any reason (a wrong key, a rejected field, a rate limit), the client does not stop. With `--verbose` it prints the error. Either way it adds one to the round counter and tries again at once, with no wait. After 100 failures it returns the status `incomplete` and saves a run file. A later run skips every query that has a saved run file (`oss_client.py:338-350`). A bad key therefore produces 830 useless run files in seconds. Always smoke-test with `--verbose`, and delete the output folder after any failed attempt.

### B6. Smoke test

**STOP.** This spends money on OpenRouter. Five queries at the rough bound in section 1 cost under $0.30.

```bash
head -n 5 topics-qrels/queries.tsv > smoke_queries.tsv
export MODEL_API_KEY=<your OpenRouter key>
python search_agent/oss_client.py --model openai/gpt-oss-20b --model-url https://openrouter.ai/api/v1 --reasoning-effort high --output-dir runs/smoke/oss-20b --searcher-type bm25 --index-path indexes/bm25/ --num-threads 1 --snippet-max-tokens 512 --k 5 --query smoke_queries.tsv --verbose
```

Adapted from `BCP:docs/oss.md:21`. Changes: `--model` set to the 20b model as `oss.md:23` says; `--model-url` added (`oss_client.py:461-463`); `--reasoning-effort`, `--snippet-max-tokens` and `--k` written out although they equal the defaults; `--query` pointing at a 5-line TSV (a `.tsv` path that exists is read as a dataset, `oss_client.py:522-533`); `--verbose`; one thread. `head` is standard shell. This needs the one-line key edit from B5.

Check:
- Nothing printed after `Error:`. If you see errors, stop, read them, and delete `runs/smoke/`.
- Count the run files that are not `completed`. Standard `grep`; the run files are written with `indent=2` (`oss_client.py:314`):

```bash
grep -L '"status": "completed"' runs/smoke/oss-20b/run_*.json | wc -l
```

- Most of the five should be `completed`. A completed run has a final `output_text` item in `result`.

### B7. Full run

**STOP.** Rough bound $13 to $56 for input at OpenRouter prices (section 1), plus output. Confirm the smoke test looked sane.

```bash
python search_agent/oss_client.py --model openai/gpt-oss-20b --model-url https://openrouter.ai/api/v1 --reasoning-effort high --output-dir runs/bm25/oss-20b --searcher-type bm25 --index-path indexes/bm25/ --num-threads 10 --snippet-max-tokens 512 --k 5
```

Adapted from `BCP:docs/oss.md:21`. Same changes as B6, plus ten threads (the value in `oss.md:21`), and no `--query` and no `--verbose`. The default query file is all 830 queries. If the run is interrupted, run the same command again. It skips query ids that already have a run file (`oss_client.py:338-350`). If the count of non-`completed` files is high, see the warning in B5 before resuming.

### B8. Judge with `gpt-4.1`

**STOP.** 830 judge calls on your OpenAI account. The output cap is 1,024 tokens each, at most $6.80 of output at $8 per million. Input is not bounded.

From `BCP:docs/llm_as_judge.md:9-12`:

```bash
export OPENAI_API_KEY={your_openai_api_key}
python scripts_evaluation/evaluate_with_openai.py --input_dir runs/my_model
```

Adapted: `runs/my_model` becomes `runs/bm25/oss-20b`. Defaults at `BCP:scripts_evaluation/evaluate_with_openai.py:385-410`: `--model gpt-4.1`, `--max_output_tokens 1024`, `--ground_truth data/browsecomp_plus_decrypted.jsonl`, `--eval_dir ./evals`, `--qrel_evidence topics-qrels/qrel_evidence.txt`, and `--force` to redo finished files. Do not confuse the `evals/` folder created here with the agentbus `evals` folder.

The repository's default judge is Qwen3-32B (`scripts_evaluation/evaluate_run.py`, `BCP:README.md:113-117`). The paper used `gpt-4.1` (Section 4.4 of the paper), which is why this runbook uses the OpenAI script. `BCP:docs/llm_as_judge.md:5` says the two judges differ by less than 1% on average.

Two behaviours to know. First, if a judge call fails, the script prints the error and moves on, and the query is left out of the totals (`evaluate_with_openai.py:543-569`). Check that the line `Processed N evaluations` shows 830. Rerunning skips queries whose `_eval.json` already exists (`evaluate_with_openai.py:465-474`). Second, a run whose status is not `completed`, or with no final answer text, is counted as wrong without a judge call (`evaluate_with_openai.py:508-539`).

### B9. Output format and mapping to omega-eval

Each query writes `run_<UTC timestamp>.json` in the output folder (`BCP:search_agent/oss_client.py:298-314`). The fields are exactly these:
- `metadata`: `model`, `reasoning` (the effort and summary request), `output_dir`.
- `query_id`, `tool_call_counts` (search calls are counted under `search`), `status` (`completed` or `incomplete`), `retrieved_docids`.
- `result`: a list of items of type `tool_call` (`tool_name`, `arguments`, `output`), `reasoning` (`output`) and `output_text` (`output`).

Token usage, cost and time are not saved. The client drops `response.usage`. Only `openai_client.py` saves usage (B5). Leave `input_tokens`, `output_tokens`, `cost_usd` and `wall_seconds` out of your omega-eval records. Note the provider's billed total for the run in your notes (section 6).

Judge outputs go to a folder that mirrors the input path after `runs` (`evaluate_with_openai.py:203-225`), so `runs/bm25/oss-20b` gives `evals/bm25/oss-20b/`:
- `<run file stem>_eval.json` per query (line 465), with `query_id`, `is_completed`, `judge_result` (`correct`, `confidence`, `parse_error`), `judge_response`, `tool_call_counts`, `retrieval`, `model_info`.
- `evaluation_summary.json` (lines 741-751): `LLM`, `Accuracy (%)`, `Recall (%)`, `avg_tool_stats`, `Calibration Error (%)`, and `per_query_metrics` (a list of `query_id`, `correct`, `recall`).
- `detailed_judge_results.csv` (lines 321-378): `query_id`, `judge_correct`, `is_completed`, `parse_error`, `json_path` (the run file for that query), and others.

Accuracy is correct answers divided by queries evaluated, so incomplete runs count as wrong (`evaluate_with_openai.py:660-665`).

Mapping to omega-eval result fields:

| omega-eval field | Source |
|---|---|
| `task_id` | `query_id` |
| `passed` | `correct` in `per_query_metrics` (or `judge_correct` in the CSV) |
| `attempt` | `0` for the single run |
| `trajectory` | `json_path` from `detailed_judge_results.csv` |
| `source` | the run folder, `runs/bm25/oss-20b` |
| `harness_version` | `046949032b0328319cc9a02663a759ec601d9402` |
| `input_tokens`, `output_tokens`, `cost_usd`, `wall_seconds` | not available; leave out |

Fixed values: `arm` = `solo`, `model` = `gpt-oss-20b`, `harness` = `browsecomp-plus-search-agent`, `run_set` = `screening`, `settings` = `{"retriever": "bm25", "reasoning_effort": "high", "max_iterations": 100, "max_tokens": 10000, "snippet_max_tokens": 512, "k": 5, "route": "openrouter"}`. The two keys the reference match needs are `retriever` and `reasoning_effort`. As a sanity check against the paper, compare `avg_tool_stats.search` with the paper's 26.87 search calls (Table 4).

---

## 5. Load results into omega-eval and read the report

Work in the `evals` folder of the agentbus repository. For the CMU suite, skip the JSON Lines files below and use the `import-cmu` command shown below. For BrowseComp-Plus, write your task records and results as JSON Lines (one object per line), using the field lists below.

Task records, one per task. For the CMU suite: `benchmark` = `cmu-business-tasks`, `family` = the paper's task name (`attendance-auditing`, `budget-approval`, `stock-alert`, `anomaly-detection`, `playwright-testing`, `website-management`, `code-refactoring`). For BrowseComp-Plus: `benchmark` = `browsecomp-plus`, `family` = empty.

```json
{"task_id": "q-0001", "benchmark": "browsecomp-plus", "benchmark_version": "<commit or date>", "family": "", "structure": "unknown", "grader": "model_judge", "split": "unassigned", "vetted": false, "notes": ""}
```

The values in this example are illustrative. `structure` is `unknown` unless you have a reason. The `grader` values below come from reading the scoring code:
- attendance-auditing, stock-alert, anomaly-detection: `state_check` (files, databases and uploaded reports are compared with ground truth).
- budget-approval and website-management: `mixed` (rule checks with an optional model fallback).
- playwright-testing and code-refactoring: `tests` (generated tests or an evaluation script are run).
- BrowseComp-Plus: `model_judge`.

For CMU, set `split` to `test` (you load test-split rows only). For BrowseComp-Plus choose `split` yourself from `unassigned`, `screening`, `development`, `test`.

Result records, one per attempt:

```json
{"task_id": "q-0001", "arm": "solo", "model": "gpt-oss-20b", "harness": "browsecomp-plus-search-agent", "harness_version": "<commit>", "run_set": "screening", "attempt": 0, "passed": false, "settings": {"retriever": "bm25", "reasoning_effort": "high"}, "trajectory": "runs/bm25/oss-20b/run_<timestamp>.json", "source": "runs/bm25/oss-20b"}
```

The values are illustrative. For CMU records, `harness` = `openhands-sdk-default`, `attempt` = 0, 1, 2 for the three runs, `model` is one of the three CMU model names, and each record also carries `score` (A8). The field mapping is in A8 and B9. `tasks_new.jsonl` below is the file of task records you wrote; `tasks.jsonl` is the registry it is merged into.

Unknown values: leave `cost_usd` out (or set it to `null`) when you do not know the cost. It is stored as unknown, not as zero, and the `validate` report does not need it. Leave `input_tokens`, `output_tokens` and `wall_seconds` out when unknown; do not set them to `null`. Extra keys inside `settings` are accepted as long as their values are strings, numbers or booleans.

Load the CMU results with the importer. It uses the `summary.json` from step A7, keeps test-split records and sets every CMU field described above, including `score`, `settings` and the task records with their `grader` values. Pass `--model` once per model to keep, and `--optimized` only for the authors' optimized prompts (harness `openhands-sdk-adapted`).

```bash
omega-eval results import-cmu generated/summary_gemma/summary.json --db results.sqlite --registry tasks.jsonl --model gemma-4-26b-a4b
```

Load BrowseComp-Plus records, or any records you wrote yourself:

```bash
omega-eval registry add tasks_new.jsonl --registry tasks.jsonl
omega-eval results import-jsonl results.jsonl --db results.sqlite --prices prices.yaml
```

Prices are needed only when `cost_usd` is missing and tokens are present. Copy `prices.example.yaml` to `prices.yaml` and fill in each model's `input_per_mtok`, `output_per_mtok`, `as_of` and `provider`. Null values are rejected on load, so fill all four models or remove those you do not need. Use the prices and dates from sections 3 and 4, and record the date you read them.

Compare:

```bash
omega-eval report validate --db results.sqlite --registry tasks.jsonl --references references.yaml
```

`references.yaml` already holds the published rates (read 2026-11-15): 21 CMU rows (7 tasks times 3 models) and BrowseComp-Plus rows for `gpt-oss-20b` at low, medium and high reasoning effort. Your BrowseComp-Plus results must carry `"retriever": "bm25"` and `"reasoning_effort": "high"` in `settings` to match the 21.08 row. CMU rates in that file are in percent. Those CMU rates are test-split means of partial scores (A8), so load `score` for every CMU record; without it omega-eval falls back to the pass rate, which is a different quantity for attendance-auditing, budget-approval and playwright-testing.

Reading the report:
- `consistent`: the published rate falls inside the 95% Wilson interval of your measured rate. The interval treats tasks as independent trials and stays informative at 0% and 100%.
- `differs`: the published rate falls outside that interval.
- `no_data`: there are no measured results that match that reference (check `benchmark`, `family`, `model`, `harness` and `settings`).

A `differs` result does not by itself mean the paper is wrong. Check, in order: settings mismatch, split mismatch (A7), judge differences, model identifier or provider differences (sections 3 and 4), sample size. For BrowseComp-Plus the paper does not give step limit, temperature or run count, so treat 21.08 as a loose reference.

---

## 6. What to record for every run

1. Repository commit hashes: `migration-analysis` (`2d31e17...`), its two submodule hashes, `BrowseComp-Plus` (`0469494...`), and a note of any local edit (the `oss_client.py` line, `data/refactorbench.json` paths).
2. Exact model identifier strings as sent to the provider (the `model:` values in `configs/models.yaml`; `--model` and `--model-url` for Part B), the provider, and the date.
3. For Part B: the judge model (`gpt-4.1`) and its date.
4. Run dates and times.
5. All settings: for Part A the manifest rows and your `configs/models.yaml` with keys removed; for Part B the flags in B7.
6. Prices used, with the provider, per-million-token input and output values, and the date read.
7. The split you evaluated (A7), and the `task_id` of each paper task as in section 3.
8. Total spend per model, from the provider's billing page.
9. The converter you wrote for the output formats in A8 and B9.

---

## 7. Known gaps

Items marked CHECK in this file:
1. Whether `uv sync` resolves and installs on Python 3.14 (A2).
2. Whether tool calling works for the three CMU models through OpenRouter (A3).
3. Whether a task container starts when `.vertex-ai.json` is missing (A4).
4. What the LOCA-bench custom build needs on the host (A4).
5. Whether your RefactorBench clone has the folder layout the data paths assume (A4).
6. Whether `collect_run_data.py` stops with a `KeyError` on a run that failed before a trace was saved (A7).
7. Whether the `flash-attn` line is needed for the BM25 path (B2).
8. Whether OpenRouter or Groq accepts, ignores or rejects `truncation`, `summary: "detailed"` and `strict` tools (B5).
9. Whether the installed `openai` package reads `OPENAI_BASE_URL` for `openai_client.py` (B5).

Not documented in either repository, and not settled by this file:
- The authors' `configs/models.yaml`: temperature, token limits and backend provider. The repository does not name a provider; only the paper's prices (which match the OpenRouter rows for Google Vertex and Amazon Bedrock) hint at one.
- The OpenHands SDK source and LOCA-bench (submodules, not read). The SDK defaults in A3 are from `docs/sdk_reference_details/01_llm.md`, not from the source.
- The WebArena image size and start-up time, and the disk size of the BM25 index and corpus.
- Whether the CMU commit pinned here is the one used for the paper's runs. It is the replication-package commit of 2026-09-04.
- Part B: the paper does not report step limit, temperature, serving stack or number of runs. The documented serving is vLLM `v0.10.1` (`BCP:docs/oss.md:11-16`). A hosted backend may differ from it in quantisation and sampling. The README's example summary (`BCP:README.md:123-136`) shows 32.17% for `openai/gpt-oss-20b` with no retriever, effort or judge named. It matches none of the BM25 rows of Table 4, so treat it as a format example and not as a target.
- `gpt-4.1` is marked Deprecated by OpenAI with no shutdown date shown. If it is withdrawn, the repository's Qwen3-32B judge (`evaluate_run.py`) is the fallback, at a difference the repository puts under 1% on average.
- Neither client can give per-query cost for Part B. Only the provider's total is available.
