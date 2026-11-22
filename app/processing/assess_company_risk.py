"""Build legal, audit, and versioned supplier-risk assessments from a company JSONL file."""

import argparse
import json
from datetime import date
from decimal import Decimal
from math import isfinite
from pathlib import Path
from typing import Any, cast

import pandas as pd

if __package__:
    from .calculate_financials import X_SCORE_VERSION, annual_calculations
else:
    from calculate_financials import X_SCORE_VERSION, annual_calculations  # pyrefly: ignore [missing-import]

RULE_VERSION = "hybrid-screening-1.0"
COLUMNS = ["registrationNumber", "companyName", "legalAnalysis", "auditAnalysis", "riskAssessment"]
FINANCIAL_EVIDENCE = ("revenue", "profitAfterTax", "operatingCashFlow", "currentRatio", "debtServiceCoverageRatio", "xScore")
AUDIT_DISCLAIMER = "AUDIT_DISCLAIMER"
GOING_CONCERN = "MATERIAL_GOING_CONCERN_UNCERTAINTY"

type Record = dict[str, Any]


def normalized(value: str | None) -> str:
    return (value or "").strip().casefold()


def active_flags(*checks: tuple[str, bool]) -> list[str]:
    return [flag for flag, applies in checks if applies]


def analyze_legal(cases: list[Record] | None, status: str | None, revenue: int | float | None) -> tuple[Record, bool]:
    records = cases or []
    if len({case["caseNumber"] for case in records}) != len(records):
        raise ValueError("Duplicate case numbers within a company")

    defendants = [case for case in records if normalized(case.get("role")) == "defendant"]
    pending = [case for case in defendants if normalized(case.get("status")) == "pending"]
    judgments = [case for case in defendants if normalized(case.get("status")) == "judgment entered"]
    pending_winding_up = sum(normalized(case.get("caseType")) == "winding-up petition" for case in pending)
    winding_up_judgments = sum(normalized(case.get("caseType")) == "winding-up petition" for case in judgments)
    qualifying = [case for case in (*pending, *judgments) if normalized(case.get("caseType")) != "winding-up petition"]
    amounts = [case["claimAmount"] for case in qualifying if case.get("claimAmount") is not None]
    if any(type(amount) not in (int, float) or not isfinite(amount) or amount < 0 for amount in amounts):
        raise ValueError("Qualifying claim amounts must be finite, nonnegative numbers or null")

    total = None
    if cases is not None and (amounts or not qualifying):
        total = sum((Decimal(str(amount)) for amount in amounts), start=Decimal(0))

    positive_revenue = Decimal(str(revenue)) if revenue is not None and revenue > 0 else None
    claim_ratio = total / positive_revenue if total is not None and positive_revenue is not None else None
    severe = (
        len(judgments) - winding_up_judgments >= 2
        and len(pending) - pending_winding_up >= 1
        and total is not None
        and positive_revenue is not None
        and total >= Decimal("0.10") * positive_revenue
    )

    return {
        "totalCases": len(records) if cases is not None else None,
        "defendantCases": len(defendants) if cases is not None else None,
        "plaintiffCases": sum(normalized(case.get("role")) == "plaintiff" for case in records) if cases is not None else None,
        "pendingDefendantCases": len(pending) if cases is not None else None,
        "defendantJudgmentRecords": len(judgments) if cases is not None else None,
        "pendingWindingUpPetitions": pending_winding_up if cases is not None else None,
        "windingUpJudgmentRecords": winding_up_judgments if cases is not None else None,
        "qualifyingClaimAmount": float(total) if total is not None else None,
        "claimsToRevenueRatio": float(claim_ratio) if claim_ratio is not None else None,
        "missingQualifyingClaimAmounts": len(qualifying) - len(amounts) if cases is not None else None,
        "latestCaseDate": max(
            (date.fromisoformat(case["filedOn"]).isoformat() for case in records if case.get("filedOn")), default=None
        ),
        "legalFlags": active_flags(
            ("COMPANY_WINDING_UP", normalized(status) in {"winding up", "winding-up"}),
            ("PENDING_WINDING_UP_PETITION", pending_winding_up > 0),
            ("WINDING_UP_JUDGMENT_RECORD", winding_up_judgments > 0),
            ("DEFENDANT_JUDGMENT_RECORDED", bool(judgments)),
            ("PENDING_DEFENDANT_LITIGATION", bool(pending)),
        ),
        "cases": cases,
        "verificationStatus": "Dataset only",
    }, severe


