# Football-IQ — Observability Guide

This document describes the production observability layer added in Issue #115.
It covers structured logging, Prometheus metrics, health endpoints, recommended
dashboards, and alerting rules for the API, GPU worker, object storage, and job
processing subsystems.

---

## Architecture overview

```
┌──────────────┐   scrape /metrics   ┌─────────────┐   dashboards   ┌──────────┐
│ Backend API  │ ──────────────────▶  │ Prometheus   │ ────────────▶  │ Grafana  │
│ (FastAPI)    │                      │              │               │          │
├──────────────┤                      └──────────────┘               └──────────┘
│ GPU Worker   │ ──────────────────▶        ▲
│ (queue poller│   scrape :9090/metrics     │
│  + pipeline) │                            │
└──────────────┘                     alert rules
                                     (see §Alerting)
```

Both the backend API and GPU worker expose Prometheus-compatible `/metrics`
endpoints.  Structured JSON logs are emitted to stdout and can be shipped to
any log aggregator (Loki, CloudWatch, Datadog, etc.).

---

## Structured logging

All services emit **structured JSON logs** via `structlog`.  Every log event
includes the following baseline fields:

| Field       | Source           | Example                          |
|-------------|------------------|----------------------------------|
| `timestamp` | auto             | `2025-05-27T18:30:00.000000Z`    |
| `log_level` | auto             | `info`, `error`, `warning`       |
| `logger`    | auto             | `app.routers.jobs`               |
| `service`   | injected         | `football-iq-backend`            |
| `env`       | `ENVIRONMENT`    | `production`                     |

Additional context fields are added by each subsystem (e.g. `job_id`,
`job_type`, `pipeline_mode`, `key`, `bucket`, `duration_seconds`).

### Log safety

- **No secrets** (tokens, keys, signed URLs) are ever logged.
- **No raw user IDs** or PII appear in metrics labels.
- Log levels are configurable via the `LOG_LEVEL` environment variable.

---

## Health endpoints

### Backend API

| Endpoint   | Purpose                          | Success | Failure |
|------------|----------------------------------|---------|---------|
| `GET /health` | Liveness — process is running | 200     | N/A     |
| `GET /live`   | Lightweight liveness alias    | 200     | N/A     |
| `GET /ready`  | Readiness — DB reachable      | 200     | 503     |

`/health` additionally returns `service`, `environment`, and `uptime_seconds`.

`/ready` runs a `SELECT 1` against the database and returns per-check status:
```json
{"status": "ready", "checks": {"database": "ok"}}
```

### GPU Worker

The GPU worker starts a lightweight HTTP server on port `GPU_METRICS_PORT`
(default `9090`):

| Endpoint          | Purpose                          |
|-------------------|----------------------------------|
| `GET /health`     | Returns 200 if process is alive  |
| `GET /live`       | Alias for `/health`              |
| `GET /metrics`    | Prometheus metrics exposition     |

The heartbeat gauge `gpu_worker_heartbeat_timestamp` is updated every poll
cycle and after every processed job, providing a reliable liveness signal.

---

## Prometheus metrics

### Backend API metrics (exposed on `/metrics`)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `http_requests_total` | Counter | `method`, `route`, `status_code`, `service`, `env` | Total HTTP requests |
| `http_request_duration_seconds` | Histogram | `method`, `route`, `service`, `env` | Request latency |
| `jobs_started_total` | Counter | `job_type`, `pipeline_mode`, `service`, `env` | Jobs started |
| `jobs_succeeded_total` | Counter | `job_type`, `pipeline_mode`, `service`, `env` | Jobs succeeded |
| `jobs_failed_total` | Counter | `job_type`, `pipeline_mode`, `service`, `env` | Jobs failed |
| `job_duration_seconds` | Histogram | `job_type`, `pipeline_mode`, `service`, `env` | Job latency |
| `s3_operations_total` | Counter | `operation`, `outcome`, `bucket`, `service`, `env` | Object-store operations |
| `s3_operation_duration_seconds` | Histogram | `operation`, `bucket`, `service`, `env` | Object-store latency |

