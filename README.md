# EduGrade Backend (Python)

Standalone backend for the EduGrade school platform — assignments, online
assessments with proctoring, AI grading (Gemini direct + batch), and
multi-tenant school administration. Built from scratch on SOLID/OOP
principles; see **[AGENTS.md](AGENTS.md)** for the engineering contract
(layering, docstrings, `mypy --strict`, no `Any`).

## Stack

| Concern | Choice |
|---|---|
| Framework | FastAPI (all routes under `/api/v1`, camelCase JSON) |
| ORM / DB | SQLAlchemy 2.0 async + asyncpg on **PostgreSQL** |
| Migrations | Alembic (async env) |
| IDs | **UUIDv7** values in native `uuid` columns (`uuid-utils`) |
| Auth | Standalone JWT (access 15 min + **rotating, revocable** refresh tokens) + server-side Google OAuth · bcrypt passwords |
| Rate limiting | **Redis** fixed-window counters behind a `RateLimiter` protocol (in-memory fallback for tests) |
| Object storage | **SeaweedFS** via its S3 gateway (boto3, pre-signed PUT/GET) |
| AI | Gemini (`google-genai`): direct free-text/full-submission grading + Gemini **Batch API** pipeline · GLM OCR |
| Tests | pytest + pytest-asyncio — unit tests run services against protocol fakes; integration tests run the full API on in-memory sqlite |

## Architecture

```
routes (HTTP) → services (business rules) → repositories (persistence) → domain (ORM)
        schemas (camelCase DTOs)     core (config, db, security, ratelimit, storage, ids, pagination)
```

- **DIP**: services depend only on protocols (`PasswordHasher`, `TokenService`,
  `RateLimiter`, `ObjectStorage`, `LlmGrader`, `OcrProvider`, `IdProvider`,
  `Clock`); `app/api/v1/deps.py` is the single composition root.
- **OCP**: grading strategies per question type, OCR provider registry, and
  storage/limiter backends are all swappable without touching workflows.
- **SRP**: routers translate HTTP↔DTO; entities own their invariants (e.g.
  `AssessmentAttempt.register_tab_switch` decides proctoring termination).

## Quick start (Docker — full stack)

```bash
cp .env.example .env          # set JWT_SECRET etc.
make docker-up                # api + postgres + redis + seaweedfs
docker compose exec api alembic upgrade head
docker compose exec api python scripts/seed.py
```

- API docs: http://localhost:8000/docs
- Health/ready: `/api/v1/health`, `/api/v1/ready`
- SeaweedFS S3 gateway: localhost:8333 · Redis: 6379 · Postgres: 5432

## Local development

```bash
make install      # venv + deps (requires Python 3.13+)
make dev          # uvicorn --reload (needs postgres/redis/seaweedfs, see compose)
make upgrade      # alembic upgrade head
make seed         # two isolated demo schools, each with users and a quiz
make lint typecheck test   # quality gates (CI parity)
```

Seeding creates `demo-school` (Demo Academy) and `demo-school-2` (Demo Academy 2).
Each has an admin, teacher, two students, separate taxonomy/classroom IDs, and its
own quiz. Existing schools are skipped independently without resetting passwords.

| Subdomain | Admin-tab key | Admin | Teacher | Students |
| --- | --- | --- | --- | --- |
| `demo-school` | `demo-admin-key` | `admin@demo-school.edu` | `teacher@demo-school.edu` | `student1@demo-school.edu`, `student2@demo-school.edu` |
| `demo-school-2` | `demo-admin-key-2` | `admin@demo-school-2.edu` | `teacher@demo-school-2.edu` | `student1@demo-school-2.edu`, `student2@demo-school-2.edu` |

Passwords in both schools: admin `Admin@123`, teacher `Teacher@123`, student1
`20090101`, student2 `20090102`. Teachers use the Admin tab. See
[Test Credentials.md](<Test Credentials.md>) for login and isolation demonstrations.

## API surface (summary)

| Module | Endpoints |
|---|---|
| auth | login · refresh (rotation) · logout · me · google + callback |
| school | config (auth) · config/public · validate-admin · refresh |
| admin | classes / divisions / grades CRUD · students & teachers (search + **pagination**) · bulk-csv · users/status · **stats** · **class-teachers** · **school/setup** · **audit list** · audit/download |
| assignments | role-scoped list (**q/state/classIds/page**) · CRUD · **submissions list** · submit · comments · questions (answers stripped for students) · reorder · grade-all · grade override |
| assessments | start · status (remainingSeconds) · answer · tab-switch · snapshot · submit (MCQ auto-grade + Gemini free text) |
| attempts | grade override · review |
| grading | direct AI grading |
| files | pre-signed upload / download grants (scope-checked) |
| profile | get / update · avatar upload (multipart) · password change |
| analytics | batched client events |
| cron | grading worker pass · audit archiver (`?key=CRON_SECRET`) |

Error envelope: `{"error": {"code", "message", "details"}}` · rate-limit
headers: `X-RateLimit-Limit/Remaining/Reset` · every response carries
`X-Request-Id`.

## Conventions

Read **[AGENTS.md](AGENTS.md)** before contributing — it defines the SOLID
mapping, docstring requirements (Google style, enforced by ruff `D`), and the
strict-typing policy (`mypy --strict`, `Any` banned). PRs must keep
`make lint typecheck test` green.

## Frontend integration

See [frontend integration](docs/frontend-integration.md) for gateway authentication, added API contracts, browser storage configuration, local startup, and verification. The current project requires Python 3.14, as declared in pyproject.toml.
