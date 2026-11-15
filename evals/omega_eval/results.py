import json
import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

_COLUMNS = [
    "task_id",
    "arm",
    "model",
    "harness",
    "harness_version",
    "run_set",
    "attempt",
    "passed",
    "cost_usd",
    "input_tokens",
    "output_tokens",
    "wall_seconds",
    "settings",
    "trajectory",
    "source",
    "score",
]
_KEY = ("task_id", "arm", "model", "harness", "run_set", "attempt")


class Attempt(BaseModel):
    task_id: str
    arm: Literal["solo", "agentbus", "omega"]
    model: str
    harness: str
    harness_version: str = ""
    run_set: Literal["screening", "scoring"]
    attempt: int = Field(ge=0)  # 0-based
    passed: bool
    score: float | None = Field(default=None, ge=0, le=1)  # partial credit, if graded
    cost_usd: float | None = Field(default=None, ge=0)  # None means unknown
    input_tokens: int = 0
    output_tokens: int = 0
    wall_seconds: float = 0.0
    settings: dict[str, str | int | float | bool] = {}
    trajectory: str = ""
    source: str = ""


class ResultsStore:
    def __init__(self, path: str | Path):
        self._db = sqlite3.connect(path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS attempts ("
            "task_id TEXT, arm TEXT, model TEXT, harness TEXT, harness_version TEXT, "
            "run_set TEXT, attempt INTEGER, passed INTEGER, cost_usd REAL, "
            "input_tokens INTEGER, output_tokens INTEGER, wall_seconds REAL, "
            "settings TEXT, trajectory TEXT, source TEXT, score REAL, "
            "UNIQUE (task_id, arm, model, harness, run_set, attempt))"
        )

    def add(self, attempts: list[Attempt]) -> None:
        sql = f"INSERT INTO attempts VALUES ({','.join('?' * len(_COLUMNS))})"
        try:
            with self._db:
                for a in attempts:
                    row = a.model_dump()
                    row["settings"] = json.dumps(row["settings"])
                    self._db.execute(sql, [row[c] for c in _COLUMNS])
        except sqlite3.IntegrityError as e:
            key = {k: getattr(a, k) for k in _KEY}
            raise ValueError(f"duplicate attempt key {key}; nothing inserted") from e

    def query(
        self,
        *,
        arm: str | None = None,
        model: str | None = None,
        harness: str | None = None,
        run_set: str | None = None,
    ) -> list[Attempt]:
        filters = {"arm": arm, "model": model, "harness": harness, "run_set": run_set}
        active = {k: v for k, v in filters.items() if v is not None}
        where = " AND ".join(f"{k} = ?" for k in active) or "1"
        rows = self._db.execute(
            f"SELECT {','.join(_COLUMNS)} FROM attempts WHERE {where} "
            "ORDER BY task_id, model, harness, attempt",
            list(active.values()),
        ).fetchall()
        out = []
        for row in rows:
            d = dict(zip(_COLUMNS, row, strict=True))
            d["passed"] = bool(d["passed"])
            d["settings"] = json.loads(d["settings"])
            out.append(Attempt(**d))
        return out

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "ResultsStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