### GPU Worker metrics (exposed on `:9090/metrics`)

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `gpu_jobs_started_total` | Counter | `job_type`, `pipeline_mode`, `service`, `env` | GPU jobs started |
| `gpu_jobs_succeeded_total` | Counter | `job_type`, `pipeline_mode`, `service`, `env` | GPU jobs succeeded |
| `gpu_jobs_failed_total` | Counter | `job_type`, `pipeline_mode`, `service`, `env` | GPU jobs failed |
| `gpu_jobs_timed_out_total` | Counter | `job_type`, `pipeline_mode`, `service`, `env` | GPU jobs timed out |
| `gpu_job_duration_seconds` | Histogram | `job_type`, `pipeline_mode`, `service`, `env` | GPU job latency |
| `gpu_queue_poll_total` | Counter | `outcome`, `service`, `env` | Queue poll attempts |
| `gpu_queue_messages_received_total` | Counter | `service`, `env` | Messages received |
| `gpu_s3_operations_total` | Counter | `operation`, `outcome`, `bucket`, `service`, `env` | Object-store operations |
| `gpu_s3_operation_duration_seconds` | Histogram | `operation`, `bucket`, `service`, `env` | Object-store latency |
| `gpu_worker_heartbeat_timestamp` | Gauge | `service`, `env` | Last heartbeat unix ts |
| `gpu_worker_up` | Gauge | `service`, `env` | 1 if worker loop running |

### Cardinality control

- Route labels use **FastAPI route templates** (e.g. `/api/v1/clips/{clip_id}`)
  rather than concrete paths.
- No unbounded IDs (`clip_id`, `video_id`, `user_id`) appear in any metric label.
- Unmatched paths are bucketed under `/unmatched`.

---

## Recommended dashboards

### 1. API Dashboard

| Panel              | Query (PromQL)                                                  |
|--------------------|-----------------------------------------------------------------|
| Request rate       | `rate(http_requests_total{service="football-iq-backend"}[5m])`  |
| Error rate (5xx)   | `rate(http_requests_total{status_code=~"5.."}[5m])`            |
| P50 / P95 / P99    | `histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))` |
| Health status      | Panel showing `/ready` endpoint result                          |

### 2. Jobs Dashboard

| Panel                | Query (PromQL)                                                     |
|----------------------|--------------------------------------------------------------------|
| Jobs started/min     | `rate(gpu_jobs_started_total[5m]) * 60`                            |
| Job success rate     | `rate(gpu_jobs_succeeded_total[5m]) / rate(gpu_jobs_started_total[5m])` |
| Job failure rate     | `rate(gpu_jobs_failed_total[5m])`                                  |
| Job duration P95     | `histogram_quantile(0.95, rate(gpu_job_duration_seconds_bucket[5m]))` |
| Timeout count        | `increase(gpu_jobs_timed_out_total[1h])`                           |
| Queue messages/min   | `rate(gpu_queue_messages_received_total[5m]) * 60`                 |

### 3. Object Storage Dashboard

| Panel              | Query (PromQL)                                                        |
|--------------------|-----------------------------------------------------------------------|
| Operations/min     | `rate(gpu_s3_operations_total[5m]) * 60`                              |
| Failure rate       | `rate(gpu_s3_operations_total{outcome="error"}[5m])`                  |
| Latency P95        | `histogram_quantile(0.95, rate(gpu_s3_operation_duration_seconds_bucket[5m]))` |

### 4. GPU Worker Health Dashboard

| Panel              | Query (PromQL)                                                |
|--------------------|---------------------------------------------------------------|
| Worker up          | `gpu_worker_up`                                               |
| Last heartbeat     | `time() - gpu_worker_heartbeat_timestamp`                     |
| Jobs throughput    | `rate(gpu_jobs_succeeded_total[5m]) * 60`                     |
| Poll error rate    | `rate(gpu_queue_poll_total{outcome="error"}[5m])`             |

