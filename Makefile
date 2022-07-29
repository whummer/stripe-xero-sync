VENV_CMD ?= python3 -m venv
VENV_DIR ?= .venv
VENV_RUN = . $(VENV_DIR)/bin/activate
PIP_CMD ?= pip

usage:
	@fgrep -h "##" $(MAKEFILE_LIST) | fgrep -v fgrep | sed -e 's/:.*##\s*/##/g' | awk -F'##' '{ printf "%-20s %s\n", $$1, $$2 }'

install: ## Install dependencies
	test -e $(VENV_DIR) || virtualenv $(VENV_DIR)
	. $(VENV_DIR)/bin/activate; $(PIP_CMD) install -e .

dry-run:  ## Run the migration script in dry run mode
	ARGS=--dry make run

run:   ## Run the migration script (live mode!)
	. $(VENV_DIR)/bin/activate; python -m stripe_xero.migration $(ARGS)

lint: ## Run code linter
	(. .venv/bin/activate; pip install pyproject-flake8 flake8; pflake8)

format: ## Run code formatter (black)
	(. .venv/bin/activate; pip install black; black stripe_xero)

.PHONY: install usage run dry-run lint format
