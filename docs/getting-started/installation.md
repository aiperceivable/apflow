---
description: Installation guide for apflow, optional extras, development setup, and basic verification.
---

# Installation

## Requirements

- Python >= 3.11

## Install

```bash
pip install apflow
```

This installs the full stack: orchestration engine + apcore (MCP/A2A/CLI).

## Optional Extras

```bash
pip install apflow[postgres]    # PostgreSQL for distributed deployment
pip install apflow[email]       # SMTP email executor
pip install apflow[scheduling]  # Cron-like task scheduling
pip install apflow[all]         # All optional features
```

## Verify

```bash
apflow --version
apflow info
```

## Development Install

```bash
git clone https://github.com/aiperceivable/apflow.git
cd apflow
pip install -e ".[dev]"
pytest tests/
```
