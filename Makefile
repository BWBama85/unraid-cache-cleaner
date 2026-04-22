PYTHON ?= python3

.PHONY: test
test:
	$(PYTHON) -m unittest discover -s tests -v

.PHONY: run-once
run-once:
	PYTHONPATH=src $(PYTHON) -m unraid_cache_cleaner scan
