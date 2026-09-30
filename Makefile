# Optional convenience wrapper. Everything here is a plain `docker compose` command
# (documented in README.md), so hosts without `make` lose nothing.
ENV_FILE ?= .env
export IRIS_UID ?= $(shell id -u)
export IRIS_GID ?= $(shell id -g)
COMPOSE = docker compose --env-file $(ENV_FILE)

.PHONY: init up smoke test down reset portability

init:            ## create .env from the dev template
	@test -f .env || cp .env.example .env
up:              ## start DB (waits for healthy) and run the worker once
	$(COMPOSE) up --build --abort-on-container-exit --exit-code-from worker worker
smoke:           ## worker + smoke test (the documented smoke path)
	$(COMPOSE) --profile smoke run --build --rm smoke
test:            ## pytest incl. DB-backed integration tests, inside Docker
	$(COMPOSE) --profile test run --build --rm test
down:            ## stop containers, keep data
	$(COMPOSE) --profile smoke --profile test down
reset:           ## stop containers and DELETE the DB volume
	$(COMPOSE) --profile smoke --profile test down -v
portability:     ## run smoke path with both env templates
	./scripts/verify-portability.sh
