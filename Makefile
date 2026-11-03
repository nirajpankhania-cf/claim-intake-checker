PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: install run test

install:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements.txt

run:
	$(BIN)/uvicorn app.main:app --reload

test:
	$(BIN)/pytest
