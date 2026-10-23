#!/usr/bin/env python3
"""
promote.py - decide whether a retrained model replaces the current one, and
roll back to an older model if needed.

The rule (config.py): the candidate in model/candidate/ replaces the current
model ONLY if its recall and ROC-AUC are not worse than the current model's on
the SAME test customers (train.py --candidate measures both). If the data and
every score are exactly the same, nothing would change, so the current model
is kept. Before a replacement the current model is copied to
model/archive/<model_version>/, so nothing is ever lost.

How to run (from the project folder):
    python model/train.py --candidate          # 1. train a candidate
    python model/promote.py                     # 2. compare, and replace only if not worse
    python model/promote.py --list              # show the archived versions
    python model/promote.py --rollback 20261023-1015-random-forest
                                                # put an archived version back
After a promotion or roll-back: commit the model/ folder and push, then run
score.py so the warehouse uses the same model as the web app.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import config

FILES = (config.MODEL_FILE, config.METRICS_FILE)


def read_metrics(folder: Path) -> dict:
    return json.loads((folder / config.METRICS_FILE).read_text(encoding="utf-8"))


def archive_current() -> str:
    """Copy the current model into model/archive/<version>/ (kept for roll-back)."""
    version = read_metrics(config.MODEL_DIR)["model_version"]
    target = config.ARCHIVE_DIR / version
    target.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        shutil.copy2(config.MODEL_DIR / name, target / name)
    return version


def install_from(folder: Path) -> None:
    for name in FILES:
        shutil.copy2(folder / name, config.MODEL_DIR / name)


def github_output(**values) -> None:
    """Tell the next workflow steps what happened (only inside GitHub Actions)."""
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            for key, value in values.items():
                fh.write(f"{key}={value}\n")


def promote() -> None:
    if not (config.CANDIDATE_DIR / config.MODEL_FILE).exists():
        sys.exit("No candidate. Run  python model/train.py --candidate  first.")
    candidate = read_metrics(config.CANDIDATE_DIR)
    comparison = candidate.get("comparison_with_current", {})
    has_current = (config.MODEL_DIR / config.MODEL_FILE).exists()

    if not has_current:
        decision, why = True, "there is no current model yet"
    elif "current" not in comparison:
        decision, why = False, ("the current model could not be measured on the new test set "
                                f"({comparison.get('current_error', 'no comparison saved')})")
    else:
        old, new, tol = comparison["current"], comparison["candidate"], config.PROMOTION_TOLERANCE
        lines = [f"{m}: current {old[m]:.4f} -> candidate {new[m]:.4f}" for m in config.PROMOTION_METRICS]
        worse = [m for m in config.PROMOTION_METRICS if new[m] < old[m] - tol]
        same_rows = candidate["data"]["rows"] == read_metrics(config.MODEL_DIR)["data"]["rows"]
        unchanged = same_rows and all(new[k] == old[k] for k in new)
        decision = not worse and not unchanged
        why = "; ".join(lines) + (f" -> worse on {', '.join(worse)}" if worse
                                  else " -> identical results on the same data, nothing to gain" if unchanged
                                  else " -> not worse")

    if decision:
        old_version = archive_current() if has_current else None
        install_from(config.CANDIDATE_DIR)
        print(f"PROMOTED {candidate['model_version']} ({candidate['model_name']}): {why}.")
        if old_version:
            print(f"Previous model kept in model/archive/{old_version}/ (roll back with --rollback {old_version}).")
    else:
        print(f"KEPT the current model; candidate {candidate['model_version']} rejected: {why}.")
    shutil.rmtree(config.CANDIDATE_DIR, ignore_errors=True)
    github_output(promoted=str(decision).lower(), version=candidate["model_version"])


def rollback(version: str) -> None:
    source = config.ARCHIVE_DIR / version
    if not (source / config.MODEL_FILE).exists():
        sys.exit(f"model/archive/{version}/ not found. Use --list to see the archived versions.")
    current = archive_current()
    install_from(source)
    print(f"Rolled back to {version}. The model that was active ({current}) is kept in model/archive/{current}/.")


def list_versions() -> None:
    current = read_metrics(config.MODEL_DIR)["model_version"] if (config.MODEL_DIR / config.METRICS_FILE).exists() else None
    print(f"current: {current}")
    for folder in sorted(config.ARCHIVE_DIR.glob("*/")):
        m = read_metrics(folder)
        final = next(x for x in m["models"] if x["is_final"])["test"]
        print(f"archived: {m['model_version']:<40} recall {final['recall']:.4f}  ROC-AUC {final['roc_auc']:.4f}  "
              f"trained {m['trained_at']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote a retrained model, or roll back.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--rollback", metavar="VERSION", help="restore an archived model version")
    group.add_argument("--list", action="store_true", help="list the current and archived versions")
    args = parser.parse_args()
    if args.list:
        list_versions()
    elif args.rollback:
        rollback(args.rollback)
    else:
        promote()


if __name__ == "__main__":
    main()
