"""Build DOSM business context from a company JSONL file and local source data."""

import argparse
import calendar
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, cast

import pandas as pd

# Support both package imports and direct script execution.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.processing.calculate_financials import annual_calculations
from scripts.generate_dosm_mappings import (
    CALENDAR_YEARS,
    CATALOGUE,
    CONSTRUCTION,
    DATASETS,
    METRICS,
    ROOT,
    aggregate,
    csv_rows,
    load_series,
    read_activity_mappings,
    read_rules,
    shift_month,
)

type Record = dict[str, Any]
type SeriesKey = tuple[str, str, str]

INBOUND = "tourism_satellite_account_2024"
TOURISM_VALUE_ADDED = "tourism_satellite_account_2025"
INBOUND_FILE = "tourism_inbound_2024.csv"
PUBLICATIONS = {
    INBOUND: ("Tourism Satellite Account 2024", "2025-09-12"),
    TOURISM_VALUE_ADDED: ("Tourism Satellite Account 2025", "2026-09-15"),
}
INBOUND_URL = "https://storage.dosm.gov.my/tourism/tourism_2024.pdf"
MATCH_TYPES = {
    "msic_item": "exact",
    "msic_group": "group",
    "msic_division": "division",
    "msic_section": "section",
    "msic_composite": "composite",
    "custom_subsector": "subsector",
    "custom_industry": "subsector",
    "thematic_proxy": "thematic_proxy",
}
REVENUE_INDICATORS = {"nominal_trade_sales", "nominal_services_revenue"}
PRICE_INDICATORS = {"producer_price_index", "services_producer_price_index"}
PRICE_PRESSURE_THRESHOLD_PCT = 5.0


def series_key(rule: Record) -> SeriesKey:
    return rule["dataset"], rule["dosm_key"], rule["value_field"]


def load_crosswalk(
    root: Path, codes: dict[str, tuple[str, str]], activities: set[str]
) -> tuple[dict[str, list[Record]], dict[SeriesKey, dict[date, float]], dict[date, tuple[int, float]]]:
    source_dir = root / "data/dosm"
    dictionary = csv_rows((source_dir / "msic.csv").read_bytes())
    valid_codes = {row["item"] for row in dictionary if row["digits"] == "5" and row["canonical"] == "True"}
    rules = read_rules(root, {name: codes[name] for name in activities}, valid_codes)
    supplemental = csv_rows((root / "mappings/dosm_context_supplement.csv").read_bytes())

    for rule in supplemental:
        activity = rule["normalized_activity"]

        if activity not in activities:
            continue

        if rule["code_role"] != "primary" or rule["applied_msic_2008"] != codes[activity][0]:
            raise ValueError(f"MSIC mapping has changed for {activity}. Review the DOSM context supplement.")

        rules[activity].append(rule)

    def source_file(url: str) -> Path:
        name = "construction-statistics.html" if url == CONSTRUCTION else url.rsplit("/", 1)[-1]
        return source_dir / name

    def read_source(url: str) -> bytes:
        return source_file(url).read_bytes()

    publishable = [rule for group in rules.values() for rule in group if rule["summary_use"] in {"lead", "support"}]
    active = [
        rule
        for rule in publishable
        if rule["dataset"] in DATASETS and source_file(DATASETS[rule["dataset"]].data_url).is_file()
    ]

    series = cast("dict[SeriesKey, dict[date, float]]", load_series(active, read_source))
    inbound = {}

    if any(rule["dataset"] == INBOUND for rule in publishable) and (source_dir / INBOUND_FILE).is_file():
        for row in csv_rows((source_dir / INBOUND_FILE).read_bytes()):
            inbound[date(int(row["year"]), 12, 31)] = (int(row["value_myr"]), float(row["published_growth_pct"]))

    return cast("dict[str, list[Record]]", rules), series, inbound


