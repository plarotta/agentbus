import pytest
from omega_eval.pricing import cost_usd, load_prices


def write(tmp_path, body):
    p = tmp_path / "p.yaml"
    p.write_text(body)
    return p


def test_null_price_rejected(tmp_path):
    p = write(tmp_path, "prices:\n  m: {input_per_mtok: 1.0, output_per_mtok: null}\n")
    with pytest.raises(ValueError, match="m"):
        load_prices(p)


def test_example_file_is_rejected_until_filled():
    from pathlib import Path

    example = Path(__file__).parent.parent / "prices.example.yaml"
    with pytest.raises(ValueError, match="gemma-4-26b-a4b"):
        load_prices(example)


def test_unknown_model_raises(tmp_path):
    prices = load_prices(write(tmp_path, "prices:\n  m: {input_per_mtok: 1, output_per_mtok: 2}\n"))
    with pytest.raises(KeyError):
        cost_usd(prices, "other", 10, 10)


def test_known_computation(tmp_path):
    p = write(
        tmp_path,
        "prices:\n  m: {input_per_mtok: 0.5, output_per_mtok: 2.0, as_of: 2026-11-15}\n",
    )
    prices = load_prices(p)
    assert prices["m"].as_of == "2026-11-15"
    # 2,000,000 * 0.5 / 1e6 + 500,000 * 2.0 / 1e6 = 1.0 + 1.0
    assert cost_usd(prices, "m", 2_000_000, 500_000) == pytest.approx(2.0)
