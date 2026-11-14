"""YOUR PoC PIPELINE — this is the file your group owns.

Lab contract = 5 automated steps, run by `python -m src.main ...`:
    01 load      input + provenance check (manifest.csv)
    02 process   your algorithm / model (the research part)
    03 rank      prioritise for a fixed review budget B
    04 review    human-in-the-loop: queue -> reviewer decisions -> overrides + audit log
    05 decide    GO / HOLD / REWORK (or track equivalent) with reason_code + owner, write outputs

Required output: out/<pred_file> with the columns listed for your track in
docs/tracks/TXX.md (also in src/metrics/tracks.py::TRACKS). Then evaluate with
    python -m src.evaluate --track tXX --pred out/<pred_file>

Rules: never read fixtures/*/gt/ here (the GT guard will stop you); seed everything via ctx.rng;
no network calls; fail loudly on missing models instead of downloading.
See src/examples/random_baseline.py for a minimal end-to-end wiring example.
"""
from __future__ import annotations

import pandas as pd
from .common.image_ops import load_rgb, laplacian_variance, brightness_stats

from .common.context import RunContext
from .common.decision import Decision, write_decision
from .common.manifest import verify_manifest
from .common.review import load_review_decisions, make_review_queue, write_review_queue
from .metrics.tracks import get_track


# ---------------------------------------------------------------- 01 load
def load(ctx: RunContext) -> dict:
    manifest = ctx.input_dir / "manifest.csv"
    problems = verify_manifest(manifest) if manifest.exists() else [{"problem": "no_manifest"}]
    if problems:
        ctx.audit.log(actor="pipeline", action="provenance_problems", item_id=str(manifest), after=problems[:20])
        if ctx.config.get("strict_provenance", True):
            raise ValueError(f"provenance check failed ({len(problems)} problems), first: {problems[:3]}")
    data = {"manifest": pd.read_csv(manifest) if manifest.exists() else None}
    # Load file images.csv T02
    images_csv = ctx.input_dir / "images.csv"
    if images_csv.exists():
        data["images"] = pd.read_csv(images_csv)
    else:
        raise FileNotFoundError(f"Missing images.csv in {ctx.input_dir}")
    return data


# ---------------------------------------------------------------- 02 process
def process(ctx: RunContext, data: dict) -> pd.DataFrame:
    spec = get_track(ctx.track)
    images_df = data["images"]
    rows = []
    for r in images_df.itertuples(index=False):
        # Đường dẫn tới từng file ảnh trong fixture
        img_path = ctx.input_dir / r.path
        img = load_rgb(img_path)
        
        # Thuật toán QC: Đo độ sắc nét (Laplacian variance)
        sharpness = laplacian_variance(img)
        
        # Điểm nghi lỗi: Ảnh càng mờ (sharpness càng thấp) thì score càng cao
        score = 1.0 / (sharpness + 1e-6)
        
        rows.append({
            "image_id": r.image_id,
            "score": score,
            "reason": "low_sharpness" if sharpness < 100 else "acceptable"
        })
        
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 03 rank
def rank(ctx: RunContext, scored: pd.DataFrame) -> pd.DataFrame:
    """Default: top-B by `score`."""
    budget = int(ctx.param("budget", 20))
    if "score" not in scored.columns:
        raise ValueError("rank(): process() output needs a numeric `score` column")
    
    q = make_review_queue(
        scored, 
        id_col=scored.columns[0], 
        score_col="score", 
        budget=budget,
        reason_col="reason" if "reason" in scored.columns else None
    )
    write_review_queue(q, ctx.out("review_queue.csv"))
    return q


# ---------------------------------------------------------------- 04 review
def review(ctx: RunContext, scored: pd.DataFrame, queue: pd.DataFrame) -> pd.DataFrame:
    """If a reviewed queue exists (out/review_queue_reviewed.csv), apply it. Otherwise pass through.

    TODO: decide what an `edit` / `reject` means for your track and apply it
    (e.g. via common.review.apply_overrides), logging every change to ctx.audit.
    """
    reviewed_path = ctx.output_dir / "review_queue_reviewed.csv"
    if reviewed_path.exists():
        reviewed = load_review_decisions(reviewed_path)
        ctx.stats["n_reviewed"] = len(reviewed)
        # TODO: apply overrides
    return scored


# ---------------------------------------------------------------- 05 decide
def decide(ctx: RunContext, final: pd.DataFrame) -> Decision:
    owner = ctx.config.get("owner", "Kim Ngan")
    
    # Policy mẫu cho T02: Kiểm tra nếu số lượng ảnh có score quá cao vượt ngưỡng cho phép
    # -> Trả về RECAPTURE, ngược lại KEEP.
    high_defect_count = (final["score"] > 0.05).sum() if "score" in final.columns else 0
    
    if high_defect_count > 3:
        return Decision(
            status="RECAPTURE",
            reason_code="HIGH_BLUR_RATE",
            reason=f"Phát hiện {high_defect_count} ảnh vượt ngưỡng mờ cho phép, yêu cầu chụp lại.",
            owner=owner,
            scope=ctx.track
        )
    else:
        return Decision(
            status="KEEP",
            reason_code="QUALITY_PASSED",
            reason="Chất lượng ảnh thô nằm trong giới hạn chấp nhận được.",
            owner=owner,
            scope=ctx.track
        )

def run(ctx: RunContext) -> None:
    spec = get_track(ctx.track)
    data = load(ctx)
    scored = process(ctx, data)
    ctx.stats["n_items"] = len(scored)
    queue = rank(ctx, scored)
    final = review(ctx, scored, queue)
    
    # Xuất file kết quả dự đoán ra out/ranked_review.csv đúng contract của T02
    final[spec.pred_columns + [c for c in final.columns if c not in spec.pred_columns]].to_csv(ctx.out(spec.pred_file), index=False)
    write_decision(decide(ctx, final), ctx.out("decision.json"), ctx.out("decision_log.csv"))