def metadata(rule: Record, primary_msic: str, lead_inbound: bool) -> Record:
    dataset, key, field = series_key(rule)
    release, published = PUBLICATIONS.get(dataset, (None, None))

    if dataset == INBOUND:
        indicator, unit, aggregation = "inbound_tourism_expenditure", "MYR", "published_annual"
        data_url, frequency, source_file = INBOUND_URL, "annual", INBOUND_FILE
    else:
        source = DATASETS[dataset]
        indicator, unit, aggregation = (
            ("services_persons_engaged", "persons", "mean") if field == "employees" else METRICS[dataset, field]
        )

        data_url, frequency = source.data_url, source.frequency
        source_file = "construction-statistics.html" if dataset == "construction_statistics" else data_url.rsplit("/", 1)[-1]

    match_level = rule["match_level"]

    if indicator in REVENUE_INDICATORS:
        measure = "Revenue"
    elif indicator in PRICE_INDICATORS:
        measure = "Prices"
    elif indicator.endswith("_index") or indicator in {"real_value_added", "nominal_gross_value_added"}:
        measure = "Output"
    else:
        measure = "None"

    primary = rule["summary_use"] == "lead" and (dataset != TOURISM_VALUE_ADDED or not lead_inbound)
    limitations = [rule["screening_note"]]

    if dataset == INBOUND:
        limitations.append("The published value is rounded. Growth uses the published percentage from Exhibit 5.")

    if not published:
        limitations.append("The source cache does not identify the original release date for each observation.")

    return {
        "source": "Department of Statistics Malaysia",
        "datasetId": dataset,
        "indicatorId": indicator,
        "indicator": indicator.replace("_", " ").capitalize(),
        "publication": release,
        "sourcePublishedOn": published,
        "sourceUrl": (
            f"https://www.dosm.gov.my/portal-main/release-content/{dataset.replace('_', '-')}"
            if dataset in PUBLICATIONS
            else data_url
            if dataset == "construction_statistics"
            else CATALOGUE + dataset
        ),
        "dataUrl": data_url,
        "sourceFile": f"data/dosm/{source_file}",
        "primaryMSIC": primary_msic,
        "appliedMSIC": rule["applied_msic_2008"].split("|"),
        "msicVersion": "2008",
        "matchedCode": None if dataset == INBOUND else key,
        "matchType": MATCH_TYPES[match_level],
        "matchLevel": "national_tourism" if dataset == INBOUND else match_level,
        "scope": rule["published_scope"],
        "geography": "Malaysia",
        "relevance": "Primary" if primary else "Supplementary",
        "comparableWith": measure,
        "unit": unit,
        "indexBaseYear": int(unit[:4]) if re.fullmatch(r"\d{4} = 100", unit) else None,
        "frequency": frequency,
        "aggregation": aggregation,
        "limitations": limitations,
    }


def prior_year_end(end: date) -> date:
    year = end.year - 1
    previous_days = calendar.monthrange(year, end.month)[1]
    is_month_end = end.day == calendar.monthrange(end.year, end.month)[1]

    return date(year, end.month, previous_days if is_month_end else min(end.day, previous_days))


def indicator_for_period(
    details: Record,
    key: SeriesKey,
    end: date,
    has_filing: bool,
    values: dict[date, float],
    inbound: dict[date, tuple[int, float]],
) -> Record | None:
    calendar_end = date(end.year, 12, 31)
    actual_end = end
    value: int | float | None
    growth: float | None

    if key[0] == INBOUND:
        value, growth = inbound.get(calendar_end, (None, None))
        actual_end = calendar_end
    else:
        frequency, aggregation = details["frequency"], details["aggregation"]
        value = aggregate(values, end, frequency, aggregation)

        if value is None and end != calendar_end:
            actual_end = calendar_end
            value = aggregate(values, actual_end, frequency, aggregation)

        previous = aggregate(values, prior_year_end(actual_end), frequency, aggregation)
        growth = 100 * (value / previous - 1) if value is not None and previous not in (None, 0) else None

    if value is None:
        return None

    aligned = has_filing and actual_end == end
    approximate = has_filing and actual_end != end
    comparable = (
        aligned
        and details["comparableWith"] == "Revenue"
        and details["matchType"] in {"exact", "group", "division"}
        and details["primaryMSIC"].startswith(details["matchedCode"] or "unmatched")
    )

    limitations = list(details["limitations"])

    if approximate:
        limitations.append("Calendar-year data cover a different window from the company filing.")

    if comparable and details["matchType"] != "exact":
        limitations.append(
            "The matched industry includes other activities. Comparison is limited to annual nominal growth rates."
        )

    if key[0] == TOURISM_VALUE_ADDED and actual_end.year == 2024:
        limitations.append("The 2024 figure is an estimate from the 2025 account, published in 2026.")

    if key[0] == INBOUND and actual_end.year == 2022:
        limitations.append("The 2022 rebound was measured from a very low 2021 base.")

    return {
        **details,
        "id": f"DOSM/{':'.join(key)}/{actual_end.isoformat()}",
        "year": actual_end.year,
        "periodStart": shift_month(actual_end, -11).isoformat(),
        "periodEnd": actual_end.isoformat(),
        "companyFinancialYearEnd": end.isoformat() if has_filing else None,
        "periodBasis": "fiscal" if aligned and details["frequency"] != "annual" else "calendar",
        "fiscalWindowAligned": aligned,
        "calendarApproximation": approximate,
        "value": value,
        "growthPct": growth,
        "growthBasis": "Published annual percentage change" if key[0] == INBOUND else "Change from prior annual window",
        "verified": True,
        "directlyComparableWithCompanyRevenue": comparable,
        "limitations": limitations,
    }


