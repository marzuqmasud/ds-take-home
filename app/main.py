"""The viewer: shows each company's record next to the summaries your system wrote. It only reads files."""

import re
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel

ROOT = Path(__file__).resolve().parent.parent
COMPANIES = ROOT / "data" / "companies.jsonl"
SUMMARIES = ROOT / "summaries"


class Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class FormerName(Model):
    name: str
    changed_on: str


class Profile(Model):
    company_name: str
    status: str
    company_type: str
    incorporation_date: str
    industry: str
    state: str
    nature_of_business: str
    share_capital: int
    former_names: list[FormerName]


class Director(Model):
    name: str
    designation: str
    appointed_on: str


class Shareholder(Model):
    type: Literal["company", "individual"]
    name: str
    percentage: float
    registration_number: str | None = None
    incorporated_in: str | None = None


class Relationships(Model):
    directors: list[Director]
    shareholders: list[Shareholder]


class Audit(Model):
    firm: str
    opinion: str


class Statements(Model):
    revenue: int
    cost_of_sales: int
    gross_profit: int
    operating_expenses: int
    operating_profit: int
    depreciation: int
    interest_expense: int
    profit_before_tax: int
    tax_expense: int
    profit_after_tax: int
    cash: int
    trade_receivables: int
    inventory: int
    current_assets: int
    non_current_assets: int
    total_assets: int
    trade_payables: int
    borrowings: int
    current_liabilities: int
    total_liabilities: int
    total_equity: int
    operating_cash_flow: int
    capital_expenditure: int
    borrowings_repaid: int


class Profitability(Model):
    gross_profit_margin: float
    operating_profit_margin: float
    ebitda: int
    ebitda_margin: float
    operating_expense_ratio: float
    net_profit_margin: float
    tax_efficiency_ratio: float | None


class LiquidityAndCashFlow(Model):
    quick_ratio: float
    cash_ratio: float
    cash_to_revenue_ratio: float
    operating_cash_flow: int
    operating_cash_flow_margin: float
    free_cash_flow: int
    accrual_ratio: float


class OperationalEfficiency(Model):
    cash_conversion_cycle: float
    days_sales_outstanding: float
    days_inventory_outstanding: float | None
    days_payables_outstanding: float
    inventory_turnover: float | None
    receivables_turnover: float
    asset_turnover_ratio: float
    net_working_capital: int


class CapitalStructure(Model):
    leverage_ratio: float
    interest_coverage_ratio: float
    fixed_asset_composition: float
    depreciation_to_asset_ratio: float
    debt_service_coverage_ratio: float


class RatiosAndMetrics(Model):
    profitability: Profitability
    liquidity_and_cash_flow: LiquidityAndCashFlow
    operational_efficiency: OperationalEfficiency
    capital_structure: CapitalStructure


class FinancialYear(Model):
    financial_year: int
    financial_year_end: str
    audit: Audit
    statements: Statements
    ratios_and_metrics: RatiosAndMetrics


class Financials(Model):
    years: list[FinancialYear]


class Case(Model):
    case_number: str
    role: Literal["plaintiff", "defendant"]
    case_type: str
    court: str
    filed_on: str
    status: str
    claim_amount: float


class Litigation(Model):
    cases: list[Case]


class Company(Model):
    registration_number: str
    profile: Profile
    relationships: Relationships
    financials: Financials
    litigation: Litigation


class CompanyListItem(Model):
    registration_number: str
    company_name: str
    industry: str
    state: str


class SummaryLine(Model):
    registration_number: str
    summary: str


class SummaryVersion(Model):
    summaries: dict[str, str]
    invalid_lines: list[int]


app = FastAPI(title="Company summaries viewer")


def load_companies() -> list[Company]:
    lines = COMPANIES.read_text(encoding="utf-8").splitlines()
    companies: list[Company] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            companies.append(Company.model_validate_json(line, strict=True, extra="forbid", by_name=False))
        except ValidationError as exc:
            raise ValueError(f"{COMPANIES}:{number}\n{exc}") from exc
    return companies


@app.get("/api/companies")
def list_companies() -> list[CompanyListItem]:
    return [
        CompanyListItem(
            registration_number=c.registration_number,
            company_name=c.profile.company_name,
            industry=c.profile.industry,
            state=c.profile.state,
        )
        for c in load_companies()
    ]


@app.get("/api/companies/{registration_number}")
def get_company(registration_number: str) -> Company:
    for company in load_companies():
        if company.registration_number == registration_number:
            return company
    raise HTTPException(404, f"No company {registration_number}")


@app.get("/api/summaries")
def list_versions() -> list[str]:
    return sorted(p.stem for p in SUMMARIES.glob("*.jsonl"))


@app.get("/api/summaries/{version}")
def get_version(version: str) -> SummaryVersion:
    path = SUMMARIES / f"{version}.jsonl"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", version) or not path.is_file():
        raise HTTPException(404, f"No summaries/{version}.jsonl")
    summaries: dict[str, str] = {}
    invalid_lines: list[int] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = SummaryLine.model_validate_json(line)
        except ValidationError:
            invalid_lines.append(number)
            continue
        summaries[row.registration_number] = row.summary
    return SummaryVersion(summaries=summaries, invalid_lines=invalid_lines)


app.mount("/", StaticFiles(directory=ROOT / "app" / "static", html=True), name="static")
