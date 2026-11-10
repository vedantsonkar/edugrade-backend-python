# Frontend integration

The Next.js frontend uses a same-origin `/api/v1/*` gateway. Python owns identity, school/assignment permissions, data, file grants, and grading. Legacy frontend database records and files are not imported.

## Run locally

```powershell
Copy-Item .env.example .env
# Set JWT_SECRET and CRON_SECRET to local random values.
docker compose up -d --build
docker compose exec api python scripts/seed.py
docker compose exec api python scripts/configure_storage.py
```

The API runs on port 8000, with PostgreSQL, Redis, and SeaweedFS managed by compose. Migrations run when the API container starts. Seeding creates `demo-school` and `demo-school-2`, each with its own users, taxonomy, classroom, and quiz. Existing schools are skipped independently without changing their data. The default frontend tenant is `demo-school`; select `demo-school-2` with `NEXT_PUBLIC_DEV_HOSTNAME` and restart the frontend to demonstrate isolation. See [Test Credentials.md](<../Test Credentials.md>) for both sets of logins and the isolation walkthrough.

For native Python development, start just `postgres redis seaweedfs`, apply `alembic upgrade head`, then run `uvicorn app.main:app --reload --port 8000` from the backend directory.

Set `SEAWEED_S3_ENDPOINT` to the backend-reachable endpoint and `SEAWEED_S3_PUBLIC_ENDPOINT` to the browser-reachable endpoint used to sign URLs. Compose uses `http://seaweedfs:8333` internally and `http://localhost:8333` publicly. Configure `STORAGE_CORS_ORIGINS` for the gateway startup flag; the pinned SeaweedFS 3.80 does not implement per-bucket PutBucketCors. Bucket initialization preserves existing files.

## Authentication

Credentials and Google login set HTTP-only cookies. The frontend gateway sets `X-Edugrade-Browser: 1`; login and refresh then omit explicit token values from JSON. Other API clients retain explicit token pairs. Refresh rotates stored tokens; logout revokes the supplied refresh token.

Google login requires a registered, active student account that permits Google authentication and belongs to the selected school. OAuth state is signed, expires using the injected clock, and is bound to an HTTP-only browser cookie. Set `GOOGLE_OAUTH_REDIRECT_URI=http://localhost:3000/api/v1/auth/google/callback` and `FRONTEND_URL=http://localhost:3000` locally. Live Google login requires configured provider credentials.

## Added contracts

- `GET/PATCH /admin/school/settings`: school settings and feature configuration.
- `GET /admin/user-counts?role=STUDENT|TEACHER`: active/inactive counters independent of list filters.
- `GET /admin/audit/archives`: school-scoped object metadata.
- `GET /assignments/{id}/attempts`: paginated, answer-free results; students receive only their own attempts.
- `GET /assignments/{id}/grading-task`: latest batch task for authorized staff.
- `GET /assignments/{id}/grading-task/stream`: server-sent events (`text/event-stream`) pushing batch task snapshots to authorized staff. The worker publishes state transitions over Redis pub/sub; the stream wakes on publish with a 3s safety tick, emits `event: task` frames carrying the `BatchGradingTaskResponse` JSON, sends comment heartbeats when idle, closes on a terminal status, and caps connections at 60s (clients reconnect transparently via `EventSource`).
- `POST /assignments/{id}/grading-tasks/{taskId}/retry`: requeue a terminally failed or partially failed batch task with a fresh retry budget (staff, manageability enforced; 409 while the task is queued/running).
- Assignment responses include `state`, `submissionCount`, `mySubmission`, and `myAttempt`; lists apply role-specific state filters before pagination.
- Submission listing permits caller-owned student history. Teacher result views keep file submissions and online attempts separate.
- Attempt grading accepts `questionOverrides` and nullable `gradeOverride`; points must remain within the assessment's limits. Teacher review includes student identity and temporary snapshot URLs. Unreleased results are hidden across student endpoints.
- CSV requests accept `operation` (`create`, `update`, `status`, or existing `upsert`), `userType`, `dryRun`, and `skipInvalid`. Previews never write. Results include validation totals and row errors. Existing template headers and password generation are retained.
- Teacher creation returns `generatedPassword` once; list responses contain classroom IDs and never expose passwords.

All collections use the existing `items/page/pageSize/total/totalPages` envelope where pagination applies. Files remain storage keys; temporary URLs are generated for display. Public branding optionally includes `logoDownloadUrl` for the school's configured logo.

## Workers and validation

Run the existing cron endpoints directly on Python using `CRON_SECRET`:

```text
GET /api/v1/cron/grading?key=<secret>
GET /api/v1/cron/archive?key=<secret>
```

Grading requires Gemini configuration. Missing AI credentials produce an explicit error for direct grading; free-text assessment answers remain pending for manual grading. Queued batch work requires a configured worker pass. No worker or AI credential is needed in the frontend.

### Batch grading retries

Failed batch grading runs are retried automatically. Each execution is recorded as a `grading_attempts` row with status `PENDING`, `SUCCESS`, or `FAILED`; task responses expose the run history in `attempts` plus `retryCount`, `maxAttempts`, and `nextRetryAt`. Retryable failures (start errors, poll errors, provider job failures, per-submission failures) requeue the task as `QUEUED` within the same worker pass, so progress polling never observes a transient `FAILED` state. The first `GRADING_RETRY_FAST_RETRIES` (default 2) retries run on the next worker pass; later retries wait out an exponential backoff (`GRADING_RETRY_BACKOFF_BASE_SECONDS` × 2^(retry − fast − 1), capped at `GRADING_RETRY_BACKOFF_CAP_SECONDS`). After `GRADING_RETRY_MAX_ATTEMPTS` (default 5) total executions the task is terminal: `FAILED` for hard failures, `PARTIAL` when some submissions were graded. Retried tasks re-grade only submissions still missing a grade, preserving results applied before the failure.

```powershell
python -m ruff check app tests scripts
python -m mypy app
python -m pytest
```

Frontend tests cover the HTTP transport, gateway, session renewal, and wire adapters. Backend contract tests cover settings, CSV previews/imports, role/tenant scope, assessment review/overrides, answer integrity, OAuth policy with a fake provider, and result-release behavior.
