PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: install run test seed reset

install:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements.txt

run:
	$(BIN)/uvicorn app.main:app --reload

test:
	$(BIN)/pytest

# Build the demo DB and data/*.csv from synthetic data. Refuses to write
# into a DB that already has data; delete the file first to regenerate.
seed:
	$(BIN)/python -m scripts.generate_data

# Start over: delete the DB file (and its -wal/-shm sidecars), then seed again.
# Development only; the app itself never deletes anything.
reset:
	$(BIN)/python -m scripts.reset_db
	$(MAKE) seed