def contextual_signals(latest_indicators: list[Record], performance: Record) -> list[Record]:
    signals = []
    company_growth = performance.get("revenueGrowthPct")

    for indicator in latest_indicators:
        sector_growth = indicator["growthPct"]

        if sector_growth is None:
            continue
        reference = {"indicatorId": indicator["id"], "sectorGrowthPct": sector_growth}

        if indicator["indicatorId"] in PRICE_INDICATORS:
            if sector_growth >= PRICE_PRESSURE_THRESHOLD_PCT:
                signals.append(
                    {
                        **reference,
                        "signal": "possibleCostPressure",
                        "interpretation": "hypothesis",
                        "thresholdPct": PRICE_PRESSURE_THRESHOLD_PCT,
                        "note": "Producer selling prices rose. The company's exposure to higher input costs is unverified.",
                    }
                )
            continue

        if company_growth is None or indicator["indicatorId"] == "services_persons_engaged":
            continue

        signal = None
        if company_growth < 0 < sector_growth:
            signal = "companyDecliningSectorGrowing"
        elif company_growth < 0 and sector_growth < 0:
            signal = "companyAndSectorDeclining"
        elif company_growth > 0 > sector_growth:
            signal = "companyGrowingSectorDeclining"

        if signal:
            comparable = indicator["directlyComparableWithCompanyRevenue"]
            signals.append(
                {
                    **reference,
                    "signal": signal,
                    "companyRevenueGrowthPct": company_growth,
                    "companyFinancialYearEnd": performance["financialYearEnd"],
                    "interpretation": "quantitative_growth_comparison" if comparable else "directional_observation",
                }
            )

    return signals


def economic_backdrop(latest_indicators: list[Record]) -> Record:
    primary = [
        item
        for item in latest_indicators
        if item["relevance"] == "Primary"
        and item["growthPct"] is not None
        and item["indicatorId"] not in PRICE_INDICATORS | {"services_persons_engaged"}
    ]

    directions = {0 if item["growthPct"] == 0 else 1 if item["growthPct"] > 0 else -1 for item in primary}

    if not directions:
        label = "Unavailable"
    elif len(directions) > 1:
        label = "Mixed"
    else:
        label = {1: "Favorable", -1: "Unfavorable", 0: "Stable"}[directions.pop()]

    return {"label": label, "indicatorIds": [item["id"] for item in primary]}


def company_performance(years: list[Record]) -> Record:
    latest = years[-1] if years else {}
    growth = latest.get("revenueGrowth") if latest.get("consecutiveAnnualPeriods") else None
    if len(years) >= 2 and (years[-2]["revenue"] is None or years[-2]["revenue"] <= 0):
        growth = None
    margin = latest.get("netProfitMargin")

    return {
        "financialYearEnd": latest.get("financialYearEnd"),
        "comparisonFinancialYearEnd": latest.get("comparisonFinancialYearEnd"),
        "revenue": latest.get("revenue"),
        "revenueGrowthPct": growth * 100 if growth is not None else None,
        "netProfitMarginPct": margin * 100 if margin is not None else None,
        "netProfitChange": latest.get("netProfitChange"),
    }


