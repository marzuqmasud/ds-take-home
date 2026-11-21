"""Calculate annual financial metrics and trends from a company JSONL file using pandas."""

import argparse
import json
from pathlib import Path

import pandas as pd

METRICS = [
    "returnOnAssets",
    "grossProfitMargin",
    "operatingProfitMargin",
    "netProfitMargin",
    "ebitda",
    "ebitdaMargin",
    "currentRatio",
    "quickRatio",
    "cashRatio",
    "netWorkingCapital",
    "liabilitiesToAssets",
    "liabilitiesToEquity",
    "operatingCashFlowMargin",
    "freeCashFlow",
    "interestCoverageRatio",
    "debtServiceCoverageRatio",
    "cashConversionOfProfit",
    "negativeEquity",
]
IDENTIFIERS = ["registrationNumber", "companyName", "financialYear", "financialYearEnd"]


def ratio(numerator, denominator):
    return numerator / denominator.where(denominator.ne(0))


X_SCORE_VERSION = "plus-liquidity-1.0"
X_SCORE_COEFFICIENTS = {
    "returnOnAssets": -4.513,
    "liabilitiesToAssets": 5.679,
    "currentRatio": 0.004,
}
X_SCORE_INTERCEPT = -4.336

TREND_THRESHOLDS = {
    "revenueGrowth": 0.05,
    "netProfitMarginChange": 0.01,
    "operatingCashFlowMarginChange": 0.01,
    "currentRatioChange": 0.1,
    "dscrChange": 0.1,
}


def add_trends(result):
    companies = result["registrationNumber"]
    grouped = result.groupby(companies, sort=False)
    end = pd.to_datetime(result["financialYearEnd"])
    prior_end = end.groupby(companies).shift()

    consecutive = end.eq(prior_end + pd.DateOffset(years=1)) | (
        end.dt.is_month_end
        & prior_end.dt.is_month_end
        & end.dt.month.eq(prior_end.dt.month)
        & end.dt.year.eq(prior_end.dt.year + 1)
    )

    result["comparisonFinancialYearEnd"] = grouped["financialYearEnd"].shift()
    result["consecutiveAnnualPeriods"] = consecutive.astype("boolean").mask(prior_end.isna())
    profit = result["profitAfterTax"]
    prior_profit = grouped["profitAfterTax"].shift()
    cash_flow = result["operatingCashFlow"]
    result["revenueGrowth"] = ratio(result["revenue"], grouped["revenue"].shift()) - 1

    for metric, change in {
        "profitAfterTax": "netProfitChange",
        "netProfitMargin": "netProfitMarginChange",
        "operatingCashFlow": "operatingCashFlowChange",
        "operatingCashFlowMargin": "operatingCashFlowMarginChange",
        "freeCashFlow": "freeCashFlowChange",
        "currentRatio": "currentRatioChange",
        "debtServiceCoverageRatio": "dscrChange"}.items():
        result[change] = grouped[metric].diff()

    comparable_profit = profit.notna() & prior_profit.notna()
    result["profitabilityTurnaround"] = (prior_profit.lt(0) & profit.gt(0)).astype("boolean").where(comparable_profit)
    result["profitTurnedNegative"] = (prior_profit.gt(0) & profit.lt(0)).astype("boolean").where(comparable_profit)

    for name, change in {"profitDeclinedYoY": "netProfitChange", "marginDeclinedYoY": "netProfitMarginChange"}.items():
        result[name] = result[change].lt(0).astype("boolean").where(consecutive & result[change].notna())

    result["decliningProfitDespiteRevenueGrowth"] = result["revenueGrowth"].gt(0) & result["netProfitChange"].lt(0)
    result["dscrBelowOne"] = result["debtServiceCoverageRatio"].lt(1)
    result["currentRatioBelowOne"] = result["currentRatio"].lt(1)
    result["cashOutflowDespiteProfit"] = cash_flow.lt(0) & profit.gt(0)

    return result


def historical_summary(years):
    losses = years["profitAfterTax"].lt(0)

    def longest_loss_run(breaks):
        return int(losses.groupby(breaks.cumsum()).cumsum().max()) if len(losses) else 0

    negative_ocf = int(years["operatingCashFlow"].lt(0).sum())
    thresholds = pd.Series(TREND_THRESHOLDS)

    changes = years[thresholds.index]
    improved = changes.ge(thresholds).any().any() or years["profitabilityTurnaround"].any()
    declined = changes.le(-thresholds).any().any() or years["profitTurnedNegative"].any()
    trend = "Insufficient Data"

    if improved or declined or changes.notna().any().any():
        trend = {(True, True): "Mixed", (True, False): "Improving", (False, True): "Deteriorating", (False, False): "Stable"}[
            improved, declined
        ]

    direction = "Insufficient Data"
    score_changes = years["xScoreChange"].iloc[1:]

    if len(score_changes) and score_changes.notna().all():
        direction = "Mixed"

        for matches, label in [
            (score_changes.gt(0), "Increasing"),
            (score_changes.lt(0), "Decreasing"),
            (score_changes.eq(0), "Stable")]:

            if matches.all():
                direction = label
                break

    return {
        "consecutiveLossYears": longest_loss_run(~losses | ~years["consecutiveAnnualPeriods"].fillna(False)),
        "consecutiveAvailableLossObservations": longest_loss_run(~losses),
        "negativeOperatingCashFlowYears": negative_ocf,
        "negativeFreeCashFlowYears": int(years["freeCashFlow"].lt(0).sum()),
        "repeatedOperatingCashOutflows": negative_ocf >= 2,
        "financialTrend": trend,
        "xScoreDirection": direction,
    }


