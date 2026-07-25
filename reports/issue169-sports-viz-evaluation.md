# Issue #169 Sports Visualization Evaluation

**Date:** 2026-05-31<br>
**Scope:** Evaluate `sportypy`, `cfbplotR`, and `sportyR` for American-football field and Toledo/MAC visualization utilities.

---

## Recommendation

Use **frontend-native rendering** for production dashboards and film-review overlays.

Allow `sportypy` only as an optional analyst/reporting reference for static field diagrams, and do not add it to the backend, GPU worker, Worker, or frontend dependency graph yet. Defer `cfbplotR` and `sportyR` for production use.

This keeps Football-IQ aligned with the single-camera, calibrated-coordinate pipeline: the renderer should consume existing `field_x` / `field_y` outputs after calibration quality is known, not introduce a separate plotting or data path.

## Sample Artifact

See [issue169-field-route-sample.svg](assets/issue169-field-route-sample.svg) for a small report-only field/route chart. It is illustrative synthetic geometry, not Toledo practice data and not a coach-facing metric.

The sample shows the target output shape for a future frontend component:

- NCAA-style 120 yd by 53.3 yd field coordinate frame.
- Yard-line grid and hash marks.
- Simple receiver route paths from calibrated field coordinates.
- No external logos, vendor data, or private footage.

## Resource Governance Review

| Resource | Source URL | Sport coverage | License/access terms | Runtime category | Secret/key requirement | Privacy risk | Router/registry path | ADR/architecture overlap | #127/#128/#129 dependency |
|---|---|---|---|---|---|---|---|---|---|
| `sportypy` | https://sportypy.sportsdataverse.org and https://github.com/sportsdataverse/sportypy | Multi-sport; includes American football surfaces such as NCAA and NFL fields | GPL-3.0 code; no account required | Documentation/report-only candidate; do not add to production services now | None | Low if used only with synthetic or already-approved aggregated coordinates | None; visualization only, no inference | No new vector DB, model route, SAM path, or camera architecture | Yes for real route/spacing charts: requires calibrated field coordinates before coach-visible use |
| `cfbplotR` | https://cfbplotr.sportsdataverse.org and https://github.com/sportsdataverse/cfbplotR | College football visualization, especially team logos in ggplot2 | MIT code; package docs state CFB data/logos belong to their respective owners and are governed by their own terms | Deferred/reference only; avoid R in production backend | None for package install; no live CFBD/Sportradar calls in this scope | Medium for logos/marks because team marks are third-party IP | None; visualization only, no inference | R runtime in backend is out of scope unless explicitly justified | Not for static logos; yes for field-position charts |
| `sportyR` | https://sportyr.sportsdataverse.org and https://github.com/sportsdataverse/sportyR | Multi-sport R playing surfaces; includes American football but also soccer surfaces | GPL-3.0 code; no account required | Deferred; R dependency and GPL surface library are not needed for production | None | Low if synthetic/approved coordinates only | None; visualization only, no inference | R runtime in backend is out of scope; multi-sport API includes soccer surfaces, so avoid broad adoption | Yes for real route/spacing charts |

## Findings

### `sportypy`

`sportypy` is the best fit of the evaluated packages for Python-side exploratory diagrams. Its docs and repository show football field support, including NCAA fields using yards as the plotting unit. It can draw field surfaces and overlay tracking-style points, arrows, contours, or heatmaps through matplotlib.

The downside is dependency and licensing. The package is GPL-3.0, and adding it to a deployed backend or report service would need a deliberate license review. It also supports many sports, including soccer, so any adoption must import only American-football surfaces and avoid broad "football" discovery paths.

Decision: **do not adopt as a production dependency now**. It can be used locally by analysts for exploratory report images after license review, but production dashboards should not depend on it.

### `cfbplotR`

`cfbplotR` solves a different problem: college football team logo plotting in R/ggplot2. Its code is MIT, but the package documentation explicitly separates code licensing from the terms for CFB data and marks. That makes it risky as a source of Toledo/MAC logos for coach-facing UI unless Toledo confirms rights for each mark and storage path.

Decision: **do not adopt**. If Football-IQ needs Toledo/MAC identity in UI, use a small frontend-owned brand token set and only approved Toledo assets, not a scraped or package-provided logo catalog.

### `sportyR`

`sportyR` is the R counterpart to `sportypy`. It is GPL-3.0, R-based, and multi-sport. Because the production backend should not gain an R dependency without explicit justification, and because `sportypy` covers the same playing-surface niche in Python, `sportyR` does not add enough value.

Decision: **defer**.

## Production Path

Build field/route visuals in the frontend when calibrated coordinates are ready:

1. Normalize existing field coordinates into a single NCAA football coordinate frame.
2. Render field lines, hashes, zones, player markers, and route polylines with SVG or canvas.
3. Gate coach-visible spacing, depth, and separation values on calibration confidence.
4. Keep team branding as local, approved frontend assets or tokens.

Backend-generated static report images can be reconsidered later if a real export workflow requires them. At that point, evaluate either a small first-party matplotlib helper or a tightly scoped `sportypy` optional dependency with legal review.

## Follow-Up Issue

No follow-up implementation issue is worthwhile yet. The work should wait until calibrated tracking dependencies are ready enough for coach-visible route/spacing charts. When that lands, the useful follow-up is a frontend-native NCAA field component, not a `sportypy`/R integration.

## LICENSES.md Impact

Rows were added for `sportypy`, `cfbplotR`, and `sportyR` as evaluated/deferred resources. No runtime dependency was added.
