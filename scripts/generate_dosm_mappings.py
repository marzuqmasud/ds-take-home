"""Build the DOSM activity, company, and fiscal-year CSVs."""

import argparse
import calendar
import csv
import io
import json
import math
import os
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory
from typing import NamedTuple
from urllib.request import urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = "https://open.dosm.gov.my/data-catalogue/"
STORAGE = "https://storage.dosm.gov.my/"
CONSTRUCTION = "https://open.dosm.gov.my/dashboard/construction-statistics"
TOURISM = STORAGE + "tourism/tourism_2025.pdf"
CENSUS = "https://www.dosm.gov.my/portal-main/release-content/economic-census-2023-health-and-social-works-services-sector"
MONTHS = {"monthly": 1, "quarterly": 3, "annual": 12}
CALENDAR_YEARS = (2022, 2023, 2024)


class Dataset(NamedTuple):
    data_url: str
    key_field: str
    frequency: str
    series_filter: str = ""


DATASETS = {
    "ipi_5d": Dataset(STORAGE + "ipi/ipi_5d.csv", "item", "monthly", "abs"),
    "ppi_3d": Dataset(STORAGE + "ppi/ppi_3d.csv", "group", "monthly", "abs"),
    "sppi_3d": Dataset(STORAGE + "ppi/sppi_3d.csv", "group", "quarterly", "index"),
    "iowrt_3d": Dataset(STORAGE + "iowrt/iowrt_3d.csv", "group", "monthly", "abs"),
    "services_volume_activity": Dataset(STORAGE + "services/services_volume_activity.csv", "activity", "quarterly"),
    "services_volume_subsector": Dataset(STORAGE + "services/services_volume_subsector.csv", "subsector", "quarterly"),
    "services_subsector": Dataset(STORAGE + "services/services_subsector.csv", "subsector", "quarterly"),
    "gdp_qtr_real_supply_granular": Dataset(STORAGE + "gdp/gdp_qtr_real_supply_sub.csv", "sector", "quarterly", "abs"),
    "gdp_annual_real_supply_granular": Dataset(STORAGE + "gdp/gdp_annual_real_supply_sub.csv", "sector", "annual", "abs"),
    "construction_statistics": Dataset(CONSTRUCTION, "project_type", "quarterly"),
    "tourism_satellite_account_2025": Dataset(TOURISM, "table_row", "annual"),
}

METRICS = {
    ("ipi_5d", "index"): ("industrial_production_index", "2015 = 100", "mean"),
    ("ppi_3d", "index"): ("producer_price_index", "2010 = 100", "mean"),
    ("sppi_3d", "index"): ("services_producer_price_index", "2010 = 100", "mean"),
    ("iowrt_3d", "sales"): ("nominal_trade_sales", "MYR million", "sum"),
    ("iowrt_3d", "volume"): ("trade_volume_index", "2015 = 100", "mean"),
    ("services_volume_activity", "volume"): ("services_volume_index", "2015 = 100", "mean"),
    ("services_volume_subsector", "volume"): ("services_volume_index", "2015 = 100", "mean"),
    ("services_subsector", "revenue"): ("nominal_services_revenue", "MYR", "sum"),
    ("gdp_qtr_real_supply_granular", "value"): ("real_value_added", "MYR million at 2015 prices", "sum"),
    ("gdp_annual_real_supply_granular", "value"): ("real_value_added", "MYR million at 2015 prices", "sum"),
    ("construction_statistics", "value"): ("nominal_value_of_work_done", "MYR million", "sum"),
    ("tourism_satellite_account_2025", "value"): ("nominal_gross_value_added", "MYR million", "sum"),
}


def fetch(url):
    if not url.startswith(("https://storage.dosm.gov.my/", "https://open.dosm.gov.my/")):
        raise ValueError(f"Unexpected DOSM download URL: {url}")
    try:
        with urlopen(url, timeout=30) as response:  # noqa: S310
            return response.read()
    except OSError as error:
        raise ValueError(f"Could not download {url}: {error}") from error


def csv_rows(content):
    return csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline=""))


