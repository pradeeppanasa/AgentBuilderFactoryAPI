.PHONY: dev down logs clean

# Full local dev bootstrap from a fresh clone — see scripts/local-setup.sh
# and docs/local-dev.md.
dev:
	bash scripts/local-setup.sh

down:
	docker-compose down

# Follow the runtime container's logs.
logs:
	docker-compose logs -f agent-builder-runtime

# Tear down containers AND volumes (wipes Postgres data) plus the cached
# Python venv hash, forcing a full re-bootstrap on the next `make dev`.
clean: down
	docker-compose down -v
	rm -f .venv/.requirements.sha256
