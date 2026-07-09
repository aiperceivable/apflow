.PHONY: help check-imports check-circular check-performance check-heavy test clean docs-prep docs-serve docs-build

help:
	@echo "Available commands:"
	@echo "  make check-imports     - Run all import checks"
	@echo "  make check-circular    - Detect circular imports"
	@echo "  make check-performance - Check import performance"
	@echo "  make check-heavy       - Check for heavy module-level imports"
	@echo "  make test              - Run tests"
	@echo "  make docs-prep         - Generate docs/index.md from README.md (mirrors CI)"
	@echo "  make docs-serve        - Prep + run mkdocs serve"
	@echo "  make docs-build        - Prep + run mkdocs build"
	@echo "  make clean             - Clean cache files"

# Run all import checks
check-imports: check-circular check-performance check-heavy

# Detect circular imports
check-circular:
	@echo "Checking for circular imports..."
	@python scripts/detect_circular_imports.py

# Check import performance
check-performance:
	@echo "Checking import performance..."
	@bash scripts/check_import_performance.sh

# Check for heavy imports at module level
check-heavy:
	@echo "Checking for heavy module-level imports..."
	@find src -name "*.py" | xargs python scripts/check_heavy_imports.py

# Run tests
test:
	@pytest tests/ -v

# Clean cache files
clean:
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete
	@find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@echo "Cache cleaned!"

# Generate docs/index.md from README.md and copy logo (mirrors .github/workflows/docs.yml)
docs-prep:
	@cp README.md docs/index.md
	@(printf '%s\n' '---' 'description: apflow documentation home for AI-perceivable distributed orchestration, quick start, architecture, and feature guides.' '---' ''; cat docs/index.md) > docs/index.md.tmp
	@mv docs/index.md.tmp docs/index.md
	@mkdir -p docs/assets
	@cp apflow-logo.svg docs/assets/ 2>/dev/null || true
	@sed -i.bak -e 's|\./docs/|./|g' -e 's|(docs/|(|g' -e 's|\./apflow-logo\.svg|./assets/apflow-logo.svg|g' docs/index.md && rm docs/index.md.bak
	@echo "docs/index.md generated from README.md"

# Serve docs locally with mkdocs
docs-serve: docs-prep
	@mkdocs serve

# Build docs locally
docs-build: docs-prep
	@mkdocs build
