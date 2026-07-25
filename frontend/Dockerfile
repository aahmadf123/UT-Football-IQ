# syntax=docker/dockerfile:1

FROM node:20-alpine AS base
WORKDIR /app

# ── Install dependencies ──────────────────────────────────────────────────────
FROM base AS deps
COPY package*.json .
RUN npm ci

# ── Build ─────────────────────────────────────────────────────────────────────
FROM deps AS builder
COPY . .
# NEXT_PUBLIC_* values are inlined into the static export at build time —
# runtime environment on the serve stage has no effect. Without these args the
# bundle falls back to the production Fly.io URL (see src/lib/endpoints.ts),
# which rejects CORS from localhost.
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ARG NEXT_PUBLIC_WORKER_URL=""
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL \
    NEXT_PUBLIC_WORKER_URL=$NEXT_PUBLIC_WORKER_URL
RUN npm run build

# ── Serve (for local preview — Cloudflare Pages serves in production) ─────────
FROM node:20-alpine AS runner
WORKDIR /app
RUN addgroup --system nextgroup && adduser --system --ingroup nextgroup nextuser

COPY --from=builder /app/out ./out

RUN npm install -g serve
USER nextuser

EXPOSE 3000
# No -s/--single: the export is multi-page (out/login/index.html, …). Single-
# page mode rewrites every route to the root index.html, which serves the
# dashboard shell at /login/ and causes an infinite auth-redirect reload loop.
CMD ["serve", "out", "-l", "3000"]
