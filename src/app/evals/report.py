"""Write eval results to evals/reports/<suite>-<split>-<timestamp>-<gitsha>.{json,md}."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.evals.runner import Report


def git_sha() -> str:
    git = shutil.which("git")
    if git is None:
        return "nogit"
    try:
        out = subprocess.run(  # noqa: S603  (fixed argv, no shell)
            [git, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return "nogit"
    sha = out.stdout.strip()
    if out.returncode != 0 or not sha:
        return "nogit"
    dirty = subprocess.run(  # noqa: S603
        [git, "status", "--porcelain"], capture_output=True, text=True, timeout=5
    ).stdout.strip()
    return f"{sha}-dirty" if dirty else sha


def _default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(f"not serializable: {type(o).__name__}")


def write_report(report: Report, metric_names: tuple[str, ...], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    sha = git_sha()
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    stem = f"{report.suite}-{report.split}-{stamp}-{sha}"
    n = 2
    while (out_dir / f"{stem}.json").exists():  # never overwrite an earlier run's evidence
        stem = f"{report.suite}-{report.split}-{stamp}-{sha}-{n}"
        n += 1
    summary = report.summary(metric_names)

    payload = {
        "suite": report.suite,
        "split": report.split,
        "dataset_sha256": report.dataset_sha256,
        "prompt_version": report.prompt_version,
        "git_sha": sha,
        "started_at": report.started_at,
        "config": report.config,
        "n_planned": report.n_planned,
        "n_completed": report.n_completed,
        "n_errors": report.n_errors,
        "stopped_reason": report.stopped_reason,
        "total_cost_usd": report.total_cost_usd,
        "summary": summary,
        "outcomes": [asdict(o) for o in report.outcomes],
    }
    json_path = out_dir / f"{stem}.json"
    json_path.write_text(json.dumps(payload, indent=2, default=_default), encoding="utf-8")

    lines = [
        f"# Eval report: {report.suite} / {report.split}",
        "",
        f"- Dataset SHA-256: `{report.dataset_sha256}`",
        f"- Prompt version: `{report.prompt_version}` · Git: `{sha}`",
        f"- Started: {report.started_at}",
        f"- Items: {report.n_completed}/{report.n_planned} completed, {report.n_errors} errors",
        f"- Cost: ${report.total_cost_usd} · Stopped early: {report.stopped_reason or 'no'}",
        f"- Raw results: `{json_path.name}`",
        "",
        "| Metric | Mean |",
        "|---|---|",
        *(f"| {m} | {v:.3f} |" for m, v in summary.items()),
        "",
    ]
    failures = [o for o in report.outcomes if o.error or min(o.scores.values(), default=1) < 1]
    if failures:
        lines += ["## Failures", "", "| id | expected | output | error |", "|---|---|---|---|"]
        lines += [
            f"| {o.id} | {str(o.expected)[:60]} | {str(o.output)[:60]} | {o.error or ''} |"
            for o in failures[:50]
        ]
    md_path = out_dir / f"{stem}.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path
