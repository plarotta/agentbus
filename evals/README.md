# omega-eval

Stores benchmark task records and per-attempt results, and computes three reports:
success rates, a check against published reference numbers, and a budget-matched
comparison of a multi-part system against single-model runs. It does not run models or
benchmarks. Results arrive by import (JSON Lines or an Inspect AI log).

## Install

```
pip install -e ".[dev]"        # add the inspect extra to import Inspect AI logs
pip install -e ".[inspect]"
```

Copy `prices.example.yaml` to `prices.yaml` and fill in prices from your provider's price
list, with the date. Null prices are rejected.

## Data model

| Object | Stored in | Key fields |
| --- | --- | --- |
| `TaskRecord` | JSON Lines registry | `task_id`, `benchmark`, `family`, `structure`, `grader`, `split`, `vetted` |
| `Attempt` | SQLite `attempts` table | `task_id`, `arm`, `model`, `harness`, `run_set`, `attempt`, `passed`, `score`, `cost_usd`, `settings` |

An attempt is unique on `(task_id, arm, model, harness, run_set, attempt)`. `arm` is `solo`
(one model in one public harness), `agentbus` or `omega`. `run_set` is `screening` or
`scoring`.

`score` is optional partial credit between 0 and 1, for benchmarks whose published number
is a mean score and not a pass rate. `report success` and `report validate` average `score`
where an attempt has one and `passed` (0 or 1) where it does not, so the `pass_rate` column
is a mean score for those rows. `report compare` uses `passed` only.

## Reports

```
omega-eval registry add tasks_new.jsonl --registry tasks.jsonl
omega-eval results import-jsonl runs.jsonl --db results.sqlite --prices prices.yaml

omega-eval report success  --db results.sqlite --registry tasks.jsonl
omega-eval report validate --db results.sqlite --registry tasks.jsonl --references references.yaml
omega-eval report compare  --db results.sqlite --registry tasks.jsonl --system-arm omega
```

- `success`: pass rate per benchmark, family, model, harness and run set. The rate is the
  mean over tasks of each task's mean pass, with a 95% Wilson interval over tasks (it stays informative at 0% and 100%).
- `validate`: measured solo rate against each entry in `references.yaml`; status is
  `no_data`, `consistent` (expected rate inside the interval) or `differs`.
- `compare`: counts of novel solves and regressions, per family, and net per trial with a
  95% bootstrap interval over tasks.

Add `--json` to any report for JSON output. `compare` uses scoring attempts only.

## Definitions

- **Budget-matched oracle** for a task and budget B: group the task's solo attempts by
  (model, harness). In each group take attempts in ascending `attempt` order, as many as
  keep cumulative `cost_usd` <= B, but always at least the first. A group solves the task
  if any taken attempt passed; the oracle solves it if any group does.
- **Novel solve**: a system attempt passed and the oracle, with B equal to that attempt's
  `cost_usd`, did not.
- **Regression**: a system attempt failed and the oracle with the same B solved the task.

Solo attempts from the `screening` run set were used to pick tasks, so `compare` refuses
them.

`cost_usd` may be left out when it is not known (for example a model served on your own
hardware before a cost convention is chosen). It is stored as unknown, never as zero, and
`compare` refuses any attempt whose cost is unknown. The other reports do not need costs.
