"""PoC PIPELINE — Track T02 (Raw QC Triage).

Lab contract = 5 automated steps, run by `python -m src.main ...`:
    01 load      input + provenance check (manifest.csv, images.csv)
    02 process   multi-defect detection algorithm (blur, dark, glare, compression)
    03 rank      prioritise for a fixed review budget B, render offline contact sheet
    04 review    human-in-the-loop: review queue -> overrides + audit log
    05 decide    KEEP / RECAPTURE with reason_code + owner, write outputs

Outputs:
    out/ranked_review.csv: image_id, score, pred_defect_type, reason
    out/review_queue.csv: top-B suspicious images for review
    out/contact_sheet.html: visual HTML triage gallery
    out/decision.json & out/decision_log.csv: governance audit decision
"""
from __future__ import annotations

from pathlib import Path
import numpy as np
import pandas as pd

from .common.contact_sheet import render_contact_sheet
from .common.context import RunContext
from .common.decision import Decision, write_decision
from .common.image_ops import brightness_stats, laplacian_variance, load_rgb, to_gray
from .common.manifest import verify_manifest
from .common.review import (
    apply_overrides,
    load_review_decisions,
    make_review_queue,
    write_review_queue,
)
from .metrics.tracks import get_track


# ---------------------------------------------------------------- 01 load
def load(ctx: RunContext) -> dict:
    """Step 1: Verify data provenance and load required inputs."""
    if ctx.track != "t02":
        raise NotImplementedError(
            f"Track {ctx.track} is not implemented. This pipeline is configured for Track T02 (Raw QC Triage)."
        )

    manifest = ctx.input_dir / "manifest.csv"
    problems = verify_manifest(manifest) if manifest.exists() else [{"problem": "no_manifest"}]
    if problems:
        ctx.audit.log(actor="pipeline", action="provenance_problems", item_id=str(manifest), after=problems[:20])
        if ctx.config.get("strict_provenance", True):
            raise ValueError(f"provenance check failed ({len(problems)} problems), first: {problems[:3]}")

    images_csv = ctx.input_dir / "images.csv"
    if not images_csv.exists():
        raise FileNotFoundError(f"Missing images.csv in {ctx.input_dir}")

    return {
        "manifest": pd.read_csv(manifest) if manifest.exists() else None,
        "images": pd.read_csv(images_csv),
    }


# ---------------------------------------------------------------- 02 process
def _detect_defects(img_path: Path, camera_id: str, light: str) -> dict:
    """Analyze image across multiple defect types: blur, dark, glare, and compression."""
    img = load_rgb(img_path)
    sharpness = laplacian_variance(img)
    bri = brightness_stats(img)
    gray = to_gray(img)

    # 1. JPEG Compression Blockiness (8x8 DCT grid boundary discontinuities)
    # Measures difference across 8x8 block boundaries vs inside blocks
    d_b_x = np.abs(gray[:, 7:-1:8] - gray[:, 8::8]).mean()
    d_in_x = np.abs(gray[:, 3:-1:8] - gray[:, 4:-1:8]).mean()
    d_b_y = np.abs(gray[7:-1:8, :] - gray[8::8, :]).mean()
    d_in_y = np.abs(gray[3:-1:8, :] - gray[4:-1:8, :]).mean()
    block_ratio = float((d_b_x + d_b_y) / (d_in_x + d_in_y + 1e-6))

    # Defect score calculations normalized in [0.0, 1.0]:

    # A. Blur score:
    # Severe blur causes sharpness to plummet below 30 (clean images are > 150 even on cam_B night).
    # Normalizing avoids false-alarming clean cam_B or clean night images.
    s_blur = float(max(0.0, min(1.0, 1.0 - (sharpness / 30.0)))) if sharpness < 30.0 else 0.0

    # B. Dark / Under-exposure score:
    # True dark corruptions have mean < 28 and p95 < 70, whereas clean night images have headlights/streetlights (p95 > 100).
    s_dark = float(max(0.0, min(1.0, (70.0 - bri["p95"]) / 45.0))) if (bri["p95"] < 70.0 and bri["mean"] < 28.0) else 0.0

    # C. Glare / Over-exposure score:
    # Catches local saturation hotspots (e.g. headlight reflection, washed out plates).
    s_glare = float(max(0.0, min(1.0, bri["frac_saturated"] / 0.01))) if bri["frac_saturated"] > 0.003 else 0.0

    # D. Compression score:
    # High 8x8 block boundary ratio (> 1.8 indicates severe JPEG blockiness).
    s_comp = float(max(0.0, min(1.0, (block_ratio - 1.7) / 1.5))) if block_ratio > 1.8 else 0.0

    defect_scores = {
        "blur": s_blur,
        "dark": s_dark,
        "glare": s_glare,
        "compression": s_comp,
    }

    best_type = max(defect_scores, key=defect_scores.get)
    max_defect_val = defect_scores[best_type]

    if max_defect_val > 0.05:
        # Scale to [0.20, 1.00] for defective images
        final_score = float(0.20 + 0.80 * max_defect_val)
        reason = f"{best_type}: score={final_score:.3f}"
        suggested_action = "recapture" if best_type in ("blur", "dark", "glare") else "recompress"
    else:
        best_type = "none"
        # Clean images get very low defect score (< 0.01)
        final_score = float(0.005 / (sharpness + 1.0))
        reason = "acceptable"
        suggested_action = "keep"

    return {
        "score": final_score,
        "pred_defect_type": best_type,
        "reason": reason,
        "suggested_action": suggested_action,
        "sharpness": sharpness,
        "p95": bri["p95"],
        "frac_sat": bri["frac_saturated"],
        "block_ratio": block_ratio,
    }


