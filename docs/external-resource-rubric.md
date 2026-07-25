# External-resource rubric, soccer denylist, and license gates

Status: governance reference for all external resource proposals.

Football-IQ is the Toledo Rockets' video intelligence platform for **American
football** — college football, NFL-style analysis, and Toledo Rockets / MAC
workflows. It is **not** a soccer / association-football product.

Search results for "football analytics," "football tactical data," and
"football datasets" frequently return soccer / fútbol resources. This document
exists so that humans **and AI agents** stop before adding the wrong "football"
resource, and so that no licensed dataset or model becomes coach-visible or
production-adjacent without a fit check.

Use this alongside [`LICENSES.md`](../LICENSES.md) (the third-party model /
library register and its Dependency Gating Policy) and
[`docs/model-routing.md`](model-routing.md) (the model-router / model-registry
contract).

---

## 1. External-resource rubric

Every proposal to add an external resource — dataset, model, API, or library —
**must** answer every field below before review. Open questions are not a
blocker to *proposing*; they are a blocker to *adopting*.

| Field | What to state |
|---|---|
| **Sport coverage** | American football, college football, NFL, **or** soccer / association football. If soccer, see §2 (likely rejected). |
| **Toledo / MAC relevance** | Direct (Toledo Rockets / MAC), broad American football, or none. |
| **Runtime category** | One of: production API, cached ingestion, offline training, offline benchmark, documentation only. |
| **License / access terms** | License name + summary; account / approval required? |
| **Secret / key requirement** | Does it need an API key, token, or credential? Which env var / Actions secret? |
| **Data privacy risk** | Does it carry PII, medical / wellness, or recruiting-sensitive data? |
| **Model-router / model-registry path** | If it affects inference, which router bucket / registry path? (See [`docs/model-routing.md`](model-routing.md).) Default: nightly-only until benchmarked. |
| **Overlap with closed decisions** | Does it contradict an accepted ADR ([`docs/adr/`](adr/)) or product decision? |
| **Calibrated-tracking dependency** | Does it assume calibrated tracking? If so, note the dependency on [#127](https://github.com/aahmadf123/Football-IQ/issues/127) / [#128](https://github.com/aahmadf123/Football-IQ/issues/128) / [#129](https://github.com/aahmadf123/Football-IQ/issues/129). |

A proposal that cannot answer **Sport coverage** and **License / access terms**
is incomplete and must not be adopted.

---

## 2. Soccer / association-football denylist

The following resources cover **soccer / association football** and are
**rejected** for Football-IQ unless a separately verified American-football
product from the same vendor is proposed (in which case treat it as a brand-new
resource and run the full rubric). Do not add these as dependencies, ingestion
sources, benchmarks, or training data:

| Resource | Why rejected |
|---|---|
| `worldfootballR` | Soccer R package (FBref / Transfermarkt / Understat scrapers). |
| **SoccerNet** | Soccer broadcast video benchmark. |
| **FBref / Transfermarkt / WhoScored** packages | Soccer scrapers / data. |
| Generic **StatsBomb open data** | Soccer event data. (StatsBomb *American Football* is a separate product — see §4.) |
| **football-data.org** | Soccer API despite the name. |
| **SportMonks** football / soccer APIs | Soccer — unless a future American-football product is separately verified. |
| Generic **FIFA / UEFA / European league** datasets | Soccer. |

> **Rule of thumb for AI agents:** if a "football" resource talks about
> *pitches, matches, goals, fixtures, leagues, clubs, transfers, expected
> goals (xG), or formations like 4-4-2*, it is **soccer**. American football talks about
> *downs, drives, formations like I-formation / shotgun, coverages, snaps, and
> yards*. When in doubt, treat it as soccer and reject it.

---

## 3. License gate

Before any external dataset or model becomes **coach-visible or
production-adjacent**, the proposal must record all of the following. Until it
does, the resource stays offline / local-only.

- [ ] **Source URL** — canonical link to the dataset / model / API.
- [ ] **Terms / license summary** — license name and a one-line summary.
- [ ] **Allowed usage** — research, non-commercial, commercial, internal only;
      redistribution allowed / not allowed.
- [ ] **Storage location** — where it lives (R2 bucket, local disk, runtime
      download only).
- [ ] **Raw data handling** — may the raw data be committed, cached, or must it
      remain local?
- [ ] **Model-weight redistribution** — may weights trained on the dataset be
      redistributed?

This complements the **Dependency Gating Policy** in
[`LICENSES.md`](../LICENSES.md): any new model dependency must also be added to
`LICENSES.md` before the implementing PR merges, and model weights
(`.pt`, `.pth`, `.onnx`, `.engine`, `.safetensors`) must never be committed.

---

## 4. Examples (allowed vs. flagged)

These illustrate how the rubric applies; they are **not** a blanket approval.
Each still requires the rubric and (where coach-visible / production-adjacent)
the license gate.

| Resource | Sport | Notes |
|---|---|---|
| **CFBD** (CollegeFootballData) | College football ✅ | American football. API key required; verify license / rate terms before production use. |
| **Kaggle / BDB** (NFL Big Data Bowl) | NFL ✅ | American football tracking data. Check competition rules / license; typically offline training / benchmark, not redistributable. |
| **Sportradar** | American football ✅ (verify product) | Commercial API; confirm the specific feed is American football, contract terms, and secret handling before any production use. |
| **Roboflow** | Tooling / datasets ⚠️ | CV dataset/annotation platform; verify each dataset's sport and license individually (license/classes vary per project & version). **Use offline only (manual export), defer hosted inference** — see [`reports/spike-issue167-roboflow-statsbomb-amf.md`](../reports/spike-issue167-roboflow-statsbomb-amf.md). |
| **StatsBomb American Football (AMF)** | American football ✅ | Distinct from StatsBomb soccer open data (§2). NFL-only tracking/event data, **non-commercial license**. **Use offline / research only, defer any production feature** — see [`reports/spike-issue167-roboflow-statsbomb-amf.md`](../reports/spike-issue167-roboflow-statsbomb-amf.md). |
| **SportQA** | Documentation / benchmark ⚠️ | Text sports-QA benchmark (American football ✅ among 35 sports; CC-BY-4.0). **Deferred, offline-only** — see [`reports/spike-issue168-sportqa-sportr.md`](../reports/spike-issue168-sportqa-sportr.md). |
| **SportR** | Documentation / benchmark ⚠️ | Multimodal sports-reasoning benchmark (American football ✅ among 5 sports; Apache-2.0). **Deferred until full release** — see [`reports/spike-issue168-sportqa-sportr.md`](../reports/spike-issue168-sportqa-sportr.md). |

---

## 5. Where to record a proposal

- Open an issue using the **External resource proposal** issue template, which
  asks for the rubric fields.
- Any PR that adds an external resource must fill the **External resource**
  section of the pull request template (source / license / runtime category).
- On adoption, add a model / library row to [`LICENSES.md`](../LICENSES.md).