def construction_rows(content):
    # shortcut: read the dashboard's chart payload until DOSM offers the same series as a catalogue CSV.
    match = re.search(rb'<script\b[^>]*\bid="__NEXT_DATA__"[^>]*>(.*?)</script>', content, re.DOTALL)
    if not match:
        raise ValueError("OpenDOSM construction dashboard no longer exposes its chart payload")
    props = json.loads(match[1])["props"]["pageProps"]
    data = props["project"]["data"]
    owners = [data[name]["value"] for name in ("government", "private", "publiccorp")]
    headline = props["timeseries"]["data"]["actual"]
    dates = headline["x"]
    if any(owner["x"] != dates for owner in owners):
        raise ValueError("Construction dashboard owner series have different dates")
    owner_totals = zip(*(owner["total"] for owner in owners), headline["value"], dates, strict=True)
    for government, private, publiccorp, total, stamp in owner_totals:
        if not math.isclose(government + private + publiccorp, total * 1000, rel_tol=1e-10, abs_tol=2000):
            raise ValueError(f"Construction owner totals do not reconcile at timestamp {stamp}")
    for key in ("civileng", "special"):
        for stamp, *values in zip(dates, *(owner[key] for owner in owners), strict=True):
            day = datetime.fromtimestamp(stamp / 1000, UTC).date()
            yield {"date": day.isoformat(), "project_type": key, "value": sum(values) / 10**6}


def tourism_rows(content):
    # shortcut: extract Table 5 from this release until a stable machine-readable series is available.
    executable = shutil.which("pdftotext")
    if executable is None:
        raise ValueError("Install pdftotext (Poppler) to read the DOSM Tourism Satellite Account")
    try:
        result = subprocess.run(  # noqa: S603
            [executable, "-layout", "-", "-"],
            input=content,
            capture_output=True,
            check=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"Could not extract the DOSM Tourism Satellite Account: {error}") from error
    label = "Travel agencies and other reservation services"
    for page in result.stdout.decode("utf-8").split("\f"):
        if "Gross Value Added of Tourism Industries at current prices" not in page:
            continue
        match = re.search(r"Perkhidmatan agensi pengembaraan dan penempahan lain\s+([\d,. ]+)\n" + label, page)
        header = re.search(r"^ *(2019\s+2020\s+2021\s+2022\s+2023\s+2024e?\s+2025p?) *$", page, re.MULTILINE)
        if match and header:
            for year, value in zip(re.findall(r"\d{4}", header[1]), match[1].split(), strict=True):
                yield {"date": f"{year}-01-01", "table_row": f"Table 5 | {label}", "value": value.replace(",", "")}
            return
    raise ValueError("Could not locate the travel-agency row and year headings in the DOSM Tourism Satellite Account")


def load_series(rules, read_source):
    series = defaultdict(dict)
    for dataset_name, source in DATASETS.items():
        selected = defaultdict(set)
        for rule in rules:
            if rule["dataset"] == dataset_name:
                selected[rule["dosm_key"]].add(rule["value_field"])
        if not selected:
            continue
        content = read_source(source.data_url)
        parser = {"construction_statistics": construction_rows, "tourism_satellite_account_2025": tourism_rows}
        for row in parser.get(dataset_name, csv_rows)(content):
            if source.series_filter and row["series"] != source.series_filter:
                continue
            key = row[source.key_field]
            for field in selected.get(key, ()):
                if row[field] in ("", None):
                    continue
                day = date.fromisoformat(row["date"])
                identifier = (dataset_name, key, field)
                value = float(row[field])
                if day in series[identifier] or not math.isfinite(value):
                    raise ValueError(f"Invalid or duplicate observation for {identifier} on {day}")
                series[identifier][day] = value
    return series


def shift_month(day, offset):
    year, month = divmod(day.year * 12 + day.month - 1 + offset, 12)
    return date(year, month + 1, 1)