def process(ctx: RunContext, data: dict) -> pd.DataFrame:
    """Step 2: Run PoC defect triage algorithm on all input images."""
    if ctx.track != "t02":
        raise NotImplementedError(
            f"Implement process() for {ctx.track}. This pipeline is configured for Track T02 (Raw QC Triage)."
        )

    images_df = data["images"]
    rows = []

    for r in images_df.itertuples(index=False):
        img_rel_path = r.path
        img_full_path = ctx.input_dir / img_rel_path
        camera_id = getattr(r, "camera_id", "unknown")
        light = getattr(r, "light", "unknown")

        res = _detect_defects(img_full_path, camera_id, light)

        rows.append({
            "image_id": r.image_id,
            "score": res["score"],
            "pred_defect_type": res["pred_defect_type"],
            "reason": res["reason"],
            "suggested_action": res["suggested_action"],
            "slice": f"{camera_id}/{light}",
            "evidence_path": img_rel_path,
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 03 rank
def rank(ctx: RunContext, scored: pd.DataFrame) -> pd.DataFrame:
    """Step 3: Prioritise top-B suspicious images and render offline contact sheet."""
    budget = int(ctx.param("budget", 20))
    if "score" not in scored.columns:
        raise ValueError("rank(): process() output needs a numeric `score` column")

    evidence = "evidence_path" if "evidence_path" in scored.columns else None
    action_col = "suggested_action" if "suggested_action" in scored.columns else None
    reason_col = "reason" if "reason" in scored.columns else None
    slice_col = "slice" if "slice" in scored.columns else None

    q = make_review_queue(
        scored,
        id_col="image_id",
        score_col="score",
        budget=budget,
        reason_col=reason_col,
        action_col=action_col,
        slice_col=slice_col,
        evidence_col=evidence,
        ascending=False,
    )
    write_review_queue(q, ctx.out("review_queue.csv"))

    # Render offline HTML gallery for human triage
    if evidence:
        render_contact_sheet(
            q,
            ctx.out("contact_sheet.html"),
            image_root=ctx.input_dir,
            title=f"T02 Raw QC Triage — Review Queue (Top {budget})",
        )

    return q


# ---------------------------------------------------------------- 04 review
def review(ctx: RunContext, scored: pd.DataFrame, queue: pd.DataFrame) -> pd.DataFrame:
    """Step 4: Human-in-the-loop review overrides and audit logging.

    If out/review_queue_reviewed.csv exists (exported from contact sheet or edited in CSV),
    applies reviewer decisions and logs to audit_log.jsonl.
    """
    reviewed_path = ctx.output_dir / "review_queue_reviewed.csv"
    if reviewed_path.exists():
        reviewed = load_review_decisions(reviewed_path)
        ctx.stats["n_reviewed"] = len(reviewed)

        # Apply score overrides if explicitly provided
        scored = apply_overrides(scored, reviewed, id_col="image_id", value_col="score", audit=ctx.audit)

        # Apply decision semantics
        for r in reviewed.itertuples(index=False):
            if r.reviewer_decision == "reject":
                # Reviewer marked item as clean (false alarm): clear defect score
                mask = scored["image_id"] == r.item_id
                scored.loc[mask, "score"] = 0.0001
                if "pred_defect_type" in scored.columns:
                    scored.loc[mask, "pred_defect_type"] = "none"
                if "suggested_action" in scored.columns:
                    scored.loc[mask, "suggested_action"] = "keep"
                ctx.audit.log(
                    actor=r.reviewer,
                    action="review:reject_defect",
                    item_id=r.item_id,
                    reason=r.review_note or "Marked clean by human reviewer",
                )
            elif r.reviewer_decision == "approve":
                ctx.audit.log(
                    actor=r.reviewer,
                    action="review:approve_defect",
                    item_id=r.item_id,
                    reason=r.review_note or "Confirmed defect by human reviewer",
                )
    return scored


# ---------------------------------------------------------------- 05 decide
def decide(ctx: RunContext, final: pd.DataFrame) -> Decision:
    """Step 5: Governance decision policy (KEEP vs RECAPTURE) with mandatory owner and audit reason."""
    owner = ctx.config.get("owner", "Lâm Hoài Kim Ngân - owner")
    budget = int(ctx.param("budget", 20))

    # Evaluate defect volume
    defect_mask = final["score"] >= 0.20 if "score" in final.columns else pd.Series(False, index=final.index)
    total_defects = int(defect_mask.sum())
    defect_rate = total_defects / max(1, len(final))

    # Defects caught in top-B review queue
    ranked_top = final.sort_values("score", ascending=False).head(budget)
    top_defects = int((ranked_top["score"] >= 0.20).sum())

    if top_defects >= 10:
        return Decision(
            status="RECAPTURE",
            reason_code="HIGH_DEFECT_RATE",
            reason=(
                f"Phát hiện {top_defects}/{budget} ảnh nghi lỗi nghiêm trọng trong review queue "
                f"({total_defects} ảnh lỗi toàn batch, tỷ lệ={defect_rate:.1%}). Yêu cầu chụp lại theo quy trình QC."
            ),
            owner=owner,
            scope=ctx.track,
            evidence=[str(ctx.out("ranked_review.csv")), str(ctx.out("review_queue.csv"))],
        )
    else:
        return Decision(
            status="KEEP",
            reason_code="QUALITY_PASSED",
            reason=(
                f"Tỷ lệ ảnh lỗi ({total_defects}/{len(final)}, {defect_rate:.1%}) nằm trong giới hạn cho phép. "
                f"Chất lượng batch đạt yêu cầu lưu trữ và huấn luyện."
            ),
            owner=owner,
            scope=ctx.track,
            evidence=[str(ctx.out("ranked_review.csv"))],
        )


def run(ctx: RunContext) -> None:
    """Pipeline coordinator: executes all 5 steps in order."""
    spec = get_track(ctx.track)
    data = load(ctx)
    scored = process(ctx, data)
    ctx.stats["n_items"] = len(scored)
    queue = rank(ctx, scored)
    final = review(ctx, scored, queue)

    # Sort descending by score for final output
    ranked_final = final.sort_values("score", ascending=False, kind="mergesort")

    # Output contract: out/ranked_review.csv (image_id, score, pred_defect_type, reason)
    output_cols = spec.pred_columns + [
        c for c in ["pred_defect_type", "reason", "suggested_action"]
        if c in ranked_final.columns and c not in spec.pred_columns
    ]
    ranked_final[output_cols].to_csv(ctx.out(spec.pred_file), index=False)

    decision = decide(ctx, ranked_final)
    write_decision(decision, ctx.out("decision.json"), ctx.out("decision_log.csv"))
