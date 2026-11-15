import argparse
import json
import sys

import yaml

from omega_eval import analysis
from omega_eval.importers import import_inspect_log, import_jsonl
from omega_eval.pricing import load_prices
from omega_eval.registry import load_registry, save_registry
from omega_eval.results import ResultsStore


def _fmt(v: object) -> str:
    if v is None:
        return "-"
    return f"{v:.2f}" if isinstance(v, float) else str(v)


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("(no rows)")
        return
    cols = list(rows[0])
    cells = [[_fmt(r.get(c)) for c in cols] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    for line in [cols, *cells]:
        print("  ".join(v.ljust(w) for v, w in zip(line, widths, strict=True)).rstrip())


def _emit(data: object, rows: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2))
    else:
        _print_table(rows)


def _registry_add(args: argparse.Namespace) -> None:
    records = load_registry(args.registry)
    for task_id, rec in load_registry(args.file).items():
        if task_id in records and records[task_id] != rec:
            raise ValueError(f"task_id {task_id} already in registry with different content")
        records[task_id] = rec
    save_registry(args.registry, records)


def _registry_list(args: argparse.Namespace) -> None:
    rows = [r.model_dump() for r in load_registry(args.registry).values()]
    _print_table(
        [{k: r[k] for k in ("task_id", "benchmark", "family", "split", "vetted")} for r in rows]
    )


def _import_jsonl(args: argparse.Namespace) -> None:
    prices = load_prices(args.prices) if args.prices else None
    attempts = import_jsonl(args.file, prices)
    with ResultsStore(args.db) as store:
        store.add(attempts)
    print(f"imported {len(attempts)} attempts")


def _import_inspect(args: argparse.Namespace) -> None:
    attempts = import_inspect_log(
        args.log,
        arm=args.arm,
        harness=args.harness,
        run_set=args.run_set,
        prices=load_prices(args.prices),
        task_id_prefix=args.task_id_prefix,
        exclude_models=tuple(args.exclude_model),
    )
    with ResultsStore(args.db) as store:
        store.add(attempts)
    print(f"imported {len(attempts)} attempts")


def _report_success(args: argparse.Namespace) -> None:
    with ResultsStore(args.db) as store:
        attempts = store.query(run_set=args.run_set)
    rows = analysis.success_table(attempts, load_registry(args.registry))
    _emit(rows, rows, args.json)


def _report_validate(args: argparse.Namespace) -> None:
    with open(args.references) as f:
        references = yaml.safe_load(f)["references"]
    with ResultsStore(args.db) as store:
        attempts = store.query(arm="solo")
    rows = analysis.validate(attempts, load_registry(args.registry), references)
    shown = [
        {k: r.get(k) for k in ("benchmark", "family", "model", "settings", "expected_rate")}
        | {k: r.get(k) for k in ("n_tasks", "measured_rate", "ci_low", "ci_high", "status")}
        for r in rows
    ]
    _emit(rows, shown, args.json)


def _report_compare(args: argparse.Namespace) -> None:
    with ResultsStore(args.db) as store:
        system = store.query(arm=args.system_arm, run_set="scoring")
        solo = store.query(arm="solo", run_set="scoring")
    result = analysis.compare(system, solo, load_registry(args.registry))
    if args.json:
        print(json.dumps(result, indent=2))
        return
    summary = {"n_trials": result["n_trials"], "net": result["net"]} | {
        k: result[k] for k in ("net_per_trial", "ci_low", "ci_high")
    }
    _print_table([summary | result["counts"]])
    print()
    _print_table([{"family": f} | c for f, c in sorted(result["by_family"].items())])


GROUPS = ("registry", "results", "report")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="omega-eval")
    top = p.add_subparsers(dest="group", required=True)
    groups = {g: top.add_parser(g).add_subparsers(dest="cmd", required=True) for g in GROUPS}

    def command(group: str, name: str, func, args: list[tuple]) -> None:
        sub = groups[group].add_parser(name)
        for flag, kwargs in args:
            sub.add_argument(flag, **kwargs)
        sub.set_defaults(func=func)

    reg = ("--registry", {"required": True})
    db = ("--db", {"required": True})
    js = ("--json", {"action": "store_true"})
    command("registry", "add", _registry_add, [("file", {}), reg])
    command("registry", "list", _registry_list, [reg])
    command(
        "results",
        "import-jsonl",
        _import_jsonl,
        [("file", {}), db, ("--prices", {})],
    )
    command(
        "results",
        "import-inspect",
        _import_inspect,
        [
            ("log", {}),
            db,
            ("--arm", {"required": True, "choices": ["solo", "agentbus", "omega"]}),
            ("--harness", {"required": True}),
            ("--run-set", {"required": True, "choices": ["screening", "scoring"]}),
            ("--prices", {"required": True}),
            ("--task-id-prefix", {"default": ""}),
            ("--exclude-model", {"action": "append", "default": []}),
        ],
    )
    command("report", "success", _report_success, [db, reg, ("--run-set", {}), js])
    command(
        "report",
        "validate",
        _report_validate,
        [db, reg, ("--references", {"required": True}), js],
    )
    command(
        "report",
        "compare",
        _report_compare,
        [db, reg, ("--system-arm", {"required": True, "choices": ["agentbus", "omega"]}), js],
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        args.func(args)
    except (ValueError, KeyError, FileNotFoundError, ImportError) as e:
        print(f"error: {e.args[0] if e.args else e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
