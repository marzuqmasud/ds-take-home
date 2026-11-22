"""Read generation inputs and publish completed files."""

import json
import os
import shlex
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

type Payload = dict[str, Any]


def load_setting(env_file: Path, setting: str) -> str:
    value = os.environ.get(setting, "").strip()
    if value or not env_file.is_file():
        return value
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        name, separator, value = line.strip().removeprefix("export ").partition("=")
        if separator and name.strip() == setting:
            try:
                values = shlex.split(value, comments=True)
            except ValueError:
                raise ValueError(f"Invalid {setting} assignment in .env") from None
            if len(values) > 1:
                raise ValueError(f"Invalid {setting} assignment in .env")
            return values[0] if values else ""
    return ""


def parse_companies(source: str) -> list[Payload]:
    companies: list[Payload] = []
    registrations: set[str] = set()
    for line in source.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        registration = item.get("registrationNumber") if isinstance(item, dict) else None
        if not isinstance(registration, str) or not registration.strip() or registration in registrations:
            raise ValueError("LLM input must contain a unique registration number per company")
        companies.append(item)
        registrations.add(registration)
    return companies


def write_atomic(path: Path, content: str) -> None:
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        staged = Path(handle.name)
        try:
            handle.write(content)
            handle.close()
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)
