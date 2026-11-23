PYTHON ?= python3

.PHONY: test offline estimate verify integration

test:
	PYTHONPATH=. $(PYTHON) -m unittest discover -s tests -t . -v

offline:
	PYTHONPATH=. $(PYTHON) -m microtask_swarm offline --report reports/offline-pilot.json

estimate:
	PYTHONPATH=. $(PYTHON) -m microtask_swarm estimate --pretty

verify:
	PYTHONPATH=. $(PYTHON) -m microtask_swarm verify-fixtures --pretty

integration:
	PYTHONPATH=.:../../python uv run --with jsonschema --with pyyaml \
		$(PYTHON) -m unittest tests.test_integration_microtasks -v
