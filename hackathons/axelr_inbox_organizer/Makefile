.PHONY: help install test lint demo verify drift submission clean

PY   ?= python3
PIP  ?= $(PY) -m pip
ROOT ?= /tmp/axelr-demo

help:
	@echo "AXELR Inbox Organizer — unified engine"
	@echo "  make install       install engine editable + dev deps"
	@echo "  make test          pytest"
	@echo "  make lint          ruff"
	@echo "  make verify        zero-cloud proof"
	@echo "  make drift         SKILL.md ↔ manifest consistency"
	@echo "  make demo          90-second end-to-end demo at $(ROOT)"
	@echo "  make submission    build build/axelr-inbox-organizer.zip"
	@echo "  make clean"

install:
	$(PIP) install -e ".[dev]"

test:
	$(PY) -m pytest -q tests/

lint:
	$(PY) -m ruff check axelr/ tests/ scripts/ || true

verify:
	$(PY) -m axelr.cli verify

drift:
	$(PY) scripts/check_drift.py

demo:
	@rm -rf $(ROOT) && mkdir -p $(ROOT)/inbox
	@echo "▶ writing 5 fixture invoices to $(ROOT)/inbox"
	@$(PY) -c "import pathlib; [pathlib.Path('$(ROOT)/inbox/invoice-%02d.txt'%i).write_text('INVOICE\nDate: 2026-11-%02d\nFrom: Vendor %d\nTotal: USD %d.00\nDue: 2026-11-15\n'%(i,i,i*1000)) for i in range(1,6)]"
	@echo ""
	@echo "▶ zero-cloud proof"
	@$(PY) -m axelr.cli verify || true
	@echo ""
	@echo "▶ filing fixtures"
	@for f in $(ROOT)/inbox/*.txt; do $(PY) -m axelr.cli --root $(ROOT) --log-level WARNING file $$f; done
	@echo ""
	@echo "▶ summary"
	@$(PY) -m axelr.cli --root $(ROOT) --log-level WARNING summary
	@echo ""
	@echo "▶ undo last"
	@$(PY) -m axelr.cli --root $(ROOT) --log-level WARNING undo
	@echo ""
	@echo "▶ library tree"
	@find $(ROOT)/library -type f | sort

submission:
	@set -e; \
	rm -rf build && mkdir -p build/staging; \
	cp -R submission/SKILL.md submission/references build/staging/; \
	cd build/staging && zip -qr ../axelr-inbox-organizer.zip .; \
	echo "▶ built build/axelr-inbox-organizer.zip"; \
	unzip -l build/axelr-inbox-organizer.zip

clean:
	rm -rf build/ $(ROOT) .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true