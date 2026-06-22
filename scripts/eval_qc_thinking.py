#!/usr/bin/env python3
"""Throwaway eval: does disabling Gemini "thinking" change QC output quality?

Runs the REAL qc_translations_batch() over a set of sample rows twice — once
with thinking on (default) and once with thinking_budget=0 — pinned to a single
model, then diffs the corrected JSON per language and reports wall-clock time.

Use this to decide whether thinking_budget=0 is safe for the QC task BEFORE
shipping it. If the outputs match (or are equivalent) across your real rows,
turning thinking off is a free cost saving. If they diverge, keep thinking on.

Usage:
    GEMINI_API_KEY=... python scripts/eval_qc_thinking.py [samples.json] [model]

samples.json (optional) is a list of rows:
    [
      {
        "original_text": "A for Apple. An apple is red.",
        "translations": {"hi-IN": "...", "ta-IN": "..."},
        "target_languages": ["hi-IN", "ta-IN"],
        "teaching_mode": true
      }
    ]
If omitted, a small built-in sample set runs so the script works out of the box.
Replace it with REAL production rows for a meaningful verdict.
"""
from __future__ import annotations

import json
import os
import sys
import time

# Make the repo root importable when run as `python scripts/eval_qc_thinking.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.qc import qc_translations_batch  # noqa: E402

DEFAULT_SAMPLES = [
    {
        "original_text": "A for Apple. An apple is red.",
        "translations": {
            "hi-IN": "A से Apple. Apple red होता है।",
            "ta-IN": "A for Apple. ஒரு ஆப்பிள் சிவப்பு.",
        },
        "target_languages": ["hi-IN", "ta-IN"],
        "teaching_mode": True,
    },
    {
        "original_text": "Please wash your hands before eating.",
        "translations": {
            "hi-IN": "कृपया खाना खाने से पहले अपने hands wash करें।",
            "mr-IN": "जेवण्यापूर्वी कृपया आपले हात धुवा.",
        },
        "target_languages": ["hi-IN", "mr-IN"],
        "teaching_mode": False,
    },
]


def load_samples() -> list[dict]:
    if len(sys.argv) > 1 and sys.argv[1].endswith(".json"):
        with open(sys.argv[1], encoding="utf-8") as f:
            return json.load(f)
    return DEFAULT_SAMPLES


def run(rows: list[dict], *, thinking_budget: int | None) -> tuple[list[dict], float]:
    started = time.perf_counter()
    out = []
    for row in rows:
        out.append(
            qc_translations_batch(
                row["original_text"],
                row["translations"],
                row["target_languages"],
                teaching_mode=row.get("teaching_mode", False),
                thinking_budget=thinking_budget,
            )
        )
    return out, time.perf_counter() - started


def main() -> int:
    # Pin to one model so the on/off comparison is clean.
    model = sys.argv[2] if len(sys.argv) > 2 else "gemini-2.5-flash"
    os.environ["GEMINI_QC_MODELS"] = model

    rows = load_samples()
    print(f"Model: {model}   Rows: {len(rows)}\n")

    print("Running with thinking ON (default)...")
    on, on_secs = run(rows, thinking_budget=None)
    print(f"  done in {on_secs:.1f}s\n")

    print("Running with thinking OFF (budget=0)...")
    off, off_secs = run(rows, thinking_budget=0)
    print(f"  done in {off_secs:.1f}s\n")

    print("=" * 70)
    total_langs = 0
    diff_langs = 0
    for i, (row, a, b) in enumerate(zip(rows, on, off)):
        print(f"\nRow {i}: {row['original_text'][:60]!r}")
        for lang in row["target_languages"]:
            total_langs += 1
            va, vb = a.get(lang, ""), b.get(lang, "")
            if va == vb:
                print(f"  [=] {lang}: identical")
            else:
                diff_langs += 1
                print(f"  [≠] {lang}:")
                print(f"        ON : {va!r}")
                print(f"        OFF: {vb!r}")

    print("\n" + "=" * 70)
    print(f"Languages compared: {total_langs}")
    print(f"Differences:        {diff_langs}")
    print(f"Time  ON: {on_secs:.1f}s   OFF: {off_secs:.1f}s")
    if diff_langs == 0:
        print("\nVERDICT: outputs identical — thinking_budget=0 looks SAFE to ship.")
    else:
        print(
            "\nVERDICT: outputs differ — REVIEW the diffs above. Keep thinking ON "
            "unless the OFF results are equal-or-better quality."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
