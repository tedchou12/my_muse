.PHONY: install test dev docker-up docker-down docker-logs clean

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements-dev.txt

test:
	.venv/bin/python -m pytest -q

dev:
	.venv/bin/uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f muse

clean:
	rm -rf .venv .pytest_cache __pycache__ app/__pycache__ tests/__pycache__
