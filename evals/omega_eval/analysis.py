import math
import random
from collections import defaultdict
from collections.abc import Callable

from omega_eval.registry import TaskRecord
from omega_eval.results import Attempt

Registry = dict[str, TaskRecord]


def _bootstrap(
    tasks: list[str], stat: Callable[[list[str]], float], n_boot: int, seed: int
) -> tuple[float, float]:
    """95% percentile interval of `stat` over task resamples."""
    rng = random.Random(seed)
    samples = sorted(stat(rng.choices(tasks, k=len(tasks))) for _ in range(n_boot))
    return samples[int(0.025 * n_boot)], samples[min(n_boot - 1, int(0.975 * n_boot))]


def _task_rates(attempts: list[Attempt]) -> dict[str, float]:
    by_task: dict[str, list[bool]] = defaultdict(list)
    for a in attempts:
        by_task[a.task_id].append(a.passed)
    return {t: sum(v) / len(v) for t, v in by_task.items()}


def _wilson(p: float, n: int) -> tuple[float, float]:
    """95% Wilson score interval for a proportion `p` observed over `n` tasks.

    Unlike a bootstrap it stays informative at 0% and 100%, where every resample is
    identical. It treats tasks as independent trials, so it is approximate when a task
    has several attempts.
    """
    z = 1.959964
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (0.0 if p == 0 else centre - half), (1.0 if p == 1 else centre + half)


def _record(registry: Registry, task_id: str) -> TaskRecord:
    if task_id not in registry:
        raise KeyError(f"task_id not in registry: {task_id}")
    return registry[task_id]


def _rate_with_interval(attempts: list[Attempt]) -> dict:
    rates = _task_rates(attempts)
    p = sum(rates.values()) / len(rates)
    low, high = _wilson(p, len(rates))
    return {
        "n_tasks": len(rates),
        "n_attempts": len(attempts),
        "pass_rate": 100 * p,
        "ci_low": 100 * low,
        "ci_high": 100 * high,
    }


def success_table(attempts: list[Attempt], registry: Registry) -> list[dict]:
    groups: dict[tuple, list[Attempt]] = defaultdict(list)
    for a in attempts:
        rec = _record(registry, a.task_id)
        groups[(rec.benchmark, rec.family, a.model, a.harness, a.run_set)].append(a)
    keys = ("benchmark", "family", "model", "harness", "run_set")
    return [
        dict(zip(keys, key, strict=True)) | _rate_with_interval(group)
        for key, group in sorted(groups.items())
    ]


def validate(attempts: list[Attempt], registry: Registry, references: list[dict]) -> list[dict]:
    """Compare measured solo rates with published ones.

    `consistent` means the published rate lies inside the measured rate's interval. The
    published number's own sampling error is ignored, so `differs` is somewhat too eager
    when the reference came from a small sample or a single run.
    """
    out = []
    for ref in references:
        wanted = ref.get("settings", {})
        matched = []
        for a in attempts:
            rec = _record(registry, a.task_id)
            if (
                a.arm == "solo"
                and rec.benchmark == ref["benchmark"]
                and a.model == ref["model"]
                and rec.family == ref.get("family", rec.family)
                and a.harness == ref.get("harness", a.harness)
                and all(a.settings.get(k) == v for k, v in wanted.items())
            ):
                matched.append(a)
        row = dict(ref)
        if not matched:
            out.append(row | {"n_tasks": 0, "measured_rate": None, "status": "no_data"})
            continue
        r = _rate_with_interval(matched)
        inside = r["ci_low"] <= ref["expected_rate"] <= r["ci_high"]
        out.append(
            row
            | {
                "n_tasks": r["n_tasks"],
                "measured_rate": r["pass_rate"],
                "ci_low": r["ci_low"],
                "ci_high": r["ci_high"],
                "status": "consistent" if inside else "differs",
            }
        )
    return out


def _cost(a: Attempt) -> float:
    if a.cost_usd is None:
        raise ValueError(
            f"unknown cost for {a.task_id} ({a.arm}, {a.model}, {a.harness}, attempt {a.attempt}); "
            "the budget-matched comparison needs a cost for every attempt it uses"
        )
    return a.cost_usd


def oracle_solves(solo_attempts: list[Attempt], task_id: str, budget_usd: float) -> bool:
    groups: dict[tuple, list[Attempt]] = defaultdict(list)
    for a in solo_attempts:
        if a.task_id == task_id:
            groups[(a.model, a.harness)].append(a)
    for group in groups.values():
        total = 0.0
        for i, a in enumerate(sorted(group, key=lambda x: x.attempt)):
            total += _cost(a)
            if i > 0 and total > budget_usd:
                break
            if a.passed:
                return True
    return False


def compare(
    system_attempts: list[Attempt],
    solo_attempts: list[Attempt],
    registry: Registry,
    n_boot: int = 2000,
    seed: int = 0,
) -> dict:
    if any(a.run_set == "screening" for a in solo_attempts):
        raise ValueError("solo attempts from the screening run set cannot be used in compare")
    if any(a.arm == "solo" for a in system_attempts):
        raise ValueError("system_attempts contains a solo-arm attempt")
    if any(a.arm != "solo" for a in solo_attempts):
        raise ValueError("solo_attempts contains a system-arm attempt")
    solo_tasks = {a.task_id for a in solo_attempts}
    counts = {"novel": 0, "regression": 0, "both_solved": 0, "neither": 0}
    by_family: dict[str, dict[str, int]] = {}
    per_task: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # net, trials
    for a in system_attempts:
        if a.task_id not in solo_tasks:
            raise ValueError(f"no solo attempts for system task: {a.task_id}")
        solved = oracle_solves(solo_attempts, a.task_id, _cost(a))
        kind = (
            ("both_solved" if solved else "novel")
            if a.passed
            else ("regression" if solved else "neither")
        )
        counts[kind] += 1
        family = _record(registry, a.task_id).family
        fam = by_family.setdefault(family, dict.fromkeys(counts, 0))
        fam[kind] += 1
        per_task[a.task_id][0] += (kind == "novel") - (kind == "regression")
        per_task[a.task_id][1] += 1

    def net_per_trial(sample: list[str]) -> float:
        return sum(per_task[t][0] for t in sample) / sum(per_task[t][1] for t in sample)

    n_trials = len(system_attempts)
    low, high = _bootstrap(list(per_task), net_per_trial, n_boot, seed) if n_trials else (0, 0)
    net = counts["novel"] - counts["regression"]
    return {
        "counts": counts,
        "by_family": by_family,
        "net": net,
        "n_trials": n_trials,
        "net_per_trial": net / n_trials if n_trials else 0.0,
        "ci_low": low,
        "ci_high": high,
    }