def add_x_score(result):
    inputs = result[list(X_SCORE_COEFFICIENTS)]
    weighted = inputs.mul(pd.Series(X_SCORE_COEFFICIENTS))
    score = X_SCORE_INTERCEPT + weighted.sum(axis=1, min_count=len(X_SCORE_COEFFICIENTS))
    result["xScore"] = score
    result["distressWarning"] = score.gt(0).astype("boolean").mask(score.isna())
    result["xScoreModelVersion"] = X_SCORE_VERSION

    result["xScoreChange"] = score.groupby(result["registrationNumber"]).diff()
    return result


def annual_calculations(companies):
    if "financials.years" not in companies:
        return add_x_score(
            add_trends(pd.DataFrame(columns=[*IDENTIFIERS, *METRICS, "revenue", "profitAfterTax", "operatingCashFlow"]))
        )

    years = companies.reindex(columns=["registrationNumber", "profile.companyName", "financials.years"])
    years = years.explode("financials.years").dropna(subset=["financials.years"]).reset_index(drop=True)
    periods = pd.json_normalize(years["financials.years"])

    def amount(field):
        return pd.to_numeric(periods.get(f"statements.{field}", pd.Series(index=years.index, dtype=float)))

    revenue = amount("revenue")
    profit = amount("profitAfterTax")
    operating_profit = amount("operatingProfit")
    ebitda = operating_profit + amount("depreciation")
    assets = amount("totalAssets")
    liabilities = amount("totalLiabilities")
    equity = amount("totalEquity")
    current_assets = amount("currentAssets")
    current_liabilities = amount("currentLiabilities")
    cash_flow = amount("operatingCashFlow")
    interest = amount("interestExpense")

    result = pd.DataFrame(
        {
            "registrationNumber": years["registrationNumber"],
            "companyName": years["profile.companyName"],
            "financialYear": periods.get("financialYear"),
            "financialYearEnd": periods.get("financialYearEnd"),
            "revenue": revenue,
            "profitAfterTax": profit,
            "operatingCashFlow": cash_flow,
            "returnOnAssets": ratio(profit, assets),
            "grossProfitMargin": ratio(amount("grossProfit"), revenue),
            "operatingProfitMargin": ratio(operating_profit, revenue),
            "netProfitMargin": ratio(profit, revenue),
            "ebitda": ebitda,
            "ebitdaMargin": ratio(ebitda, revenue),
            "currentRatio": ratio(current_assets, current_liabilities),
            "quickRatio": ratio(current_assets - amount("inventory"), current_liabilities),
            "cashRatio": ratio(amount("cash"), current_liabilities),
            "netWorkingCapital": current_assets - current_liabilities,
            "liabilitiesToAssets": ratio(liabilities, assets),
            "liabilitiesToEquity": ratio(liabilities, equity),
            "operatingCashFlowMargin": ratio(cash_flow, revenue),
            "freeCashFlow": cash_flow - amount("capitalExpenditure"),
            "interestCoverageRatio": ratio(operating_profit, interest),
            "debtServiceCoverageRatio": ratio(ebitda, interest + amount("borrowingsRepaid")),
            "cashConversionOfProfit": ratio(cash_flow, profit.where(profit.gt(0))),
            "negativeEquity": equity.lt(0).astype("boolean").mask(equity.isna()),
        }
    )
    return add_x_score(add_trends(result.sort_values(["registrationNumber", "financialYearEnd"], ignore_index=True)))


def calculate_financials(source: Path) -> pd.DataFrame:
    companies = pd.json_normalize(pd.read_json(source, lines=True, dtype=False).to_dict("records"))
    annual = annual_calculations(companies)
    by_company = dict(tuple(annual.groupby("registrationNumber", sort=False)))
    identities = companies.reindex(columns=["registrationNumber", "profile.companyName"])
    records = []

    for company in json.loads(identities.rename(columns={"profile.companyName": "companyName"}).to_json(orient="records")):
        years = by_company.get(company["registrationNumber"], annual.iloc[:0])
        metrics = years.drop(columns=["registrationNumber", "companyName", "revenue", "profitAfterTax", "operatingCashFlow"])
        records.append(
            {
                **company,
                "annualMetrics": json.loads(metrics.to_json(orient="records", double_precision=15)),
                "historicalSummary": historical_summary(years),
            }
        )
    return pd.DataFrame.from_records(
        records, columns=["registrationNumber", "companyName", "annualMetrics", "historicalSummary"]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, nargs="?", help="path to the company JSONL file")
    args = parser.parse_args()
    source = args.input if args.input is not None else Path(input("Company JSONL path: ").strip()).expanduser()
    result = calculate_financials(source)
    output = source.with_name(f"{source.stem}_calculations.jsonl")
    output.write_text(
        "".join(json.dumps(company, ensure_ascii=False) + "\n" for company in result.to_dict("records")), encoding="utf-8"
    )
    print(f"Wrote calculations for {len(result)} companies to {output}")


if __name__ == "__main__":
    main()
