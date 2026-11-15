"""JSONL datasets with SHA-256 integrity checks.

Layout:  evals/datasets/<suite>/{dev,test}.jsonl  +  evals/datasets/<suite>/SHA256SUMS
SHA256SUMS uses the ``sha256sum`` format: ``<hex>  <filename>`` per line.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SUMS_FILE = "SHA256SUMS"


class DatasetError(Exception): ...


class DatasetIntegrityError(DatasetError): ...


class EvalItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1)
    input: Any
    expected: Any
    tags: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class Dataset:
    suite: str
    split: str
    path: Path
    sha256: str
    items: list[EvalItem]


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_sums(directory: Path) -> dict[str, str]:
    sums_path = directory / SUMS_FILE
    if not sums_path.exists():
        return {}
    sums: dict[str, str] = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, _, name = line.strip().partition("  ")
            sums[name.strip()] = digest.strip()
    return sums


def write_sums(directory: Path) -> dict[str, str]:
    """Freeze every .jsonl in ``directory``. Run once, when a split is created."""
    sums = {p.name: file_sha256(p) for p in sorted(directory.glob("*.jsonl"))}
    (directory / SUMS_FILE).write_text(
        "".join(f"{d}  {n}\n" for n, d in sums.items()), encoding="utf-8", newline="\n"
    )
    return sums


def load_dataset(path: Path, *, require_frozen: bool) -> Dataset:
    """Load and validate a JSONL split. If SHA256SUMS lists the file, the hash must match;
    if ``require_frozen`` (always for held-out test sets), it must be listed."""
    if not path.exists():
        raise DatasetError(f"dataset not found: {path}")
    digest = file_sha256(path)
    expected = read_sums(path.parent).get(path.name)
    if expected is None and require_frozen:
        raise DatasetIntegrityError(f"{path.name} is not listed in {SUMS_FILE}; freeze it first")
    if expected is not None and expected != digest:
        raise DatasetIntegrityError(
            f"{path.name} changed since it was frozen (expected {expected[:12]}…, got "
            f"{digest[:12]}…). Frozen sets must not be edited."
        )

    items: list[EvalItem] = []
    seen: set[str] = set()
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = EvalItem.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValidationError) as e:
            raise DatasetError(f"{path.name}:{n}: {e}") from e
        if item.id in seen:
            raise DatasetError(f"{path.name}:{n}: duplicate id {item.id!r}")
        seen.add(item.id)
        items.append(item)
    if not items:
        raise DatasetError(f"{path.name} is empty")
    return Dataset(suite=path.parent.name, split=path.stem, path=path, sha256=digest, items=items)