---

## Alerting rules

The following critical alerts should be configured in Prometheus / Alertmanager
(or equivalent).  Thresholds are starting points — tune based on real traffic.

```yaml
groups:
  - name: football-iq
    rules:
      # ── API ────────────────────────────────────────────────────────
      - alert: APIHighErrorRate
        expr: >
          (
            rate(http_requests_total{service="football-iq-backend",status_code=~"5.."}[5m])
            / rate(http_requests_total{service="football-iq-backend"}[5m])
          ) > 0.05
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "Backend API 5xx error rate above 5% for 5 minutes"

      - alert: APIHighLatency
        expr: >
          histogram_quantile(0.95,
            rate(http_request_duration_seconds_bucket{service="football-iq-backend"}[5m])
          ) > 2.0
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Backend API P95 latency above 2s for 10 minutes"

      # ── Jobs ───────────────────────────────────────────────────────
      - alert: JobHighFailureRate
        expr: >
          (
            rate(gpu_jobs_failed_total[5m])
            / rate(gpu_jobs_started_total[5m])
          ) > 0.10
        for: 10m
        labels:
          severity: critical
        annotations:
          summary: "GPU job failure rate above 10% for 10 minutes"

      - alert: JobQueueBacklog
        expr: >
          rate(gpu_queue_messages_received_total[5m]) == 0
          and rate(gpu_queue_poll_total{outcome="success"}[5m]) > 0
        for: 30m
        labels:
          severity: warning
        annotations:
          summary: "No queue messages received for 30 minutes despite successful polls"

      # ── GPU Worker ─────────────────────────────────────────────────
      - alert: GPUWorkerDown
        expr: gpu_worker_up == 0
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "GPU worker is down"

      - alert: GPUWorkerHeartbeatMissing
        expr: (time() - gpu_worker_heartbeat_timestamp) > 300
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "GPU worker heartbeat missing for more than 5 minutes"

      # ── Object storage ────────────────────────────────────────────
      - alert: R2HighErrorRate
        expr: >
          (
            rate(gpu_s3_operations_total{outcome="error"}[5m])
            / rate(gpu_s3_operations_total[5m])
          ) > 0.05
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Object-store error rate above 5% for 10 minutes"
```

---

## Environment variables

The following environment variables control observability behaviour.  All have
safe defaults and are **optional**.

| Variable | Default | Description |
|----------|---------|-------------|
| `ENVIRONMENT` | `development` | Used in metric labels and log context (`env` field). |
| `LOG_LEVEL` | `INFO` | Python log level for all services. |
| `GPU_METRICS_PORT` | `9090` | Port for the GPU worker's Prometheus metrics HTTP server. |

These are documented in `.env.example`.

---

## Running observability locally

1. Start the backend normally (`uvicorn app.main:app --port 8000`).
2. Visit `http://localhost:8000/metrics` to see Prometheus text output.
3. Visit `http://localhost:8000/health` and `/ready` for health checks.
4. For the GPU worker, metrics are on port `9090` by default:
   `http://localhost:9090/metrics`.
5. To add a local Prometheus scraper, add these targets to `prometheus.yml`:
   ```yaml
   scrape_configs:
     - job_name: football-iq-backend
       static_configs:
         - targets: ["host.docker.internal:8000"]
     - job_name: football-iq-gpu-worker
       static_configs:
         - targets: ["host.docker.internal:9090"]
   ```

---

## Follow-ups

- Tune alert thresholds based on real-world traffic and failure rates.
- Extend dashboards with more detailed breakdowns per pipeline stage.
- Add distributed tracing (OpenTelemetry) once the metrics foundation is stable.
- Consider Grafana provisioning files for automated dashboard deployment.
