"""Runs the ZweiGeeks benchmark and writes the results.

    python -m bench.run [--cases a1,b2] [--fixer-model M] [--auditor-model M]
                        [--verifier-model M] [--repeats N] [--passes N] [--out DIR]

For each case and repeat: a read-only audit of a fresh copy (detection), then
the full audit -> fix loop on another fresh copy, judged by the case's fix
checks and behaviour tests (fixing). Repeat i runs with OLLAMA_SEED = base + i.
Writes <out>/<timestamp>.json (every run's raw data) and <timestamp>.md (the
tables); earlier results are never overwritten.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from bench import core

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = Path(__file__).resolve().parent / "results"

Runner = Callable[[dict], dict]


def parse_args(argv=None) -> argparse.Namespace:
    from src import config

    parser = argparse.ArgumentParser(prog="python -m bench.run", description=__doc__.splitlines()[0])
    parser.add_argument("--cases", help="comma-separated case ids, e.g. a1,b2 (default: all)")
    parser.add_argument("--auditor-model")
    parser.add_argument("--fixer-model")
    parser.add_argument("--verifier-model")
    parser.add_argument("--repeats", type=int, default=3, help="runs per case, with seeds base..base+N-1 (default 3)")
    parser.add_argument("--passes", type=int, default=config.MAX_ITERATIONS,
                        help=f"fix passes per run (default {config.MAX_ITERATIONS}, as in the app)")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR, help="where results go (default bench/results)")
    args = parser.parse_args(argv)
    if args.repeats < 1 or args.passes < 1:
        parser.error("--repeats and --passes must be at least 1")
    return args


def job_env(job: dict) -> dict:
    """The environment the worker's src.config reads at import."""
    env = {**os.environ, "OLLAMA_SEED": str(job["seed"]),
           "REPORTS_DIR": str(Path(job["work"]) / "reports"),
           # Reviews are cached by content and model, not seed: a shared
           # cache would make every repeat replay the first one's review.
           "REVIEW_CACHE_DIR": str(Path(job["work"]) / "review_cache")}
    for role in ("auditor", "fixer", "verifier"):
        if job["models"].get(role):
            env[f"{role.upper()}_MODEL"] = job["models"][role]
    return env


def subprocess_runner(job: dict) -> dict:
    out = Path(job["work"]) / "result.json"
    cmd = [sys.executable, "-m", "bench.worker", "--case", str(job["case"].path), "--repeat", str(job["repeat"]),
           "--passes", str(job["passes"]), "--work", str(job["work"]), "--out", str(out)]
    proc = subprocess.run(cmd, cwd=ROOT, env=job_env(job), capture_output=True, text=True)
    if proc.returncode == 0 and out.is_file():
        return json.loads(out.read_text())
    return {"case": job["case"].id, "tier": job["case"].tier, "repeat": job["repeat"], "seed": job["seed"],
            "error": f"worker exited with {proc.returncode}: {(proc.stderr or proc.stdout)[-1500:]}"}


def ollama_reachable() -> bool:
    import ollama

    from src import config

    try:
        ollama.Client(host=config.OLLAMA_HOST, timeout=5).list()
    except Exception:  # noqa: BLE001
        return False
    return True


def _results_path(out_dir: Path, stamp: str) -> Path:
    """<stamp>.json, or <stamp>-2.json and so on: never an existing file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    name, n = stamp, 1
    while (out_dir / f"{name}.json").exists() or (out_dir / f"{name}.md").exists():
        n += 1
        name = f"{stamp}-{n}"
    return out_dir / f"{name}.json"


def _one_line(run: dict) -> str:
    if run.get("error"):
        return f"error: {run['error'].splitlines()[0][:120]}"
    c = core.run_counts(run)
    if run["tier"] == "C":
        found = f"findings static {c['findings']['static']}, llm {c['findings']['llm']} in {c['files']} files"
    else:
        found = (f"found static {c['detected']['static']}/{c['bugs']}, llm {c['detected']['llm']}/{c['bugs']}; "
                 f"fixed {c['fixed_any']}/{c['bugs']}")
    return f"{found}; regressions {c['regressions']}/{c['behaviour_tests']}"


def main(argv=None, *, cases_root: Path = core.CASES_DIR, runner: Optional[Runner] = None) -> Optional[Path]:
    from src import config

    args = parse_args(argv)
    cases = core.list_cases(cases_root, args.cases.split(",") if args.cases else None)
    if runner is None:
        if not ollama_reachable():
            print(f"Ollama isn't reachable at {config.OLLAMA_HOST}; start it with `ollama serve` and try again.",
                  file=sys.stderr)
            return None
        runner = subprocess_runner

    base_seed = config.OLLAMA_SEED
    models = {"auditor": args.auditor_model, "fixer": args.fixer_model, "verifier": args.verifier_model}
    started = time.strftime("%Y%m%d-%H%M%S")
    runs = []
    for case in cases:
        for repeat in range(args.repeats):
            work = Path(tempfile.mkdtemp(prefix=f"zweigeeks-bench-{case.id}-r{repeat}-"))
            job = {"case": case, "repeat": repeat, "seed": base_seed + repeat, "passes": args.passes,
                   "models": models, "work": work}
            print(f"[{case.id} r{repeat} seed {job['seed']}] running...", flush=True)
            t0 = time.monotonic()
            try:
                run = runner(job)
            finally:
                shutil.rmtree(work, ignore_errors=True)
            run["wall_seconds"] = round(time.monotonic() - t0, 1)
            runs.append(run)
            print(f"[{case.id} r{repeat}] {_one_line(run)} ({run['wall_seconds']} s)", flush=True)

    recorded = next((r["models"] for r in runs if r.get("models")), {})
    meta = {
        "started": started, "repeats": args.repeats, "passes": args.passes,
        "seeds": f"{base_seed}..{base_seed + args.repeats - 1}" if args.repeats > 1 else str(base_seed),
        "models": {role: recorded.get(role) or models[role] for role in models},
        "cases": [c.id for c in cases], "python": sys.version.split()[0],
    }
    agg = core.aggregate(runs)
    path = _results_path(Path(args.out), started)
    path.write_text(json.dumps({"meta": meta, "aggregate": agg, "runs": runs}, indent=2, default=str))
    table = core.markdown(meta, runs, agg)
    path.with_suffix(".md").write_text(table)
    print(table)
    print(f"Raw results: {path}\nTables: {path.with_suffix('.md')}")
    return path


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
