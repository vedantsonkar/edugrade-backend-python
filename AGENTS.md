# AGENTS.md — Edugrade Backend (Python)

> **Read this file before writing or reviewing any code in this repository.**
> These conventions are enforced by tooling (`ruff`, `mypy --strict`) and by code review.
> The rules below are not suggestions — CI fails when they are violated.

---

## 1. Project Purpose

Standalone FastAPI backend for the EduGrade school platform (assignments, online
assessments, AI grading, multi-tenant school administration). Replaces the
Next.js-embedded API routes and the legacy `edugrade-backend` prototype.

- **Stack**: Python 3.13 · FastAPI · SQLAlchemy 2.0 async (asyncpg) · Alembic ·
  PostgreSQL (native `uuid` PKs, UUIDv7 values) · Redis (rate limiting) ·
  SeaweedFS via its S3 gateway (boto3) · Google Gemini (grading) · uvicorn · Docker.
- **API prefix**: everything is served under `/api/v1/...` with camelCase JSON.

---

## 2. SOLID — the non-negotiable core

Every class you add must be justifiable against these five principles. When in
doubt, look at how existing modules apply them.

### S — Single Responsibility

- **One reason to change per class.**
- `routes/` translate HTTP ↔ DTO only. No business rules, no SQL, no SDK calls.
- `services/` hold business rules and workflows. No HTTP concepts (no `Request`,
  no status codes), no raw SQL beyond what a repository exposes.
- `repositories/` own persistence queries only. No business decisions, no DTOs.
- `domain/` holds ORM entities + invariants that live *on* an entity (e.g.
  attempt termination logic belongs to `AssessmentAttempt`, not to a service).
- `core/` holds cross-cutting infrastructure (config, db, security, ids,
  pagination, rate limiting, storage). Nothing app-specific leaks into `core/`.
- `workers/` orchestrate long-running jobs; they compose services, never touch HTTP.

### O — Open/Closed

- **Extend via new classes, not by editing stable ones.**
- Every variable behavior is behind an abstraction with multiple implementations:
  - `GradingStrategy` — one subclass per question type (`mcq_single`,
    `mcq_multiple`, `true_false`, `free_text`).
  - `OcrProvider` — registry of named providers (`glm`, `off`); add `vision` by
    adding a class + registry entry, nothing else changes.
  - `LlmGrader` + `BatchGradingClient` — grading providers as a named registry
    (`gemini`, `openai`; built by `services/ai/registry.py`, resolved via
    `settings.grading_provider`). Both adapters emit their wire schema from the
    Pydantic models in `services/ai/schema.py` (single source of truth) and
    validate responses with the same models. Add a provider = one builder
    branch + settings keys; the worker and services never change.
  - `ObjectStorage` — SeaweedFS/S3 today; any object store tomorrow.
  - `RateLimiter` — Redis today, in-memory for tests/dev.
  - `PasswordHasher`, `TokenService`, `IdProvider`, `Clock` — all protocols.
- Adding a question type = adding one strategy class + registering it. You must
  not need to touch the submit workflow.

### L — Liskov Substitution

- Any implementation of a protocol must be **substitutable without surprises**:
  same preconditions, no narrower exceptions, no hidden extra requirements
  (e.g. a fake `ObjectStorage` used in tests must accept the same key rules).
- Services in unit tests run against fakes (`tests/fakes.py`) — if a service
  needs a "real" implementation to work, the abstraction is wrong. Fix the port.

### I — Interface Segregation

- **Narrow, role-focused ports.** No god-interfaces like `IRepository`.
- A service receives exactly the collaborators it uses. `AuthService` gets a
  `UserRepository` + `RefreshTokenRepository` — not one giant `Repository`.
- Protocols live next to their consumers (or in `core/` when shared) and expose
  only what is needed.

### D — Dependency Inversion

- **High-level modules depend on abstractions; `api/v1/deps.py` is the ONLY
  composition root** that binds interfaces to concrete classes.
- Services/repositories receive all collaborators through **constructor
  injection**. No module-level singletons, no global `settings` reads inside
  services, no `Container` imports inside `domain/`.
- Never `import boto3`, `import redis`, or `from google import genai` outside
  the matching adapter (`core/storage/s3.py`, `core/ratelimit/redis_limiter.py`,
  `services/ai/llm.py`). This is what makes the code testable and portable.
- Constructor argument explosion is expected and fine (ruff `PLR0913` is
  ignored project-wide for this reason). Do not "fix" it with hidden globals.

---

## 3. OOP conventions

- **Protocols over ABCs** for infra ports (`typing.Protocol`, runtime-checkable
  where needed). ABCs are acceptable for strategy families that share template
  logic (e.g. `BaseRepository[T]`).
