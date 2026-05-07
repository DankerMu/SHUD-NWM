.PHONY: dev migrate reset-db seed-demo test lint

dev:
	@echo "Starting infrastructure services..."
	docker compose -f infra/docker-compose.dev.yml up -d
	@echo "Waiting for services to be healthy..."
	@sleep 5
	@echo ""
	@echo "=== Running Services ==="
	@echo "  PostgreSQL (TimescaleDB+PostGIS): localhost:5432"
	@echo "  MinIO API:                        localhost:9000"
	@echo "  MinIO Console:                    localhost:9001"
	@echo "  FastAPI (Swagger UI):             localhost:$(or $(API_PORT),8000)/docs"
	@echo ""
	@echo "Starting FastAPI in reload mode..."
	API_PORT=$(or $(API_PORT),8000) python -m uvicorn apps.api.main:app --host 0.0.0.0 --port $(or $(API_PORT),8000) --reload

migrate:
	@echo "Running database migrations..."
	python -m packages.common.migrate
	@echo "Migration complete."

reset-db:
	@echo "Resetting database..."
	PGPASSWORD=nhms_dev psql -h localhost -U nhms -d postgres -c "DROP DATABASE IF EXISTS nhms;" || true
	PGPASSWORD=nhms_dev psql -h localhost -U nhms -d postgres -c "CREATE DATABASE nhms;"
	$(MAKE) migrate
	$(MAKE) seed-demo
	@echo "Database reset complete."

seed-demo:
	@echo "Seeding demo data..."
	python -m db.seeds.seed_demo
	@echo "Seed complete."

test:
	python -m pytest tests/ -v

lint:
	ruff check .
	@echo "Lint passed."
