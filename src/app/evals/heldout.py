"""Held-out guard: a frozen test set is run once per project. Re-runs need --force and are
logged, so 'tuned on the test set' can be checked from the repo history."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

HEADER = (
    "# Held-out test set runs\n\n"
    "Every run of a frozen test split is recorded here. More than one row for the same\n"
    "suite and dataset hash means the set was re-run (forced) and must be disclosed.\n\n"
    "| UTC time | suite | split | dataset sha256 | git | forced |\n"
    "|---|---|---|---|---|---|\n"
)


class HeldOutAlreadyRun(Exception): ...


def _key(suite: str, split: str, sha: str) -> str:
    return f"| {suite} | {split} | {sha} |"


def check_heldout(log_path: Path, suite: str, split: str, dataset_sha: str, force: bool) -> None:
    if not log_path.exists():
        return
    if _key(suite, split, dataset_sha) in log_path.read_text(encoding="utf-8") and not force:
        raise HeldOutAlreadyRun(
            f"{suite}/{split} (sha {dataset_sha[:12]}…) was already run. Re-running a held-out "
            "set is a disclosure event: pass --force and explain why in your journal."
        )


def record_heldout(
    log_path: Path, suite: str, split: str, dataset_sha: str, git: str, forced: bool
) -> None:
    if not log_path.exists():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(HEADER, encoding="utf-8", newline="\n")
    when = datetime.now(UTC).isoformat(timespec="seconds")
    with log_path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(
            f"| {when} {_key(suite, split, dataset_sha)} {git} | {'yes' if forced else 'no'} |\n"
        )
