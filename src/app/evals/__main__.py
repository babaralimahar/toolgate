"""Eval CLI.

    uv run python -m app.evals --suite toy --split dev
    uv run python -m app.evals --suite toy --split dev --sample 5 --seed 1 --max-cost 0.05
    uv run python -m app.evals --suite toy --split test          # held-out: once per project
    uv run python -m app.evals --suite toy --split test --force  # logged re-run

With LLM_PROVIDER=fake the toy suite uses a deterministic fake model (zero spend).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.adapters.llm import FakeLLMClient, LLMClient, load_pricing
from app.adapters.llm_factory import build_llm_client
from app.config import get_settings
from app.evals.dataset import DatasetError, load_dataset
from app.evals.heldout import HeldOutAlreadyRun, check_heldout, record_heldout
from app.evals.report import git_sha, write_report
from app.evals.runner import run_suite
from app.evals.suites import SUITES, fake_uppercase_responder

HELDOUT_SPLITS = frozenset({"test"})


def _decimal(value: str) -> Decimal:
    try:
        d = Decimal(value)
    except InvalidOperation as e:
        raise argparse.ArgumentTypeError(f"not a number: {value}") from e
    if d < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return d


def _build_llm(suite: str) -> LLMClient:
    settings = get_settings()
    if settings.llm_provider == "fake" and suite == "toy":
        return FakeLLMClient(responder=fake_uppercase_responder(Decimal("0.0001")))
    return build_llm_client(settings, load_pricing(settings.pricing_path))


async def _run(args: argparse.Namespace) -> int:
    root = Path(args.evals_dir)
    held_out = args.split in HELDOUT_SPLITS
    try:
        dataset = load_dataset(
            root / "datasets" / args.suite / f"{args.split}.jsonl", require_frozen=held_out
        )
    except DatasetError as e:
        print(f"dataset error: {e}", file=sys.stderr)
        return 2

    log_path = root / "HELDOUT_LOG.md"
    if held_out:
        try:
            check_heldout(log_path, args.suite, args.split, dataset.sha256, args.force)
        except HeldOutAlreadyRun as e:
            print(f"refused: {e}", file=sys.stderr)
            return 3
        if args.sample is not None:
            print("refused: held-out sets are run in full (no --sample)", file=sys.stderr)
            return 2

    suite = SUITES[args.suite](_build_llm(args.suite))
    report = await run_suite(
        suite,
        dataset,
        concurrency=args.concurrency,
        max_cost=args.max_cost,
        sample=args.sample,
        seed=args.seed,
    )
    json_path, md_path = write_report(report, suite.metrics, root / "reports")
    if held_out:
        record_heldout(log_path, args.suite, args.split, dataset.sha256, git_sha(), args.force)

    print(md_path.read_text(encoding="utf-8"))
    print(f"wrote {json_path}\nwrote {md_path}")
    return 0 if report.stopped_reason is None else 4


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="app.evals")
    p.add_argument("--suite", required=True, choices=sorted(SUITES))
    p.add_argument("--split", default="dev")
    p.add_argument("--sample", type=int, default=None)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--max-cost", type=_decimal, default=Decimal("0.50"), help="USD cap per run")
    p.add_argument("--force", action="store_true", help="allow re-running a held-out set")
    p.add_argument("--evals-dir", default="evals")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
