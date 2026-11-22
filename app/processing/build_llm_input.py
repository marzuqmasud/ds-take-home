"""Assemble one LLM input record per company from the three assessment DataFrames."""

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.processing.assess_company_risk import assess_company_risk
from app.processing.build_economic_context import build_economic_context
from app.processing.calculate_financials import calculate_financials


def build_llm_input(source: Path | str) -> pd.DataFrame:
    """Join the financial, risk, and economic assessments for the same JSONL input."""
    path = Path(source)
    result = calculate_financials(path)
    for build in (assess_company_risk, build_economic_context):
        section = build(path).drop(columns="companyName")
        if set(section["registrationNumber"]) != set(result["registrationNumber"]):
            raise ValueError(f"{build.__name__} returned a different set of registration numbers")
        result = result.merge(section, on="registrationNumber", validate="one_to_one")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, nargs="?", help="path to the company JSONL file")
    args = parser.parse_args()
    source = args.input if args.input is not None else Path(input("Company JSONL path: ").strip()).expanduser()
    result = build_llm_input(source)
    output = source.with_name(f"{source.stem}_llm_input.jsonl")
    output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in result.to_dict("records")),
        encoding="utf-8",
    )
    print(f"Wrote LLM inputs for {len(result)} companies to {output}")


if __name__ == "__main__":
    main()
