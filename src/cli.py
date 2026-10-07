"""Command-line entry point, mainly for measuring runs:

    python -m src.cli /path/to/repo               # audit + fix, 5 fix passes
    python -m src.cli /path/to/repo --passes 1
    python -m src.cli /path/to/repo --audit-only

Prints the run summary table at the end; the full log is in
audit_results/<run_id>/run.jsonl.
"""

from __future__ import annotations

import argparse

from . import config
from .graph import run_audit_only, run_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.cli", description=__doc__.splitlines()[0])
    parser.add_argument("repo", help="path to a local git repository")
    parser.add_argument("--passes", type=int, default=config.MAX_ITERATIONS, help="fix passes")
    parser.add_argument("--audit-only", action="store_true", help="report only, change nothing")
    args = parser.parse_args(argv)
    if args.audit_only:
        run_audit_only(args.repo)
    else:
        run_pipeline(args.repo, max_iterations=args.passes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
