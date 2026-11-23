PYTHON ?= python3

.PHONY: test offline async adaptive production-stop estimate verify integration

test:
	PYTHONPATH=. $(PYTHON) -m unittest discover -s tests -t . -v

offline:
	PYTHONPATH=. $(PYTHON) -m microtask_swarm offline --report reports/offline-pilot.json

async:
	PYTHONPATH=. $(PYTHON) -m microtask_swarm async \
		--report reports/async-benchmark.json \
		--markdown reports/async-benchmark.md

adaptive:
	PYTHONPATH=. $(PYTHON) -m microtask_swarm adaptive \
		--report reports/adaptive-benchmark.json \
		--markdown reports/adaptive-benchmark.md

production-stop:
	PYTHONPATH=. $(PYTHON) -m microtask_swarm production-stop \
		--report reports/production-stop.json \
		--markdown reports/production-stop.md

estimate:
	PYTHONPATH=. $(PYTHON) -m microtask_swarm estimate --pretty

verify:
	PYTHONPATH=. $(PYTHON) -m microtask_swarm verify-fixtures --pretty

integration:
	PYTHONPATH=.:../../python uv run --with jsonschema --with pyyaml \
		$(PYTHON) -m unittest tests.test_integration_microtasks -v
