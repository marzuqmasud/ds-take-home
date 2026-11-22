"""Review a saved summary version against the source and backend evidence."""

import argparse
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from time import sleep, time
from typing import Annotated, Literal

from httpx import RequestError
from openrouter.errors import OpenRouterError
from pydantic import ConfigDict, StringConstraints, ValidationError

from ..models import Model, SummaryLine
from .llm_io import load_setting, parse_companies, write_atomic
from .openrouter import DEFAULT_MODEL, ROOT, request_completion

type NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Finding(Model):
    model_config = ConfigDict(extra="forbid", strict=True)

    severity: Literal["material", "minor"]
    claim: NonEmpty
    evidence: NonEmpty
    correction: NonEmpty


class Judgment(Model):
    model_config = ConfigDict(extra="forbid", strict=True)

    registration_number: NonEmpty
    findings: list[Finding]


def rate_limit(exc: OpenRouterError) -> tuple[str, int | None]:
    try:
        body = json.loads(exc.body)
    except ValueError:
        body = None
    error = body.get("error", {}) if isinstance(body, dict) else {}
    metadata = error.get("metadata") if isinstance(error, dict) else None
    source = metadata.get("limit_source") if isinstance(metadata, Mapping) else None
    message = str(error.get("message", "")).lower() if isinstance(error, dict) else ""

    if "daily" in str(source) or "per-day" in message or "per day" in message or "daily" in message:
        return "the account's daily free-model quota is exhausted", None
    retry = exc.headers.get("Retry-After", "")
    reset = exc.headers.get("X-RateLimit-Reset", "")
    delay = max(1, int(retry)) if retry.isascii() and retry.isdecimal() else None
    if delay is None and reset.isascii() and reset.isdecimal():
        delay = max(1, ceil(int(reset) / 1000 - time()))
    if source == "upstream_provider_shared_pool" or "rate-limited upstream" in message:
        return "the selected provider is temporarily at capacity", delay or 20
    if source == "openrouter_free_tier_per_minute":
        return "the account hit OpenRouter's free-model per-minute limit", delay or 60
    return "OpenRouter did not identify which limit was hit", delay


def judge(root: Path, version: Path, *, reasoning: bool = False) -> Path:
    summaries = [
        SummaryLine.model_validate(row, strict=True, extra="forbid", by_name=False)
        for row in parse_companies(version.read_text(encoding="utf-8"))
    ]
    if not summaries or any(not row.summary.strip() for row in summaries):
        raise ValueError("The summary version must contain a nonempty summary for each listed company")
    source, evidence = (
        {row["registrationNumber"]: row for row in parse_companies((root / "data" / name).read_text(encoding="utf-8"))}
        for name in ("companies.jsonl", "companies_llm_input.jsonl")
    )
    ids = [row.registration_number for row in summaries]
    missing = set(ids) - (source.keys() & evidence.keys())
    if missing:
        raise ValueError(f"Missing source or backend evidence for {', '.join(sorted(missing))}")

    env_file = root / ".env"
    key = load_setting(env_file, "OPENROUTER_API_KEY")
    if not key:
        raise ValueError("Set OPENROUTER_API_KEY in the repository-root .env")
    model = load_setting(env_file, "OPENROUTER_MODEL") or DEFAULT_MODEL
    prompt = (root / "app" / "llm" / "judge_system_prompt.txt").read_text(encoding="utf-8")
    records = [
        {"registrationNumber": k, "summary": row.summary, "source": source[k], "backendEvidence": evidence[k]}
        for k, row in zip(ids, summaries, strict=True)
    ]
    payload = json.dumps(records, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    reminder = f"Review all {len(ids)} records. Return one JSONL object per registration number: {', '.join(ids)}."
    print(f"Sending {len(ids)} summaries to {model} for review", flush=True)
    for attempt in range(3):
        try:
            content = request_completion(key, model, prompt, f"{payload}\n\n{reminder}", max_tokens=65536, reasoning=reasoning)
            break
        except OpenRouterError as exc:
            if exc.status_code != 429:
                raise
            reason, delay = rate_limit(exc)
            if attempt == 2 or delay is None or delay > 60:
                wait_message = f" Retry in about {delay} seconds." if delay is not None else ""
                raise ValueError(f"OpenRouter HTTP 429 for {model}. {reason.capitalize()}.{wait_message}") from None
            print(f"OpenRouter HTTP 429 for {model} ({reason}). Retrying in {delay} seconds.", flush=True)
            sleep(delay)
    else:
        raise AssertionError("Expected a completion or rate-limit error")
    try:
        results = [Judgment.model_validate_json(line, by_name=False) for line in content.strip().split("\n")]
    except ValidationError:
        raise ValueError("OpenRouter returned malformed judgment JSONL") from None
    by_id = {row.registration_number: row for row in results}
    if len(results) != len(ids) or by_id.keys() != set(ids):
        raise ValueError("OpenRouter must return exactly one judgment for every submitted summary")

    judgments = []
    for summary in summaries:
        k = summary.registration_number
        findings = by_id[k].findings
        word_count = len(re.findall(r"\b[\w\u2019-]+\b", summary.summary))
        judgments.append(
            {
                **by_id[k].model_dump(by_alias=True),
                "factualScore": 0 if any(finding.severity == "material" for finding in findings) else 1 if findings else 2,
                "wordCount": word_count,
                "withinWordTarget": 100 <= word_count <= 160,
            }
        )
    report = {"summaryVersion": version.name, "judgeModel": model, "reasoningRequested": reasoning, "judgments": judgments}
    output_dir = root / "build" / "summary-judgments"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{version.stem}-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}.json"
    write_atomic(output, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", type=Path, help="Path to a saved summary JSONL file")
    parser.add_argument("--reasoning", action="store_true", help="Enable model reasoning for this review")
    args = parser.parse_args()
    try:
        output = judge(ROOT, args.version, reasoning=args.reasoning)
    except OpenRouterError as exc:
        raise SystemExit(f"OpenRouter judgment request failed ({type(exc).__name__}, HTTP {exc.status_code})") from None
    except RequestError as exc:
        raise SystemExit(f"Could not reach OpenRouter ({type(exc).__name__})") from None
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    print(f"Published summary judgments to {output}")


if __name__ == "__main__":
    main()
