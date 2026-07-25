# Zero-shot concept search (Issue #144)

**Status:** implemented. Coach-facing read surface; zero-shot-first, no
fine-tuning, **no new vector database** (reuses pgvector / play embeddings from
[#8](https://github.com/aahmadf123/Football-IQ/issues/8) /
[#77](https://github.com/aahmadf123/Football-IQ/issues/77)).

Coaches can ask for a concept in plain football — *"find me all the Mesh
concepts"*, *"every Cover 3 trips look"*, *"jet sweep"*, *"play action boot"* —
without labelling plays first. Per the Issue #144 backlog update, this ships
**zero-shot-first**: it works on day one with no Toledo-specific training, and
fine-tuning is an optional future upgrade.

## Endpoint

```
GET /api/v1/concept-search?q=<query>&k=20&include_experimental=true
    [&since=&until=&opponent=&side_of_ball=]
```

- **Auth:** `require_coach_or_above` — `player` / `viewer` are blocked.
- **Not workload-gated:** same cost profile as `/api/v1/search/*` (a bounded
  metadata query plus one optional pgvector pass), so it follows the same
  ungated convention.

Response envelope (`app/routers/concept_search.py`):

```jsonc
{
  "query": "cover 3 trips",
  "matched_concepts": [
    {"concept_id": "cover_3", "display_name": "Cover 3", "category": "coverage", "confidence": 0.95},
    {"concept_id": "trips",   "display_name": "Trips (3x1)", "category": "formation", "confidence": 0.95}
  ],
  "approximate": true,            // always — zero-shot concept→label mapping
  "experimental": true,           // true when embedding-expansion rows are included
  "reason": null,                 // set when nothing matched (incl. soccer rejection)
  "model_version_label": "play-embed-clip-vitb32-baseline@1.0",
  "results": [
    {"clip_id": "…", "source": "metadata",  "confidence": 0.95, "score": null, "is_experimental": false, "matched_concept_ids": ["cover_3","trips"], "label_data": {…}},
    {"clip_id": "…", "source": "embedding", "confidence": 0.81, "score": 0.81, "is_experimental": true,  "matched_concept_ids": ["cover_3","trips"], "label_data": null}
  ]
}
```

## How it works — two clearly-labelled signals

1. **Grounded metadata match (`source: "metadata"`, not experimental).**
   `app.concept_lexicon` parses the query against an **American-football-only**
   concept lexicon (formations, coverages, personnel, motion, play concepts,
   field zones) and builds SQLAlchemy predicates over the labels Football-IQ
   already trusts: `clips.label_data` (formation/coverage), `personnel_grouping`,
   `field_zone`. These are real labels, so they are **not** experimental. This is
   the part that runs with **zero fine-tuning** ("structured football metadata
   where available", per the backlog update).

   Predicate logic: **OR within a concept category, AND across categories** —
   "trips cover 3" → `(trips formations) AND (cover-3 coverages)"; "cover 2 or
   cover 3" → both coverages, so they OR.

2. **Experimental embedding expansion (`source: "embedding"`, experimental).**
   When a promoted `play_embedding` model exists, the grounded matches seed a
   centroid and we run the **existing** pgvector cosine search
   (`app.routers.search._run_vector_search`) to surface visually similar reps
   that may not carry the explicit label yet. These rows are **always**
   `is_experimental: true` and flip the envelope's `experimental` flag. Toggle
   with `CONCEPT_SEARCH_EMBEDDING_EXPANSION` (default on). When off — or when no
   embedding model is promoted — concept search returns grounded metadata
   matches only. **No second vector store is created either way.**

The whole envelope is `approximate: true`: zero-shot concept→label mapping is a
heuristic until validated on corrected Toledo clips. The front end
(`frontend/src/components/concept-search.tsx`, mounted in the library view)
renders an **Approximate** badge on the box and an **EXPERIMENTAL** badge on
every embedding-expansion row, and in mock mode it refuses to search rather than
show demo data as a real result.

## American-football only

The lexicon contains no soccer vocabulary. A query that is actually about soccer
(*xG, 4-4-2, offside, …*) and matches no football concept is rejected with a
clear `reason`, per `docs/external-resource-rubric.md` §2.

## Relationship to `/api/v1/search/text`

`/search/text` is the genuine CLIP text-tower path (Issue #195). It stays gated
behind `ENABLE_EMBEDDING_TEXT_SEARCH` (503 when off) and matches a free-text
query directly against the raw `playembeddings.clip_vector(512)` — the CLIP
*image* embedding in CLIP's shared text-image space. It is *not* required for
concept search: the fused 256-d play embedding is **not** in CLIP text space
(its visual half is a random-init projection per
`docs/embeddings-architecture.md` §7), so a raw text-tower query cannot be
cosine-compared against it. Concept search instead grounds on structured labels
and expands via the *image-derived* fused embeddings, which is why it works
today with no encoder in the backend container.

`/search/text` builds the query vector in CLIP space one of two ways:

1. a deployment-injected CLIP **text** tower (`app.state.clip_text_encoder`),
   which handles arbitrary phrasing; or
2. the precomputed **concept catalog** (`app.concept_catalog`,
   `backend/app/data/concept_catalog.json`): the query is grounded to
   American-football concept(s) **lexically** via the same lexicon used here
   (soccer rejected), and those concepts' precomputed CLIP text vectors are
   averaged — so no CLIP weights live in the backend container (only committed
   vectors, never weights).

Results are **always** `experimental: true` / `approximate: true` and never
promote to a label. When neither an encoder nor a built catalog can ground the
query, `/search/text` returns a clear `reason` and no results — it never
fabricates matches. The catalog ships **unbuilt** (every vector `null`) until
the offline encoder `gpu-worker/scripts/build_concept_catalog.py` is run with
real CLIP weights and the populated JSON is committed.

## Promotion stays a coach decision

Nothing here promotes a concept to an official label. Promotion remains the
coach-review path in `app/routers/concept_proposals.py` (HDBSCAN cluster
accept/reject) and `coach_corrections`. Concept-search results — especially
embedding-expansion rows — are discovery aids, not labels.

## Future: optional fine-tuning

Coach search feedback and `coach_corrections` are the natural training/eval
signal for a future Football-CLIP fine-tune (Issue #144's original scope). That
remains **optional and dependent on corrected Toledo data volume**; first-pass
concept search does not block on it. SportQA/SportR (Issue #168) are external
reasoning probes, not a substitute for Toledo-clip validation.
