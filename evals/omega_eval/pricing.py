from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float
    as_of: str | None = None
    provider: str | None = None


def load_prices(path: str | Path) -> dict[str, Price]:
    raw = (yaml.safe_load(Path(path).read_text()) or {}).get("prices") or {}
    prices = {}
    for model, p in raw.items():
        p = p or {}
        for field in ("input_per_mtok", "output_per_mtok"):
            if p.get(field) is None:
                raise ValueError(f"price {field} missing for model {model}")
        as_of = p.get("as_of")
        prices[model] = Price(
            float(p["input_per_mtok"]),
            float(p["output_per_mtok"]),
            None if as_of is None else str(as_of),
            p.get("provider"),
        )
    return prices


def cost_usd(prices: dict[str, Price], model: str, input_tokens: int, output_tokens: int) -> float:
    p = prices[model]  # KeyError for an unpriced model, never a silent zero
    return (input_tokens * p.input_per_mtok + output_tokens * p.output_per_mtok) / 1e6