def aggregate(values, year_end, frequency, aggregation):
    step = MONTHS[frequency]
    if year_end.month % step or year_end.day != calendar.monthrange(year_end.year, year_end.month)[1]:
        return None
    start = shift_month(year_end, -11)
    points = [values.get(shift_month(start, offset)) for offset in range(0, 12, step)]
    if any(point is None for point in points):
        return None
    return mean(points) if aggregation == "mean" else sum(points)


def measure(values, year_end, frequency, aggregation):
    value = aggregate(values, year_end, frequency, aggregation)
    if value is None:
        return "", ""
    last_year = year_end.year - 1
    month = year_end.month
    prior_end = date(last_year, month, calendar.monthrange(last_year, month)[1])
    previous = aggregate(values, prior_end, frequency, aggregation)
    growth = "" if previous in (None, 0) else round(100 * (value / previous - 1), 2)
    return round(value, 3), growth


def read_activity_mappings(root):
    by_description = {}
    codes_by_activity = {}
    for row in csv_rows((root / "mappings/activity_mapping.csv").read_bytes()):
        activity = row["normalizedActivity"]
        key = (row["industry"], row["natureOfBusinessVerbatim"])
        primary = row["primaryCode"].strip()
        secondary = row["secondaryCode"].strip()
        if any(not re.fullmatch(r"[0-9]{1,5}", code) for code in (primary, secondary) if code):
            raise ValueError(f"Invalid MSIC item code for {key}")
        codes = tuple(code.zfill(5) if code else "" for code in (primary, secondary))
        if not activity or not primary or key in by_description or codes_by_activity.setdefault(activity, codes) != codes:
            raise ValueError(f"Ambiguous or incomplete activity mapping for {key}")
        by_description[key] = activity
    return by_description, codes_by_activity


