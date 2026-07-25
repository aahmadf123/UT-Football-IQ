# Spike report — SportQA & SportR as football-reasoning benchmarks (Issue #168)

**Status: EVALUATION-ONLY spike.** No fine-tuning, no coach-facing assistant, no
production wiring. Recommendation below. Governed by
[#166](https://github.com/aahmadf123/Football-IQ/issues/166); must not distract
from #127/#128/#129 or CFBD work (per the issue).

Harness: [`gpu-worker/eval/sportqa_sportr_eval.py`](../gpu-worker/eval/sportqa_sportr_eval.py)
(offline coverage report; synthetic CI sample, no data committed) ·
tests: [`gpu-worker/tests/test_sportqa_sportr_eval.py`](../gpu-worker/tests/test_sportqa_sportr_eval.py).

Sources reviewed (2026-05-31):

- SportQA paper (NAACL 2024): https://arxiv.org/abs/2402.15862 ·
  dataset: https://github.com/haotianxia/SportQA
- SportR preprint (ICLR 2026): https://arxiv.org/abs/2511.06499 ·
  dataset: https://github.com/chili-lab/SportR

---

## 1. External-resource rubric (#166)

### SportQA

| Field | Answer |
|---|---|
| **Sport coverage** | 35 sports, text MCQ. American football ✅ is explicitly one of the named non-Olympic sports (alongside baseball, ice hockey). **Also contains soccer** as a separate sport — exclude it (§2 denylist). |
| **Toledo / MAC relevance** | None directly. Generic American-football *rules/knowledge* QA, not Toledo film, not play-level tactics on Toledo formations. |
| **Runtime category** | Offline benchmark only. Never same-session/nightly; not coach-facing. |
| **License / access terms** | **CC-BY-4.0** (repo). 70,592 multiple-choice questions, three difficulty levels. Attribution required; redistribution allowed under CC-BY but we still keep raw data local/gitignored. |
| **Secret / key requirement** | None. Public download; no API key. Not read by the backend. |
| **Data privacy risk** | None — public sports-knowledge QA. No Toledo PII/medical/recruiting data. Benchmark scores are **not** Toledo labels. |
| **Model-router / registry path** | N/A — text QA, no inference variant. Any future assistant model evaluated against it still routes via `select_model(stage, priority)` and stays nightly-only until benchmarked. |
| **Overlap with closed decisions** | None. pgvector (#8/#77), single-camera (#101), SAM (#74), CFBD (#160–#163) untouched. Not a second vector DB. |
| **Calibrated-tracking dependency** | None — text-only, no tracking assumption. |

### SportR

| Field | Answer |
|---|---|
| **Sport coverage** | 5 ball/racket sports — basketball, **soccer**, table tennis, badminton, and **American football** ✅. Multimodal (images + video). Soccer present → exclude the soccer subset (§2). |
| **Toledo / MAC relevance** | None directly. Broadcast-style image/video reasoning, not Toledo single-camera practice film. Useful as an *external* reasoning probe only. |
| **Runtime category** | Offline benchmark only. Not coach-facing, not in the pipeline. |
| **License / access terms** | **Apache-2.0** (repo). 4,789 images, 2,052 videos, 20,000+ QA pairs, 6,841 human-authored chain-of-thought annotations, plus bounding-box grounding. **Full data release staged "before ICLR 2026"** — verify current availability before any use. |
| **Secret / key requirement** | None known. Public release. Not read by the backend. |
| **Data privacy risk** | Public broadcast imagery; verify no restricted footage in the released split. No Toledo data. |
| **Model-router / registry path** | N/A for this spike. A future multimodal assistant evaluated on it routes via the model router, nightly-only until benchmarked. |
| **Overlap with closed decisions** | None. Single-camera product decision (#101) means SportR's multi-angle broadcast frames are an *external probe*, never a Football-IQ capture mode. No second vector DB. |
| **Calibrated-tracking dependency** | None — it ships its own annotations; it does **not** feed Football-IQ tracking and must not be confused with calibrated Toledo coordinates (#127/#128/#129). |

---

## 2. Coverage report — is American football present and useful?

Run on the harness's synthetic sample (`python -m eval.sportqa_sportr_eval demo`)
to see the shape; against the real exports point it at local JSONL
(`report --sportqa … --sportr …`). The harness counts American-football rows and
**explicitly excludes soccer** (a bare "football" is treated as soccer-ambiguous
and excluded, per `docs/external-resource-rubric.md` §2).

| Benchmark | American football present? | Modalities | What it could validate |
|---|---|---|---|
| **SportQA** | ✅ Yes — 1 of 35 sports across L1 (foundational/historical facts), L2 (rules + strategy across all 35 sports), L3 (scenario reasoning over 6 sports). | Text MCQ only | **Rules** and **situational/strategy reasoning** in *natural-language* form. Good for "does the assistant know American-football rules?" Not for video understanding. |
| **SportR** | ✅ Yes — 1 of 5 sports, with images + video, bounding-box grounding, and chain-of-thought for penalty/tactics reasoning. | Image + video | **Multimodal reasoning**: rule-grounding on a frame, tactics explanation on a clip, visual grounding (bbox). The closest external probe to a future coach-facing "explain this play" assistant. |

**Caveats that bound usefulness:**

- **Slice size.** American football is a *fraction* of each benchmark (1/35 of
  SportQA's sport axis; 1/5 of SportR). The absolute American-football example
  count must be measured on the real export before trusting any per-sport
  metric — the harness prints exactly this count.
- **Not Toledo, not single-camera.** Neither benchmark contains Toledo film,
  MAC tactics, or single-camera practice angles (#101). SportR's broadcast
  multi-angle frames are an *external* probe, not a Football-IQ capture regime.
- **Generic rules ≠ Toledo scheme.** SportQA tests general rules/strategy, not
  Toledo's formation/coverage taxonomy. A model can score well here and still be
  wrong on Toledo clips.

---

## 3. Which Football-IQ outputs could these validate?

Per the #168 acceptance criteria — state which outputs a benchmark validates:

| Football-IQ surface | SportQA | SportR | Notes |
|---|---|---|---|
| Rules knowledge (future assistant) | ✅ text | ✅ multimodal | Both probe American-football rule understanding. |
| Situational / strategy reasoning (future assistant) | ✅ text (L2/L3) | ✅ (tactics CoT) | External reasoning probe only. |
| Multimodal "explain this play" (future) | ❌ | ✅ image/video | SportR is the only multimodal option here. |
| **Concept search / classification (#144)** | ⚠️ indirect | ⚠️ indirect | Neither tests Toledo formation/coverage *classification* directly. Toledo-clip validation remains the home for #144 quality. |
| Tracking / detection / pose (#127–#131) | ❌ none | ❌ none | Out of scope — no calibrated tracking signal. |

**Bottom line:** SportQA/SportR validate *language/multimodal reasoning about
American football*, **not** Football-IQ's CV classifiers. They are a sanity probe
for a *future* assistant, not a substitute for Toledo MP4 validation of #144 or
the Phase-CV classifiers.

---

## 4. Validation plan (external benchmark ≠ Toledo clip validation)

The owner's constraint: keep a clear path *benchmark/offline → Toledo clip
validation → coach-facing confidence thresholds*, and keep outputs experimental
until they pass project thresholds.

1. **External benchmark accuracy (offline).** If/when an assistant feature is
   scoped, measure its accuracy on the **American-football subset only** of
   SportQA (text) and SportR (multimodal). Report it as *external benchmark
   accuracy* — never as a Toledo result.
2. **Toledo clip validation (authoritative).** Re-validate the same behaviour on
   corrected Toledo MP4 clips (the same loop `docs/concept-search.md` and the
   frontier-analytics gate use). External accuracy never substitutes for this.
3. **Coach-facing thresholds.** Only after Toledo-clip validation passes the
   project's confidence/calibration thresholds (Issue #146 spirit) does any
   output lose its EXPERIMENTAL marker. Until then it stays experimental, exactly
   like frontier analytics and the zero-shot concept-search results (#144).

---

## 5. Recommendation

| Resource | Recommendation |
|---|---|
| **SportQA** | **Defer (keep as an offline text-rules probe).** Permissively licensed (CC-BY-4.0), zero secrets, American football present. Worth wiring into an offline assistant-eval harness **when** a coach-facing assistant is actually scoped — not now, since Football-IQ's near-term priority is Phase-CV + Toledo/MAC analytics. The harness + rubric in this PR make adoption a small step later. |
| **SportR** | **Defer until full release + license re-check.** Strongest multimodal fit (American football, images/video, CoT, grounding) and Apache-2.0, but the **full dataset is still staged for release "before ICLR 2026"** — do not build against an unreleased split. Re-evaluate availability before adoption. |

Neither is **rejected** (both genuinely cover American football and are
permissively licensed), and neither is **adopted now**. Both stay **offline,
local-only, evaluation-only** until a coach-facing assistant feature is scoped.

**Soccer subsets of both are rejected** and the harness drops them automatically.

---

## 6. Suggested follow-up (only when an assistant is scoped)

- Open a follow-up issue: *"Offline assistant-eval harness: American-football
  subset of SportQA/SportR"* — extend `gpu-worker/eval/sportqa_sportr_eval.py`
  from coverage-only to accuracy scoring against a candidate model, **gated** on
  SportR's full release and a license re-check, and feeding the Toledo-clip
  validation step in §4. (Do not create the assistant in that issue.)
- Until then: no data committed, no model fine-tuned, no coach-facing surface.
