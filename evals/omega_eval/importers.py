import json
from pathlib import Path
from typing import Literal

from omega_eval.pricing import Price, cost_usd
from omega_eval.results import Attempt


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
