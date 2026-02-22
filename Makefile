# SOVI Deployment Pipeline
# Usage: make deploy | make provision | make status | make logs

STUDIO      := studio
DEPLOY_PATH := ~/Work/ai/sovi
REPO        := git@github.com:ehmo/sovi.git
PSQL        := /opt/homebrew/opt/postgresql@17/bin/psql
VENV        := $(DEPLOY_PATH)/.venv/bin
KEY_PATH    := /tmp/sovi.key

# All remote commands get Homebrew on PATH
REMOTE_ENV  := export PATH=/opt/homebrew/bin:/opt/homebrew/sbin:$$PATH
REMOTE      := ssh $(STUDIO) '$(REMOTE_ENV) && cd $(DEPLOY_PATH)

.PHONY: deploy provision db-migrate db-status status logs ssh health restart sync

# ---------- Deploy ----------

deploy: ## Pull latest, install, restart dashboard
	@echo "==> Deploying to $(STUDIO)..."
	$(REMOTE) && git pull origin main'
	$(REMOTE) && git-crypt unlock 2>/dev/null || true'
	$(REMOTE) && $(VENV)/pip install -e . --quiet'
	@$(MAKE) restart
	@sleep 2
	@$(MAKE) health
	@echo "==> Deploy complete."

# ---------- Provision ----------

provision: ## Full studio setup (idempotent)
	@echo "==> Provisioning $(STUDIO)..."
	@# Clone or pull repo
	ssh $(STUDIO) '$(REMOTE_ENV) && \
		if [ -d $(DEPLOY_PATH)/.git ]; then \
			cd $(DEPLOY_PATH) && git pull origin main; \
		else \
			mkdir -p $$(dirname $(DEPLOY_PATH)) && \
			git clone $(REPO) $(DEPLOY_PATH); \
		fi'
	@# Unlock secrets
	ssh $(STUDIO) '$(REMOTE_ENV) && cd $(DEPLOY_PATH) && git-crypt unlock $(KEY_PATH) 2>/dev/null || true'
	@# Python venv + install
	ssh $(STUDIO) '$(REMOTE_ENV) && cd $(DEPLOY_PATH) && \
		if [ ! -d .venv ]; then python3.12 -m venv .venv; fi && \
		$(VENV)/pip install -e ".[dev]" --quiet'
	@# Postgres: create user + database if needed
	ssh $(STUDIO) '$(REMOTE_ENV) && \
		$(PSQL) -U $$(whoami) -d postgres -tc \
			"SELECT 1 FROM pg_roles WHERE rolname='"'"'sovi'"'"'" | grep -q 1 || \
		$(PSQL) -U $$(whoami) -d postgres -c \
			"CREATE ROLE sovi WITH LOGIN PASSWORD '"'"'sovi'"'"'"'
	ssh $(STUDIO) '$(REMOTE_ENV) && \
		$(PSQL) -U $$(whoami) -d postgres -tc \
			"SELECT 1 FROM pg_database WHERE datname='"'"'sovi'"'"'" | grep -q 1 || \
		$(PSQL) -U $$(whoami) -d postgres -c \
			"CREATE DATABASE sovi OWNER sovi"'
	@# Run migrations
	@$(MAKE) db-migrate
	@# Grant permissions
	ssh $(STUDIO) '$(REMOTE_ENV) && \
		$(PSQL) -U $$(whoami) -d sovi -c \
			"GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO sovi; \
			 GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO sovi"'
	@# Output directory
	$(REMOTE) && mkdir -p output'
	@# Install dashboard launchd plist
	@$(MAKE) _install-plist
	@echo "==> Provision complete. Run: make status"

# ---------- Database ----------

db-migrate: ## Run migration SQL files on studio
	@echo "==> Running migrations..."
	ssh $(STUDIO) '$(REMOTE_ENV) && cd $(DEPLOY_PATH) && \
		$(PSQL) -U sovi -d sovi -f migrations/001_initial_schema.sql 2>&1 | tail -3'
	ssh $(STUDIO) '$(REMOTE_ENV) && cd $(DEPLOY_PATH) && \
		$(PSQL) -U sovi -d sovi -f migrations/003_scheduler_events.sql 2>&1 | tail -3'
	@echo "==> Migrations done."

db-status: ## Show table counts via psql
	@echo "==> Database status:"
	ssh $(STUDIO) '$(REMOTE_ENV) && \
		$(PSQL) -U sovi -d sovi -c " \
			SELECT schemaname, tablename FROM pg_tables \
			WHERE schemaname = '"'"'public'"'"' ORDER BY tablename"'

# ---------- Services ----------

status: ## Show launchd services + dashboard health
	@echo "==> Services:"
	-ssh $(STUDIO) 'launchctl list | grep sovi || echo "  No sovi services loaded"'
	@echo ""
	@echo "==> Dashboard health:"
	-ssh $(STUDIO) 'curl -sf http://localhost:8888/api/overview | python3 -m json.tool 2>/dev/null || echo "  Dashboard not responding"'

health: ## Run sovi health remotely
	$(REMOTE) && $(VENV)/python -m sovi health'

restart: ## Unload + load dashboard plist
	@echo "==> Restarting dashboard..."
	-ssh $(STUDIO) 'launchctl bootout gui/$$(id -u)/com.sovi.dashboard 2>/dev/null || true'
	ssh $(STUDIO) 'launchctl bootstrap gui/$$(id -u) ~/Library/LaunchAgents/com.sovi.dashboard.plist'
	@echo "==> Dashboard restarted."

logs: ## Tail dashboard logs
	ssh $(STUDIO) 'tail -f $(DEPLOY_PATH)/output/dashboard.log $(DEPLOY_PATH)/output/dashboard.err 2>/dev/null || \
		echo "No log files found. Is the dashboard running?"'

ssh: ## Open shell on studio in deploy dir
	ssh -t $(STUDIO) 'cd $(DEPLOY_PATH) && exec $$SHELL -l'

# ---------- Sync (fast iteration fallback) ----------

sync: ## rsync local -> studio (skips .git, .venv, output)
	@echo "==> Syncing to $(STUDIO):$(DEPLOY_PATH)..."
	rsync -avz --delete \
		--exclude='.git/' \
		--exclude='.venv/' \
		--exclude='output/' \
		--exclude='__pycache__/' \
		--exclude='*.egg-info/' \
		. $(STUDIO):$(DEPLOY_PATH)/
	@echo "==> Sync complete."

# ---------- Internal ----------

_install-plist:
	@echo "==> Installing dashboard launchd plist..."
	ssh $(STUDIO) 'mkdir -p ~/Library/LaunchAgents'
	scp config/com.sovi.dashboard.plist $(STUDIO):~/Library/LaunchAgents/com.sovi.dashboard.plist
