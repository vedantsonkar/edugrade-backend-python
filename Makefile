.PHONY: install dev run seed upgrade downgrade revision lint format typecheck test \
        test-cov docker-build docker-up docker-down psql clean

# ── Environment ──────────────────────────────────────────────────────────────
install:
	python -m venv .venv && .venv/Scripts/python -m pip install -U pip \
		&& .venv/Scripts/python -m pip install -e ".[dev]"

# ── Application ──────────────────────────────────────────────────────────────
dev:
	uvicorn app.main:app --reload --port 8000

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1

seed:
	python scripts/seed.py

# ── Migrations ───────────────────────────────────────────────────────────────
upgrade:
	alembic upgrade head

downgrade:
	alembic downgrade -1

revision:
	alembic revision --autogenerate -m "$(m)"

# ── Quality gates ────────────────────────────────────────────────────────────
lint:
	ruff check app tests scripts alembic/env.py

format:
	ruff format app tests scripts
	ruff check --fix app tests scripts

typecheck:
	mypy app

test:
	pytest

test-cov:
	pytest --cov=app --cov-report=term-missing

# ── Docker ───────────────────────────────────────────────────────────────────
docker-build:
	docker compose build api

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

# ── Utilities ────────────────────────────────────────────────────────────────
psql:
	docker compose exec postgres psql -U edugrade -d edugrade

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name "__pycache__" -exec rm -rf {} +
