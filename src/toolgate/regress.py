"""Cross-project eval regression gate.

Every suite's report is reduced to one schema ({suite, source, git_sha, dataset_sha256, metrics})
and compared with the baseline + tolerance in config/regression.yaml. Any metric worse than
baseline by more than its tolerance, or missing, fails the gate with a readable diff table.

    # PR / CI mode: recorded fixtures, no API cost
    uv run python -m toolgate.regress --fixtures evals/fixtures
    # demo: swap in a real run made with a weaker prompt -> the gate fails
    uv run python -m toolgate.regress --fixtures evals/fixtures \
        --use p2-policypilot-answers=evals/fixtures/degraded/p2-policypilot-answers.json
    # live mode: re-run deterministic suites, read the newest reports of the others
    uv run python -m toolgate.regress --live
    # refresh fixtures from the newest reports
    uv run python -m toolgate.regress --record evals/fixtures
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[2]


class MetricSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline: float
    tolerance: float = Field(ge=0)
    direction: Literal["higher", "lower"]


class SuiteSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: str
    report_glob: str
    live_command: list[str] | None = None  # deterministic suites only (no LLM, no quota)
    uses_llm: bool = False
    metrics: dict[str, MetricSpec]


class RegressionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suites: dict[str, SuiteSpec]


@dataclass
class Row:
    suite: str
    metric: str
    baseline: float
    value: float | None
    tolerance: float
    direction: str

    @property
    def delta(self) -> float | None:
        return None if self.value is None else self.value - self.baseline

    @property
    def status(self) -> str:
        if self.value is None:
            return "MISSING"
        worse = (
            (self.baseline - self.value)
            if self.direction == "higher"
            else (self.value - self.baseline)
        )
        if worse > self.tolerance + 1e-12:
            return "REGRESSED"
        return "improved" if worse < -1e-4 else "ok"


def load_config(path: Path) -> RegressionConfig:
    return RegressionConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _get(d: Any, dotted: str) -> float | None:
    for part in dotted.split("."):
        if not isinstance(d, dict) or part not in d:
            return None
        d = d[part]
    try:
        return float(d)
    except (TypeError, ValueError):
        return None


def normalise(suite: str, spec: SuiteSpec, report: dict[str, Any], source: str) -> dict[str, Any]:
    return {
        "suite": suite,
        "source": source,
        "git_sha": report.get("git_sha"),
        "dataset_sha256": report.get("dataset_sha256"),
        "model": report.get("model"),
        "metrics": {m: _get(report, m) for m in spec.metrics},
    }


def newest_report(spec: SuiteSpec, base: Path) -> tuple[dict[str, Any], str] | None:
    files = sorted(glob.glob(str(base / spec.report_glob)), key=os.path.getmtime)
    files = [f for f in files if not f.endswith(".rescored.json")]
    if not files:
        return None
    return json.loads(Path(files[-1]).read_text(encoding="utf-8")), Path(files[-1]).name


def compare(cfg: RegressionConfig, results: dict[str, dict[str, Any]]) -> list[Row]:
    rows = []
    for suite, spec in cfg.suites.items():
        got = (results.get(suite) or {}).get("metrics", {})
        for m, ms in spec.metrics.items():
            rows.append(Row(suite, m, ms.baseline, got.get(m), ms.tolerance, ms.direction))
    return rows


def table(rows: list[Row], sources: dict[str, str]) -> str:
    out = [
        "| Suite | Metric | Baseline | Now | Delta | Tolerance | Status |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        now = "-" if r.value is None else f"{r.value:.4g}"
        delta = "-" if r.delta is None else f"{r.delta:+.4g}"
        mark = f"**{r.status}**" if r.status in ("REGRESSED", "MISSING") else r.status
        arrow = "higher is better" if r.direction == "higher" else "lower is better"
        out.append(
            f"| {r.suite} | {r.metric} | {r.baseline:.4g} | {now} | {delta} | +/-{r.tolerance:g} ({arrow}) | {mark} |"
        )
    out.append("")
    out += [f"- {s}: {src}" for s, src in sources.items()]
    return "\n".join(out)


def run(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="toolgate.regress")
    p.add_argument("--config", default=str(ROOT / "config" / "regression.yaml"))
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixtures", help="CI mode: read <dir>/<suite>.json")
    mode.add_argument(
        "--live", action="store_true", help="re-run deterministic suites; newest reports otherwise"
    )
    mode.add_argument(
        "--record", help="write normalised fixtures from the newest reports into <dir>"
    )
    p.add_argument(
        "--use", action="append", default=[], help="suite=path: use this fixture for one suite"
    )
    p.add_argument("--projects-root", default=str(ROOT.parent))
    p.add_argument("--out", default=str(ROOT / "evals" / "reports"))
    a = p.parse_args(argv)
    cfg = load_config(Path(a.config))
    base = Path(a.projects_root)
    results: dict[str, dict[str, Any]] = {}
    sources: dict[str, str] = {}

    if a.record:
        Path(a.record).mkdir(parents=True, exist_ok=True)
        for suite, spec in cfg.suites.items():
            found = newest_report(spec, base)
            if found is None:
                print(f"skip {suite}: no report matches {spec.report_glob}")
                continue
            fx = normalise(suite, spec, found[0], f"{spec.project}/{found[1]}")
            (Path(a.record) / f"{suite}.json").write_text(
                json.dumps(fx, indent=2) + "\n", encoding="utf-8"
            )
            print(f"recorded {suite} from {found[1]}")
        return 0

    for suite, spec in cfg.suites.items():
        if a.fixtures:
            f = Path(a.fixtures) / f"{suite}.json"
            if f.exists():
                results[suite] = json.loads(f.read_text(encoding="utf-8"))
                sources[suite] = f"fixture {f.name} (from {results[suite].get('source')})"
            continue
        if spec.live_command and not spec.uses_llm:
            print(f"running {suite}: {' '.join(spec.live_command)}", flush=True)
            subprocess.run(
                spec.live_command,
                cwd=base,
                check=False,
                capture_output=True,
                text=True,
                timeout=900,
            )
        found = newest_report(spec, base)
        if found is not None:
            results[suite] = normalise(suite, spec, found[0], f"{spec.project}/{found[1]}")
            sources[suite] = results[suite]["source"]
    for item in a.use:
        suite, _, path = item.partition("=")
        results[suite] = json.loads(Path(path).read_text(encoding="utf-8"))
        sources[suite] = f"override {path} (from {results[suite].get('source')})"

    rows = compare(cfg, results)
    md = table(rows, sources)
    failed = [r for r in rows if r.status in ("REGRESSED", "MISSING")]
    verdict = f"\n**Gate: {'FAIL' if failed else 'PASS'}** ({len(failed)} of {len(rows)} metrics regressed or missing)\n"
    print(md + verdict)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    (out / f"regression-{stamp}.md").write_text(
        "# Eval regression gate\n\n" + md + verdict, encoding="utf-8"
    )
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("## Eval regression gate\n\n" + md + verdict)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