def build_economic_context(source: Path | str, *, root: Path = ROOT) -> pd.DataFrame:
    """Return one company row with its business classification and economic context."""
    with Path(source).open(encoding="utf-8") as handle:
        companies: list[Record] = [json.loads(line) for line in handle if line.strip()]

    registrations = [company["registrationNumber"] for company in companies]
    if any(not isinstance(value, str) or not value.strip() for value in registrations) or len(set(registrations)) != len(
        registrations
    ):
        raise ValueError("Company registration numbers must be unique, nonempty strings")

    by_description, codes = read_activity_mappings(root)
    matches = []

    for company in companies:
        profile = company.get("profile") or {}
        matches.append(by_description.get((profile.get("industry"), profile.get("natureOfBusiness"))))

    rules, series, inbound = load_crosswalk(root, codes, {activity for activity in matches if activity is not None})

    annual = annual_calculations(pd.json_normalize(companies))
    by_company = {
        str(registration): cast(list[Record], years.astype(object).fillna(None).to_dict("records"))
        for registration, years in annual.groupby("registrationNumber", sort=False)
    }
    records = []

    for company, activity in zip(companies, matches, strict=True):
        profile = company.get("profile") or {}
        registration = company["registrationNumber"]
        years = by_company.get(registration, [])
        ends = [date.fromisoformat(year["financialYearEnd"]) for year in years]

        if len(ends) != len(set(ends)):
            raise ValueError(f"{registration} has duplicate financial year-ends")

        primary_msic, secondary_msic = codes[activity] if activity is not None else (None, None)
        performance = company_performance(years)
        indicators = []

        if activity is not None:
            active = [rule for rule in rules[activity] if rule["summary_use"] in {"lead", "support"}]
            lead_inbound = any(rule["dataset"] == INBOUND and rule["summary_use"] == "lead" for rule in active)

            for rule in active:
                key = series_key(rule)

                for end in ends or [date(year, 12, 31) for year in CALENDAR_YEARS]:
                    details = metadata(rule, codes[activity][0], lead_inbound and date(end.year, 12, 31) in inbound)
                    indicator = indicator_for_period(details, key, end, bool(ends), series.get(key, {}), inbound)
                    if indicator is not None:
                        indicators.append(indicator)

        indicators.sort(key=lambda item: (item["relevance"] != "Primary", item["periodEnd"], item["id"]))
        latest_indicators = [
            item for item in indicators if item["fiscalWindowAligned"] and item["periodEnd"] == performance["financialYearEnd"]
        ]

        coverage = "Unavailable"
        if indicators:
            coverage = (
                "Available"
                if any(
                    item["relevance"] == "Primary" and item["matchType"] != "thematic_proxy" and item["growthPct"] is not None
                    for item in latest_indicators
                )
                else "Partial"
            )
        signals = contextual_signals(latest_indicators, performance)
        if not indicators:
            signals.append(
                {
                    "signal": "sectorContextUnavailable",
                    "reason": "No verified DOSM series for the supplied activity and periods"
                    if activity
                    else "No activity mapping",
                }
            )

        records.append(
            {
                "registrationNumber": registration,
                "companyName": profile.get("companyName"),
                "economicContext": {
                    "natureOfBusiness": profile.get("natureOfBusiness"),
                    "normalizedActivity": activity,
                    "msicCode": primary_msic,
                    "secondaryMSIC": secondary_msic or None,
                    "msicVersion": "2008" if primary_msic else None,
                    "coverageStatus": coverage,
                    "companyPerformance": performance,
                    "economicBackdrop": economic_backdrop(latest_indicators),
                    "indicators": indicators,
                    "signals": signals,
                },
            }
        )

    return pd.DataFrame.from_records(records, columns=["registrationNumber", "companyName", "economicContext"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, nargs="?", help="path to the company JSONL file")
    args = parser.parse_args()
    source = args.input if args.input is not None else Path(input("Company JSONL path: ").strip()).expanduser()
    context = build_economic_context(source)
    output = source.with_name(f"{source.stem}_economic_context.jsonl")
    output.write_text(
        "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in context.to_dict("records")),
        encoding="utf-8",
    )
    print(f"Wrote economic context for {len(context)} companies to {output}")


if __name__ == "__main__":
    main()