def read_inputs(root):
    by_description, codes_by_activity = read_activity_mappings(root)
    companies = {}
    for line in (root / "data/companies.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        company = json.loads(line)
        registration = company["registrationNumber"]
        profile = company["profile"]
        key = (profile["industry"], profile["natureOfBusiness"])
        if not registration or registration in companies or key not in by_description:
            raise ValueError(f"Duplicate registration or missing activity mapping for {registration}: {key}")
        companies[registration] = (company, by_description[key])
    if not companies:
        raise ValueError("No companies to map")
    return codes_by_activity, companies


def read_rules(root, codes_by_activity, valid_codes):
    by_activity = defaultdict(list)
    for rule in csv_rows((root / "mappings/dosm_series_rules.csv").read_bytes()):
        activity = rule["normalized_activity"]
        if activity not in codes_by_activity:
            continue
        primary, secondary = codes_by_activity[activity]
        codes = rule["applied_msic_2008"]
        role = rule["code_role"]
        expected_codes = {"primary": primary, "secondary": secondary, "primary_and_secondary": f"{primary}|{secondary}"}
        if (role != "classification_alternative" and codes != expected_codes.get(role)) or any(
            code not in valid_codes for code in codes.split("|")
        ):
            raise ValueError(f"MSIC code or role changed for {activity}. Review dosm_series_rules.csv")
        dataset = rule["dataset"]
        use = rule["summary_use"]
        if dataset:
            if (
                use not in {"lead", "support", "conditional"}
                or (dataset, rule["value_field"]) not in METRICS
                or not rule["dosm_key"]
            ):
                raise ValueError(f"Invalid DOSM series for {activity}: {rule}")
        elif use != "omit":
            raise ValueError(f"Missing DOSM series for {activity}")
        by_activity[activity].append(rule)
    for activity in codes_by_activity:
        rows = by_activity[activity]
        lead_count = sum(row["summary_use"] == "lead" for row in rows)
        primary_gap = len(rows) == 1 and rows[0]["summary_use"] == "omit" and rows[0]["code_role"] == "primary"
        keys = {(row["dataset"], row["dosm_key"], row["value_field"]) for row in rows}
        if (lead_count != 1 and not primary_gap) or len(rows) != len(keys):
            raise ValueError(
                f"Expected one lead series or an explicit coverage gap for {activity}. Review dosm_series_rules.csv"
            )
    return by_activity


def activity_matrix(codes_by_activity, companies, rules, series, retrieved_dates):
    counts = Counter(activity for _, activity in companies.values())
    for activity, (primary, secondary) in codes_by_activity.items():
        for rule in rules[activity]:
            dataset, key, field = (rule[column] for column in ("dataset", "dosm_key", "value_field"))
            series_key = (dataset, key, field)
            source = DATASETS.get(dataset, Dataset("", "", ""))
            indicator, unit, aggregation = METRICS.get((dataset, field), ("", "", ""))
            periods = {}
            for year in CALENDAR_YEARS:
                value, growth = "", ""
                if dataset:
                    value, growth = measure(series[series_key], date(year, 12, 31), source.frequency, aggregation)
                    if value == "" or growth == "":
                        raise ValueError(f"Incomplete DOSM observations for {activity}, {series_key}, CY{year}")
                periods.update({f"cy{year}_value": value, f"cy{year}_yoy_pct": growth})
            yield dict(
                normalized_activity=activity,
                company_count=counts[activity],
                primary_msic_2008=primary,
                secondary_msic_2008=secondary,
                applied_msic_2008=rule["applied_msic_2008"],
                code_role=rule["code_role"],
                summary_use=rule["summary_use"],
                coverage_status="verified_2022_2024" if dataset else "no_verified_recurring_series",
                match_level=rule["match_level"],
                published_scope=rule["published_scope"],
                geography="Malaysia",
                series_id=":".join(series_key) if dataset else "",
                dataset=dataset,
                dosm_key_field=source.key_field,
                dosm_key=key,
                value_field=field,
                series_filter=source.series_filter,
                indicator=indicator,
                frequency=source.frequency,
                aggregation=aggregation,
                unit=unit,
                source_url=(
                    source.data_url
                    if dataset in {"construction_statistics", "tourism_satellite_account_2025"}
                    else CATALOGUE + dataset
                    if dataset
                    else CENSUS
                ),
                data_url=source.data_url,
                retrieved_on=retrieved_dates[source.data_url] if dataset else "",
                **periods,
                screening_note=rule["screening_note"],
            )


FISCAL_FIELDS = (
    "registration_number",
    "normalized_activity",
    "financial_year",
    "window_start",
    "financial_year_end",
    "series_id",
    "summary_use",
    "indicator",
    "published_scope",
    "match_level",
    "value",
    "unit",
    "yoy_pct",
)


def company_outputs(companies, codes_by_activity, matrix, series):
    by_activity = defaultdict(list)
    for row in matrix:
        if row["summary_use"] in {"lead", "support"}:
            by_activity[row["normalized_activity"]].append(row)
    company_map, context = [], []

    for registration, (company, activity) in companies.items():
        profile = company["profile"]
        primary, secondary = codes_by_activity[activity]
        rows = by_activity[activity]
        lead = next((row for row in rows if row["summary_use"] == "lead"), None)
        dates_seen, matched_lead = set(), set()
        ends = []

        for financial in company["financials"]["years"]:
            year_end = date.fromisoformat(financial["financialYearEnd"])

            if year_end in dates_seen or financial["financialYear"] != year_end.year:
                raise ValueError(f"Duplicate or inconsistent financial year for {registration}: {year_end}")

            dates_seen.add(year_end)
            ends.append(year_end.isoformat())

            for row in rows:
                key = (row["dataset"], row["dosm_key"], row["value_field"])
                value, growth = measure(series[key], year_end, row["frequency"], row["aggregation"])

                if value == "" or growth == "":
                    continue

                if row is lead:
                    matched_lead.add(year_end)
                context.append(
                    {
                        "registration_number": registration,
                        "normalized_activity": activity,
                        "financial_year": year_end.year,
                        "window_start": shift_month(year_end, -11).isoformat(),
                        "financial_year_end": year_end.isoformat(),
                        "series_id": row["series_id"],
                        "summary_use": row["summary_use"],
                        "indicator": row["indicator"],
                        "published_scope": row["published_scope"],
                        "match_level": row["match_level"],
                        "value": value,
                        "unit": row["unit"],
                        "yoy_pct": growth,
                    }
                )
        if lead is None:
            status = "no_verified_recurring_series"

        elif not ends:
            status = "calendar_context_no_company_financials"

        elif matched_lead == dates_seen:
            status = "fiscal_context_available"

        elif matched_lead:
            status = "partial_fiscal_context"

        elif lead["frequency"] == "annual" and all(day.month != 12 for day in dates_seen):
            status = "calendar_context_only_annual_source"

        else:
            status = "calendar_context_no_aligned_period"

        company_map.append(
            {
                "registration_number": registration,
                "company_name": profile["companyName"],
                "dataset_industry": profile["industry"],
                "state": profile["state"],
                "nature_of_business": profile["natureOfBusiness"],
                "normalized_activity": activity,
                "primary_msic_2008": primary,
                "secondary_msic_2008": secondary,
                "lead_series_id": lead["series_id"] if lead else "",
                "financial_year_ends": "|".join(ends),
                "coverage_status": status,
            }
        )
    return company_map, context


def generate(root, output_dir, *, refresh=False):
    source_dir = root / "data/dosm"
    local_timezone = ZoneInfo("Asia/Kuala_Lumpur")
    downloads = {}
    retrieved_dates = {}

    def read_source(url):
        filename = "construction-statistics.html" if url == CONSTRUCTION else url.rsplit("/", 1)[-1]
        if not refresh:
            try:
                with (source_dir / filename).open("rb") as handle:
                    modified = os.fstat(handle.fileno()).st_mtime
                    retrieved_dates[url] = datetime.fromtimestamp(modified, local_timezone).date().isoformat()
                    return handle.read()
            except FileNotFoundError:
                pass
        content = fetch(url)
        downloads[filename] = content
        retrieved_dates[url] = datetime.now(local_timezone).date().isoformat()
        return content

    codes, companies = read_inputs(root)
    items = csv_rows(read_source(STORAGE + "dictionaries/msic.csv"))
    valid_codes = {row["item"] for row in items if row["digits"] == "5" and row["canonical"] == "True"}
    rules = read_rules(root, codes, valid_codes)
    series = load_series([rule for group in rules.values() for rule in group], read_source)
    matrix = list(activity_matrix(codes, companies, rules, series, retrieved_dates))
    company_map, context = company_outputs(companies, codes, matrix, series)

    outputs = {
        "dosm_activity_coverage.csv": (matrix[0].keys(), matrix),
        "dosm_company_mapping.csv": (company_map[0].keys(), company_map),
        "dosm_company_fiscal_context.csv": (FISCAL_FIELDS, context),
    }

    for destination, files in ((source_dir, downloads), (output_dir, outputs)):
        if not files:
            continue
        destination.mkdir(parents=True, exist_ok=True)
        for filename in files:
            if (destination / filename).is_dir():
                raise ValueError(f"Output path is a directory: {destination / filename}")

    with TemporaryDirectory(dir=output_dir) as output_stage:
        for filename, (fields, rows) in outputs.items():
            with (Path(output_stage) / filename).open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
        if downloads:
            with TemporaryDirectory(dir=source_dir) as source_stage:
                for filename, content in downloads.items():
                    (Path(source_stage) / filename).write_bytes(content)
                for filename in downloads:
                    (Path(source_stage) / filename).replace(source_dir / filename)
        for filename in outputs:
            (Path(output_stage) / filename).replace(output_dir / filename)
    return len(codes), len(company_map), len(context)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "mappings", help="destination (default: mappings/)")
    parser.add_argument("--refresh", action="store_true", help="redownload all required DOSM sources")
    args = parser.parse_args()
    try:
        activity_count, company_count, context_count = generate(ROOT, args.output_dir, refresh=args.refresh)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"DOSM generation failed: {error}\n")
    print(
        f"Wrote {activity_count} activities, {company_count} companies and {context_count} fiscal observations "
        f"to {args.output_dir.resolve()} using DOSM sources in {ROOT / 'data/dosm'}"
    )


if __name__ == "__main__":
    main()
