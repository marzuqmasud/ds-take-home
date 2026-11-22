"""Pydantic models for company records and viewer summaries."""

from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


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
