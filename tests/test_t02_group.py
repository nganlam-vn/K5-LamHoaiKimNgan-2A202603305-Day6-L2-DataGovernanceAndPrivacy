"""Group unit tests for Track T02 (Raw QC Triage).

Contract: >= 2 tests (success + failure/edge cases), 0 failed.
"""
from __future__ import annotations

import json
from pathlib import Path
import pandas as pd
import pytest

from src import main as runner


def test_t02_poc_pipeline_end_to_end(tmp_path):
    """Success test: T02 pipeline runs end-to-end offline, produces contract files with valid outputs."""
    out = tmp_path / "poc_out"
    rc = runner.main(["--track", "t02", "--config", "configs/lab.yaml", "--output", str(out), "--offline", "--seed", "42"])
    assert rc == 0

    # Check required files
    for fname in ["ranked_review.csv", "review_queue.csv", "contact_sheet.html", "decision.json", "decision_log.csv", "run_info.json"]:
        assert (out / fname).exists(), f"Missing required output file: {fname}"

    # Validate ranked_review.csv
    df = pd.read_csv(out / "ranked_review.csv")
    assert "image_id" in df.columns and "score" in df.columns
    assert "pred_defect_type" in df.columns
    assert len(df) == 96
    # Check descending score sort
    assert df["score"].is_monotonic_decreasing

    # Validate decision.json
    decision = json.loads((out / "decision.json").read_text(encoding="utf-8"))
    assert decision["status"] in {"KEEP", "RECAPTURE", "HOLD"}
    assert decision["owner"] != ""
    assert decision["reason_code"] != ""


def test_t02_human_review_override_flow(tmp_path):
    """Failure/edge test: Human-in-the-loop review override applies successfully and logs to audit."""
    out = tmp_path / "poc_review"
    # First pass: run pipeline to create review queue
    rc = runner.main(["--track", "t02", "--config", "configs/lab.yaml", "--output", str(out), "--offline", "--seed", "42"])
    assert rc == 0

    q_path = out / "review_queue.csv"
    assert q_path.exists()
    q = pd.read_csv(q_path)
    top_item_id = q.iloc[0]["item_id"]

    # Reviewer marks top item as 'reject' (false alarm / clean image)
    reviewed = q.copy().astype(object)
    reviewed.loc[0, "reviewer"] = "Kim Ngan - Reviewer"
    reviewed.loc[0, "reviewer_decision"] = "reject"
    reviewed.loc[0, "review_note"] = "False alarm: reflection is normal"
    reviewed.to_csv(out / "review_queue_reviewed.csv", index=False)

    # Second pass: re-run pipeline to consume review overrides
    rc2 = runner.main(["--track", "t02", "--config", "configs/lab.yaml", "--output", str(out), "--offline", "--seed", "42"])
    assert rc2 == 0

    # Verify score for top_item_id was cleared and defect_type set to 'none'
    df_after = pd.read_csv(out / "ranked_review.csv")
    item_row = df_after[df_after.image_id == top_item_id].iloc[0]
    assert item_row["score"] < 0.01
    assert item_row["pred_defect_type"] == "none"

    # Verify audit log contains entry
    audit_file = out / "audit_log.jsonl"
    assert audit_file.exists()
    lines = audit_file.read_text(encoding="utf-8").strip().split("\n")
    assert any("review:reject_defect" in line and top_item_id in line for line in lines)
