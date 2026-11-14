Baseline: `baseline` (git 4915775, seed 42) · PoC: `poc` (git 4915775, seed 42)

| Metric (unit) | Slice | Baseline | PoC | Δ abs | Δ rel | Better | N |
|---|---|---|---|---|---|---|---|
| **Defect Recall@B** (GT defects in top-B / GT defects) | all | 0.4286 | 0.7143 | 0.2857 | +66.7% | higher | 56 |
| Precision@B (GT defects in top-B / B) | all | 0.6000 | 1.0000 | 0.4000 | +66.7% | higher | 56 |
| Defect-type accuracy (GT defects) (fraction) | all | n/a | 0.9643 | n/a | n/a | higher | 28 |
| False alarm in top-B (clean images in top-B / clean images) | light=day | 0.3333 | 0.0000 | -0.3333 | -100.0% | lower | 36 |
| False alarm in top-B (clean images in top-B / clean images) | light=dusk | 0.1250 | 0.0000 | -0.1250 | -100.0% | lower | 16 |
| False alarm in top-B (clean images in top-B / clean images) | light=night | 0.5000 | 0.0000 | -0.5000 | -100.0% | lower | 4 |
| Runtime / 100 items (s) | all | 0.1104 | 0.5136 | | | lower | |
