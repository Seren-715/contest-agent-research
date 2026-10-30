.PHONY: install test run image save d-selftest validate check-gateway

CASE ?= D-01
IMPL ?= vendor/d-class/$(CASE)/implementation.py
TEAM ?= team

install:
	uv sync --extra dev

test:
	uv run pytest

run:
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

d-selftest:
	uv run python scripts/d_selftest.py --case $(CASE) --implementation $(IMPL)

validate:
	uv run python scripts/validate_local.py --team $(TEAM)

check-gateway:
	uv run python scripts/check_gateway.py

image:
	./scripts/build_image.sh

save:
	docker save contest-agent:1.0 -o contest-agent.tar
