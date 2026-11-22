"""Generate viewer summaries in one request through the OpenRouter Python SDK."""

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from httpx import RequestError
from openrouter import OpenRouter
from openrouter.components import ChatResult
from openrouter.errors import OpenRouterError
from pydantic import ValidationError

from ..models import SummaryLine
from .llm_io import load_setting, parse_companies, write_atomic

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = "baidu/ernie-5.5-flash-preview:free"


def request_completion(key: str, model: str, prompt: str, content: str, *, max_tokens: int, reasoning: bool) -> str:
    with OpenRouter(api_key=key, timeout_ms=600_000) as client:
        response = client.chat.send(
            model=model,
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": content}],
            max_tokens=max_tokens,
            temperature=0.2,
            reasoning={"enabled": reasoning} if reasoning or model == "nvidia/nemotron-3-ultra-550b-a55b:free" else None,
            retries=None,
        )
    if not isinstance(response, ChatResult) or not response.choices:
        raise ValueError("OpenRouter returned no completion")
    choice = response.choices[0]
    if choice.finish_reason != "stop":
        raise ValueError(f"OpenRouter did not finish a complete response ({choice.finish_reason})")
    text = choice.message.content
    if not isinstance(text, str) or not text.strip():
        raise ValueError("OpenRouter returned no response text")
    return text


def run(root: Path, *, smoke: bool, reasoning: bool = False) -> None:
    env_file = root / ".env"
    key = load_setting(env_file, "OPENROUTER_API_KEY")
    if not key:
        raise ValueError("Set OPENROUTER_API_KEY in the repository-root .env")
    model = load_setting(env_file, "OPENROUTER_MODEL") or DEFAULT_MODEL
    prompt = (root / "app" / "llm" / "system_prompt.txt").read_text(encoding="utf-8")
    companies = parse_companies((root / "data" / "companies_llm_input.jsonl").read_text(encoding="utf-8"))
    if not prompt.strip() or not companies:
        raise ValueError("OpenRouter requires a system prompt and at least one company")
    if smoke:
        companies = companies[:1]
    expected = [company["registrationNumber"] for company in companies]
    evidence = json.dumps(companies, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    output_instruction = (
        f"Summarize all {len(expected)} records above. Output exactly {len(expected)} JSONL lines in this order: "
        f'{", ".join(expected)}. Each line must contain only the keys "registrationNumber" and "summary". '
        "Write 100\u2013160 words per company and check each summary's word count before returning it. "
        "Use escaped newlines within each summary. Return the JSONL directly without code fences."
    )
    print(f"Sending {len(companies)} companies to {model} in one request", flush=True)
    content = request_completion(
        key, model, prompt, f"{evidence}\n\n{output_instruction}", max_tokens=4096 if smoke else 32768, reasoning=reasoning
    )
    try:
        rows = [
            SummaryLine.model_validate_json(line, strict=True, extra="forbid", by_name=False)
            for line in content.strip().split("\n")
        ]
    except ValidationError:
        raise ValueError("OpenRouter returned malformed summary JSONL") from None
    by_id = {row.registration_number: row for row in rows}
    if len(rows) != len(expected) or by_id.keys() != set(expected) or any(not row.summary.strip() for row in rows):
        raise ValueError("OpenRouter did not return exactly one nonempty summary for each requested company")
    if smoke:
        print(f"Validated a summary for {expected[0]}")
        return

    output_dir = root / "summaries"
    output_dir.mkdir(exist_ok=True)
    model_slug = re.sub(r"[^A-Za-z0-9._-]", "-", model)
    variant = "-reasoning" if reasoning else ""
    output = output_dir / f"openrouter-{model_slug}{variant}-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}.jsonl"
    write_atomic(output, "".join(by_id[registration].model_dump_json(by_alias=True) + "\n" for registration in expected))
    print(f"Published {len(rows)} summaries to {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="Validate a one-company response without publishing a version")
    parser.add_argument("--reasoning", action="store_true", help="Enable model reasoning for this run")
    args = parser.parse_args()
    try:
        run(ROOT, smoke=args.smoke, reasoning=args.reasoning)
    except OpenRouterError as exc:
        if exc.status_code == 403 and "Key limit exceeded" in exc.body:
            raise SystemExit("OpenRouter API key spending limit exceeded. Update the key limit or use another key.") from None
        raise SystemExit(f"OpenRouter request failed ({type(exc).__name__}, HTTP {exc.status_code})") from None
    except RequestError as exc:
        raise SystemExit(f"Could not reach OpenRouter ({type(exc).__name__})") from None
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
