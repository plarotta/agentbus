import json
from collections.abc import Collection
from pathlib import Path
from typing import Literal

from omega_eval.pricing import Price, cost_usd
from omega_eval.registry import TaskRecord
from omega_eval.results import Attempt

# CMU task id -> (family, grader), from the benchmark's scoring code.
_CMU_TASKS: dict[str, tuple[str, Literal["tests", "state_check", "mixed"]]] = {
    "attendance_payroll_audit_s2l_high": ("attendance-auditing", "state_check"),
    "budget_approval_s2l_high": ("budget-approval", "mixed"),
    "woocommerce_stock_alert_s2l": ("stock-alert", "state_check"),
    "machine_operating_s2l": ("anomaly-detection", "state_check"),
    "webtest": ("playwright-testing", "tests"),
    "webarena": ("website-management", "mixed"),
    "refactorbench": ("code-refactoring", "tests"),
}


def import_jsonl(path: str | Path, prices: dict[str, Price] | None = None) -> list[Attempt]:
    """One Attempt per line. A missing cost is computed from tokens when prices are given;
    otherwise it stays unknown (None) and the budget-matched comparison will refuse it."""
    attempts = []
    for n, line in enumerate(Path(path).read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            if obj.get("cost_usd") is None and prices is not None and obj.get("input_tokens"):
                obj["cost_usd"] = cost_usd(
                    prices, obj["model"], obj["input_tokens"], obj.get("output_tokens", 0)
                )
            attempts.append(Attempt(**obj))
        except (ValueError, KeyError) as e:
            raise ValueError(f"line {n}: {e}") from e
    return attempts


def import_inspect_log(
    path: str | Path,
    *,
    arm: Literal["solo", "agentbus", "omega"],
    harness: str,
    run_set: Literal["screening", "scoring"],
    prices: dict[str, Price],
    task_id_prefix: str = "",
    exclude_models: tuple[str, ...] = (),
) -> list[Attempt]:
    """One Attempt per sample and epoch of an Inspect AI log.

    A sample's usage covers every model called during it, graders included. Each model is
    priced under its own name; list grader models in `exclude_models` so judging cost is
    not charged to the system under test.
    """
    try:
        from inspect_ai.log import read_eval_log
        from inspect_ai.scorer import CORRECT
    except ImportError as e:
        raise ImportError("inspect-ai is required: pip install 'omega-eval[inspect]'") from e

    log = read_eval_log(str(path))
    model = log.eval.model
    attempts = []
    for sample in log.samples or []:
        if not sample.scores:
            raise ValueError(f"sample {sample.id} epoch {sample.epoch} has no scores")
        value = next(iter(sample.scores.values())).value
        tokens_in = tokens_out = 0
        cost = 0.0
        for name, usage in sample.model_usage.items():
            if name in exclude_models:
                continue
            tokens_in += usage.input_tokens
            tokens_out += usage.output_tokens
            cost += cost_usd(prices, name, usage.input_tokens, usage.output_tokens)
        attempts.append(
            Attempt(
                task_id=task_id_prefix + str(sample.id),
                arm=arm,
                model=model,
                harness=harness,
                run_set=run_set,
                attempt=sample.epoch - 1,
                passed=value in (CORRECT, 1),
                cost_usd=cost,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                wall_seconds=sample.total_time or 0.0,
                source=str(path),
            )
        )
    return attempts


def import_cmu_summary(
    path: str | Path,
    *,
    optimized: bool = False,
    models: Collection[str] | None = None,
    run_set: Literal["screening", "scoring"] = "screening",
) -> tuple[list[Attempt], list[TaskRecord]]:
    """Test-split attempts and task records from the CMU benchmark's `summary.json`.

    Rows are filtered and de-duplicated as the authors do: a row is optimized when its
    `rollout_version` ends with "best", and per (task, model) the row with the highest
    validation score is kept. A null score counts as 0.0 and a null cost stays unknown.
    """
    best: dict[tuple[str, str], dict] = {}
    for row in json.loads(Path(path).read_text())["rows"]:
        if row["status"] != "ok" or row["task_id"] not in _CMU_TASKS:
            continue
        if row["rollout_version"].endswith("best") != optimized:
            continue
        if models is not None and row["model_name"] not in models:
            continue
        key = (row["task_id"], row["model_name"])
        if key not in best or row["val"]["score"] > best[key]["val"]["score"]:
            best[key] = row

    harness = "openhands-sdk-adapted" if optimized else "openhands-sdk-default"
    attempts = []
    for row in best.values():
        family = _CMU_TASKS[row["task_id"]][0]
        for rec in row["workspace_records"]:
            if rec["split"] != "test":
                continue
            score = rec["score"] or 0.0
            attempts.append(
                Attempt(
                    task_id=f"{family}-ex{rec['example_id']:03d}",
                    arm="solo",
                    model=row["model_name"],
                    harness=harness,
                    run_set=run_set,
                    attempt=rec["rollout_id"],
                    passed=score >= 1 - 1e-9,
                    score=score,
                    cost_usd=rec["cost"],
                    input_tokens=rec["prompt_tokens"],
                    output_tokens=rec["completion_tokens"],
                    wall_seconds=rec["end_to_end_latency_sec"],
                    settings={
                        "split": "test",
                        "rollout_version": row["rollout_version"],
                        "prompt_name": row["prompt_name"],
                    },
                    trajectory=rec["workspace_dir"],
                    source=str(path),
                )
            )
    grader_of = {family: grader for family, grader in _CMU_TASKS.values()}
    tasks = {}
    for a in attempts:
        family = a.task_id.rsplit("-ex", 1)[0]
        tasks[a.task_id] = TaskRecord(
            task_id=a.task_id,
            benchmark="cmu-business-tasks",
            family=family,
            grader=grader_of[family],
            split="test",
        )
    return attempts, sorted(tasks.values(), key=lambda t: t.task_id)
