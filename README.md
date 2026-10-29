# bank-text2sql

A natural-language-to-SQL assistant over a synthetic banking database, built with FastAPI and SQLite.

Users ask questions in Turkish (e.g. *"Tüzel müşterilerin Eylül ayı ortalama kredi kullanım miktarı nedir?"*).
The system classifies intent, asks a clarifying question when the request is ambiguous, extracts the time range,
retrieves the relevant schema documentation, generates SQL with an LLM, validates it, and runs it on a read-only connection.

## Design principles

- The LLM understands language and drafts SQL; safety checks, date handling, validation and execution are deterministic code.
- Every LLM output is validated (Pydantic models, SQL AST checks).
- Defense in depth: rule-based guardrail, intent check, AST validation, read-only connection, SQLite authorizer.
- All data in this repository is synthetic.

## Status

- [ ] Phase 1 – Foundation: synthetic data, schema docs, read-only execution, basic text-to-SQL
- [ ] Phase 2 – Smart pipeline: understanding step, clarification, time parser, semantic layer
- [ ] Phase 3 – Schema RAG and evaluation
- [ ] Phase 4 – Productionization: Docker, tests, CI

## Getting started

```bash
cp .env.example .env   # then fill in your API key
```

Setup instructions will be added as Phase 1 progresses.
