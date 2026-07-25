# Frontend E2E (Playwright)

This directory documents the Playwright end-to-end suite for the
Football-IQ frontend. The suite proves that upload → Practice Inbox →
Library/Clip Review → coach correction stay wired across releases.

## What it covers

| Spec                              | Acceptance criterion                                       | Style    |
| --------------------------------- | ---------------------------------------------------------- | -------- |
| `e2e/smoke.spec.ts`               | Smoke: app loads with empty backend responses              | mocked   |
| `e2e/empty-state.spec.ts`         | Empty backend never substitutes mock clips                 | mocked   |
| `e2e/library.spec.ts`             | Seeded video appears in Practice Library                   | mocked   |
| `e2e/inbox.spec.ts`               | Seeded video appears in Practice Inbox with status         | mocked   |
| `e2e/clip-review.spec.ts`         | Clip Review opens, signed download URL path is invoked     | mocked   |
| `e2e/upload.spec.ts`              | Sample MP4 → upload-url → PUT → backend register            | mocked   |
| `e2e/correction.spec.ts`          | Coach correction POST is accepted by backend contract      | mocked   |

All specs are **deterministic and offline**. They run the app via
`next dev` and intercept every backend fetch with `page.route()` against
the fake host configured in `playwright.config.ts`:

- `NEXT_PUBLIC_API_URL=http://api.e2e.local`

Any request that escapes the mocks will fail because that hostname is
not resolvable. This is intentional: it surfaces regressions where new
code paths hit the network unexpectedly.

## Running locally

```bash
cd frontend
npm ci
# One-time browser install (downloads Chromium):
npm run e2e:install
# Run the full headless suite:
npm run e2e
# Or interactively (requires a display):
npm run e2e:ui
```

The web server runs on port 3100 by default; override with `E2E_PORT`.

## CI

The dedicated `frontend-e2e` job in `.github/workflows/ci.yml`:

1. Installs npm dependencies.
2. Installs Chromium with `npm run e2e:install`.
3. Runs the suite with `npm run e2e`.

Use the same commands locally to reproduce CI failures. The HTML
reporter writes a `playwright-report/` artifact on failure; the suite's
`.gitignore` entry keeps it out of source control.

## Fixtures

`e2e/fixtures/sample.mp4` is a 32-byte placeholder that satisfies the
`accept="video/*"` file input. It never reaches real storage because all
HTTP calls to the fake API host are intercepted by `page.route()`.

To create a richer test fixture (for example to test playback duration),
replace `sample.mp4` with another `<10kB MP4>` — anything Chromium accepts
as `video/mp4` works.

## Mocked vs. real integration

This suite is intentionally **all mocked** so that CI does not depend
on a running backend, object storage, or live auth tokens. Real
end-to-end verification against a live backend is a separate manual
exercise — point the app at it and drive the UI:

```bash
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

Manually upload a tiny MP4 and verify the Practice Inbox row appears
within a few refresh ticks. We deliberately keep that path out of CI
to avoid coupling test runs to external service availability.