- **Composition over inheritance.** Inheritance is for: strategy families,
  the generic repository base, and exception hierarchy. Nothing else without
  a comment justifying it.
- **Dataclasses / Pydantic models over dicts.** Internal service results are
  small frozen dataclasses (`RateLimitResult`, `GradedAnswer`, `PageParams`).
  Wire payloads are Pydantic schemas. A `dict[str, object]` in a service
  signature is a design smell — model it.
- **Favor value objects.** `StorageKey`, `PageParams`, `RateLimit` parse once,
  validate once, then flow through the system as typed values.
- **Entity behavior lives on entities.** Example:
  `AssessmentAttempt.register_tab_switch(max_allowed) -> bool` returns whether
  the attempt must terminate. Services coordinate; entities enforce invariants.
- **No `@staticmethod` on services.** If it doesn't need `self`, it probably
  belongs on the entity or in a dedicated value object.

---

## 4. STRICT TYPING (enforced)

`mypy --strict` and ruff `ANN` rules run in CI. "It runs" is not enough — it
must *typecheck*.

### Rules

1. **Every** module, function, method, and variable that needs it is annotated —
   including return types, including test helpers, including `__init__` params.
2. **`Any` is banned** (ruff `ANN401`, mypy `disallow_any_generics`).
   - Untrusted/shapeless data → `object`, then **narrow** with
     `isinstance` / `TypeGuard` / `cast` (documented).
   - Heterogeneous payloads → `TypedDict`, `dataclass`, or Pydantic model.
   - JSON columns → `dict[str, object]` / `list[object]`, never `Any`.
3. **No bare generics**: `list[str]`, `dict[str, int]`, `type[Model]` — never
   naked `list`, `dict`, `tuple`.
4. **`Optional` style**: prefer `X | None` (PEP 604). `implicit_optional` is off.
5. **No `# type: ignore`** without an adjacent `# noqa/mypy: <reason>` comment
   explaining why. Unused ignores are errors (`warn_unused_ignores`).
6. **SQLAlchemy**: 2.0 typed mappings only — `Mapped[X]` + `mapped_column(...)`.
   `Column()` legacy style is rejected in review.
7. **Pydantic**: schemas are fully annotated; `model_config` includes
   `extra="forbid"` on request models (typo-proof APIs).
8. **FastAPI**: route handlers declare `response_model=` (or `-> Page[Schema]`)
   and full parameter annotations; dependencies are typed callables.
9. **Async**: `async def` functions return coroutines — annotate them normally;
   never mix sync SDK calls into async code (use `asyncio.to_thread` in adapters).

### Exceptions

- Alembic `versions/` files: auto-generated code, exempt from `D`/`ANN` (see
  `per-file-ignores`). Hand-written migrations must still be typed.
- Test *bodies* may omit annotations (`tests/**` per-file-ignores), but test
  helpers/utilities must be typed.

---

## 5. DOCSTRINGS (enforced)

Google-style docstrings (**ruff `D` rules, `convention = "google"`**) are
mandatory. CI fails on missing docstrings for public objects in `app/`.

### Required coverage

| Object | Docstring required |
|---|---|
| Every module | ✅ one-paragraph purpose + responsibilities |
| Every class | ✅ purpose + key invariants/collaborators |
| Every public method/function | ✅ |
| Private helpers (`_x`) | short one-liner if non-trivial |
| Every route handler | ✅ mirrors OpenAPI summary/description |
| Enum classes | ✅; members get comments when meaning isn't obvious |
| Protocol classes | ✅ + contract notes (what implementations MUST guarantee) |

### Templates

Module:
```python
"""User authentication use cases: login, refresh rotation, logout.

Exposes :class:`AuthService`, the only place credential checking and token
issuance decisions are made. HTTP concerns (cookies, status codes) are handled
by the auth routes.
"""
```

Class:
```python
class RefreshTokenRepository:
    """Persistence for rotating refresh tokens.

    A token row is created on login/refresh and is single-use: refresh
    revokes the row it consumed (rotation) and logout revokes the presented
    token (revocation).
    """
```

Function:
```python
async def start_attempt(
    self, assignment_id: UUID, principal: UserPrincipal
) -> AssessmentStartResult:
    """Start or resume the caller's attempt for an online assignment.

    Args:
        assignment_id: ID of the online assignment to start.
        principal: Authenticated student; must belong to the assignment's school.

    Returns:
        Attempt state with answer-safe questions (correct flags stripped),
        remaining seconds, and effective proctoring settings.

    Raises:
        NotFoundError: If the assignment does not exist in the caller's school.
        ForbiddenError: If the caller is not a targeted student.
        ConflictError: If a terminated/timed-out attempt already exists.
    """
```

