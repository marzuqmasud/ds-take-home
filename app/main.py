"""Serve the company viewer and saved summary versions."""

import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .models import Company, CompanyListItem, SummaryLine, SummaryVersion

ROOT = Path(__file__).resolve().parent.parent
COMPANIES = ROOT / "data" / "companies.jsonl"
SUMMARIES = ROOT / "summaries"


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
