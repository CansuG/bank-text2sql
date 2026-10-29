"""Tests for the synthetic data generator, run on a small config so they stay fast."""

import hashlib
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from scripts.generate_data import GeneratorConfig, build_database, validate

SMALL = GeneratorConfig(seed=7, n_individual=60, n_corporate=12, end=date(2025, 3, 31))


def table_fingerprint(db_path: Path) -> str:
    """Hash every row of every data table, so two databases can be compared exactly."""
    digest = hashlib.sha256()
    with sqlite3.connect(db_path) as conn:
        for table in ("customers", "accounts", "account_daily_balances", "transactions", "loans"):
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2"):
                digest.update(repr(row).encode())
    return digest.hexdigest()


def test_generated_database_passes_validation(tmp_path: Path) -> None:
    db_path = tmp_path / "bank.db"
    counts = build_database(db_path, SMALL)

    assert counts["customers"] == 72
    with sqlite3.connect(db_path) as conn:
        assert validate(conn) == {}


def test_same_seed_produces_identical_database(tmp_path: Path) -> None:
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    build_database(first, SMALL)
    build_database(second, SMALL)

    assert table_fingerprint(first) == table_fingerprint(second)


def test_corporate_balances_are_higher_than_individual(tmp_path: Path) -> None:
    db_path = tmp_path / "bank.db"
    build_database(db_path, SMALL)

    with sqlite3.connect(db_path) as conn:
        averages = dict(
            conn.execute(
                """
                SELECT c.customer_type, AVG(b.balance)
                FROM account_daily_balances b
                JOIN accounts a USING (account_id)
                JOIN customers c USING (customer_id)
                GROUP BY c.customer_type
                """
            ).fetchall()
        )
    assert averages["CORPORATE"] > 5 * averages["INDIVIDUAL"]


def test_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    db_path = tmp_path / "bank.db"
    build_database(db_path, SMALL)

    with pytest.raises(FileExistsError):
        build_database(db_path, SMALL)
    build_database(db_path, SMALL, force=True)  # explicit overwrite is allowed


def test_validation_catches_broken_balance_chain(tmp_path: Path) -> None:
    """Corrupt one balance and make sure the continuity rule notices."""
    db_path = tmp_path / "bank.db"
    build_database(db_path, SMALL)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE account_daily_balances SET balance = balance + 100
            WHERE rowid = (SELECT rowid FROM account_daily_balances
                           WHERE balance_date = '2025-02-15' LIMIT 1)
            """
        )
        assert "balance_does_not_match_flows" in validate(conn)