Rules: document **Raises** for every error a service can surface (routes rely
on it); document units (seconds/bytes) and casing in Args; keep docstrings
truthful — a stale docstring is worse than none.

---

## 6. Layering & import rules (enforced by review)

```
routes ──▶ services ──▶ repositories ──▶ domain ──▶ core
   │            │
   └── schemas ─┴──────────▶ core (config/ids/pagination/errors...)
```

- **Forbidden**: `domain/` importing anything above it; `services/` importing
  `fastapi`; `repositories/` importing schemas or services; anything in `app/`
  importing `edugrade-backend` (the legacy repo) or frontend code.
- **Schemas** never import domain models (decoupling ORM from wire format);
  mapping happens via `from_attributes` + explicit service assembly.
- The **only** file allowed to instantiate concrete adapters is
  `app/api/v1/deps.py` (+ `app/main.py` for app assembly, `scripts/`, `tests/`).

---

## 7. Naming & style

- Files/modules: `snake_case.py`, singular domain nouns (`assignment.py`).
- Classes: `PascalCase`; protocols end with the role noun (`TokenService`,
  `RateLimiter`) — no `I` prefix, no `Abstract` prefix.
- Private members: leading underscore. Constants: `UPPER_SNAKE`.
- UUID columns are typed `UUID` (native `uuid` in PG); IDs flow as `UUID` objects,
  serialized as strings only at the schema boundary.
- Errors: raise `AppError` subclasses from `core/errors.py` — never return
  tuples/None to signal failure.
- Timestamps: timezone-aware UTC everywhere (`DateTime(timezone=True)`),
  produced via the injected `Clock` (never `datetime.now()` in services).

---

## 8. Testing policy

- **Unit tests**: services against protocol fakes (in `tests/fakes.py`) — no DB,
  no network, no sleeps. Each SOLID port has at least one fake used here.
- **Integration tests**: FastAPI `TestClient` against an in-memory
  `sqlite+aiosqlite` DB with dependency overrides (`MemoryRateLimiter`,
  `InMemoryStorage`, `FakeLlmGrader`).
- Tests are arranged AAA, one behavior per test, named
  `test_<subject>_<scenario>_<expected>`.
- The legacy prototype's tests were a single 400-line module — do not repeat
  that; one file per feature area under `tests/unit/` and `tests/integration/`.

---

## 9. Workflow checklist — adding a feature

1. Domain entity/enum changes (typed mappings, docstrings).
2. Alembic revision (`make revision m="..."`) — never edit applied migrations.
3. Request/response schemas (camelCase, `extra="forbid"`, docstring'd fields).
4. Repository methods (typed, minimal, on the repository that owns the model).
5. Service logic (raise `AppError`s; docstring every public method).
6. Route handlers (thin: parse → call service → map to schema; rate limits on
   sensitive endpoints; `response_model` always).
7. Wire in `api/v1/deps.py` **only**.
8. Unit tests for the service (fakes) + integration test for the route.
9. `make lint typecheck test` must pass before committing.

---

## 10. Commands

| Command | Purpose |
|---|---|
| `make install` | create venv + install deps (incl. dev) |
| `make dev` | uvicorn with reload |
| `make upgrade` / `make downgrade` | run / roll back migrations |
| `make revision m="msg"` | autogenerate migration |
| `make seed` | idempotent dev seed data |
| `make lint` / `make format` | ruff check / ruff format |
| `make typecheck` | mypy strict |
| `make test` / `make test-cov` | pytest / pytest + coverage |
| `make docker-up` / `make docker-down` | full stack (api+postgres+redis+seaweedfs) |

---

## 11. Repository map

```
app/
├── main.py                 # create_app() — assembly only
├── core/                   # config, errors, logging, ids, clock, pagination,
│   ├── db/                 #   engine + declarative base/mixins
│   ├── security/           #   passwords, tokens, guards (FastAPI deps)
│   ├── ratelimit/          #   RateLimiter protocol + redis/memory impls
│   └── storage/            #   ObjectStorage protocol + SeaweedFS(S3) impl + keys
├── domain/                 # ORM entities + enums (no business workflows)
├── schemas/                # camelCase wire DTOs (Pydantic v2)
├── repositories/           # SQLAlchemy persistence, BaseRepository[T]
├── services/               # business use cases (+ grading strategies, ai adapters)
├── workers/                # batch grader + audit archiver orchestrators
└── api/v1/                 # deps.py (composition root) + routes/ (thin controllers)
alembic/  scripts/  tests/{unit,integration}  docker/  Makefile  AGENTS.md
```