def analyze_audit(years: list[Record]) -> Record:
    history: list[Record] = []
    for year in years:
        audit = year.get("audit") or {}
        opinion = normalized(audit.get("opinion")).replace("-", " ")
        history.append(
            {
                "financialYear": year["financialYear"],
                "financialYearEnd": year["financialYearEnd"],
                "firm": audit.get("firm"),
                "opinion": audit.get("opinion"),
                "flags": active_flags(
                    (AUDIT_DISCLAIMER, "disclaimer" in opinion),
                    (GOING_CONCERN, all(term in opinion for term in ("material", "uncertainty", "going concern"))),
                ),
            }
        )

    latest = history[-1] if history else {}
    flags = {flag for item in history for flag in item["flags"]}
    return {
        "latestAuditYear": latest.get("financialYear"),
        "latestAuditOpinion": latest.get("opinion"),
        "hasDisclaimer": AUDIT_DISCLAIMER in flags,
        "hasGoingConcernUncertainty": GOING_CONCERN in flags,
        "latestAuditException": bool(latest["flags"]) if latest.get("opinion") else None,
        "historicalAuditExceptions": [item for item in history[:-1] if item["flags"]],
        "auditFlags": [flag for flag in (AUDIT_DISCLAIMER, GOING_CONCERN) if flag in flags],
        "auditOpinionHistory": history,
    }


def below(value: int | float | None, threshold: int = 0) -> bool:
    return value is not None and value < threshold


def classify_risk(years: list[Record], legal: Record, audit: Record, severe_legal: bool) -> Record:
    latest = years[-1] if years else {}
    score = latest.get("xScore")
    distress = score > 0 if score is not None else None
    current_ratio_warning = below(latest.get("currentRatio"), 1)
    dscr_warning = below(latest.get("debtServiceCoverageRatio"), 1)
    latest_cash_outflow = below(latest.get("operatingCashFlow"))

    severe_financial = (
        len(years) >= 2
        and bool(latest["consecutiveAnnualPeriods"])
        and all(below(year.get("profitAfterTax")) and below(year.get("operatingCashFlow")) for year in years[-2:])
        and (current_ratio_warning or dscr_warning)
    )

    legal_flags = legal["legalFlags"]
    urgent = active_flags(
        ("COMPANY_WINDING_UP", "COMPANY_WINDING_UP" in legal_flags),
        ("PENDING_WINDING_UP", "PENDING_WINDING_UP_PETITION" in legal_flags),
        ("WINDING_UP_JUDGMENT", "WINDING_UP_JUDGMENT_RECORD" in legal_flags),
    )

    high = active_flags(
        ("ZMIJEWSKI_DISTRESS_WARNING", distress is True),
        ("SEVERE_FINANCIAL_STRESS", severe_financial),
        ("SEVERE_LEGAL_EXPOSURE", severe_legal),
    )

    review = active_flags(
        ("NEGATIVE_OPERATING_CASH_FLOW", latest_cash_outflow and not severe_financial),
        ("CURRENT_RATIO_BELOW_ONE", current_ratio_warning),
        ("DEBT_SERVICE_COVERAGE_BELOW_ONE", dscr_warning),
        ("DEFENDANT_JUDGMENT_RECORDED", "DEFENDANT_JUDGMENT_RECORDED" in legal_flags),
        ("PENDING_DEFENDANT_LITIGATION", "PENDING_DEFENDANT_LITIGATION" in legal_flags),
        ("MATERIAL_AUDIT_EXCEPTION", bool(audit["auditFlags"])),
    )

    missing_financials = ["NO_FINANCIAL_STATEMENTS"] if not years else []
    fallback: tuple[str, list[str]] = ("Lower observed flags", [])
    priority, primary = next(
        (
            (priority, triggers)
            for priority, triggers in (
                ("Urgent", urgent),
                ("Insufficient financial data", missing_financials),
                ("High", high),
                ("Review", review),
            )
            if triggers
        ),
        fallback,
    )

    loss_run = longest_loss_run(years)
    warnings = active_flags(
        ("THREE_CONSECUTIVE_LOSS_YEARS", loss_run == 3),
        ("CONSECUTIVE_LOSS_YEARS", loss_run == 2 or loss_run > 3),
        ("REPEATED_NEGATIVE_OPERATING_CASH_FLOW", sum(below(year.get("operatingCashFlow")) for year in years) >= 2),
        ("NEGATIVE_EQUITY", latest.get("negativeEquity") is True),
    )
    missing_metrics = [field for field in FINANCIAL_EVIDENCE if latest.get(field) is None]
    has_court_records = legal["cases"] is not None
    has_latest_audit = bool(audit["latestAuditOpinion"])
    limited = bool(missing_metrics or not has_court_records or not has_latest_audit or legal["missingQualifyingClaimAmounts"])

    return {
        "ruleVersion": RULE_VERSION,
        "financialModel": {
            "name": "Zmijewski X-Score",
            "version": X_SCORE_VERSION,
            "latestScore": score,
            "distressWarning": distress,
        },
        "priority": priority,
        "primaryTriggers": primary,
        "supportingWarnings": [
            flag for flag in dict.fromkeys([*warnings, *high, *review, *audit["auditFlags"]]) if flag not in primary
        ],
        "legalOverrideApplied": bool(urgent),
        "requiresFurtherReview": priority != "Lower observed flags",
        "evidenceConfidence": {
            "status": "Limited" if limited else "Dataset only",
            "financialYearCount": len(years),
            "latestFinancialYearEnd": latest.get("financialYearEnd"),
            "missingLatestFinancialMetrics": missing_metrics,
            "courtRecordsAvailable": has_court_records,
            "latestAuditOpinionAvailable": has_latest_audit,
        },
    }


def longest_loss_run(years: list[Record]) -> int:
    longest = current = 0
    for year in years:
        if not below(year.get("profitAfterTax")):
            current = 0
        elif year["consecutiveAnnualPeriods"]:
            current += 1
        else:
            current = 1
        longest = max(longest, current)
    return longest


def assess_company_risk(source: Path | str) -> pd.DataFrame:
    """Return one row per company, including companies without annual filings."""
    with Path(source).open(encoding="utf-8") as handle:
        companies: list[Record] = [json.loads(line) for line in handle if line.strip()]
    registrations = [company["registrationNumber"] for company in companies]
    if any(not isinstance(value, str) or not value.strip() for value in registrations) or len(set(registrations)) != len(
        registrations
    ):
        raise ValueError("Company registration numbers must be unique, nonempty strings")

    annual = annual_calculations(pd.json_normalize(companies))
    by_company = {
        str(registration): cast(list[Record], years.astype(object).fillna(None).to_dict("records"))
        for registration, years in annual.groupby("registrationNumber", sort=False)
    }
    assessments = []
    for company in companies:
        registration = company["registrationNumber"]
        profile = company.get("profile") or {}
        history = sorted((company.get("financials") or {}).get("years") or [], key=lambda year: year["financialYearEnd"])
        ends = [date.fromisoformat(year["financialYearEnd"]) for year in history]
        if len(set(ends)) != len(ends) or any(
            end.year != year["financialYear"] for end, year in zip(ends, history, strict=True)
        ):
            raise ValueError(f"{registration} has duplicate or inconsistent financial reporting periods")

        years = by_company.get(registration, [])
        latest = years[-1] if years else {}
        cases = (company.get("litigation") or {}).get("cases")
        legal, severe_legal = analyze_legal(cases, profile.get("status"), latest.get("revenue"))
        audit = analyze_audit(history)
        assessments.append(
            {
                "registrationNumber": registration,
                "companyName": profile.get("companyName"),
                "legalAnalysis": legal,
                "auditAnalysis": audit,
                "riskAssessment": classify_risk(years, legal, audit, severe_legal),
            }
        )
    return pd.DataFrame.from_records(assessments, columns=COLUMNS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, nargs="?", help="path to the company JSONL file")
    args = parser.parse_args()
    source = args.input if args.input is not None else Path(input("Company JSONL path: ").strip()).expanduser()
    assessments = assess_company_risk(source)
    output = source.with_name(f"{source.stem}_assessments.jsonl")
    content = "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in assessments.to_dict("records"))
    output.write_text(content, encoding="utf-8")
    print(f"Wrote assessments for {len(assessments)} companies to {output}")


if __name__ == "__main__":
    main()
