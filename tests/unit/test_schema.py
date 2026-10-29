"""Each test tries to insert a row that breaks a business rule and expects the
database itself to reject it. If a test fails, a rule exists only on paper."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

SCHEMA_PATH = Path(__file__).parents[2] / "app" / "db" / "schema.sql"


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA_PATH.read_text())
    connection.execute(
        "INSERT INTO products VALUES (1, 'DEP-DEMAND', 'Demand deposit', 'DEPOSIT', 'ALL', 'TRY')"
    )
    connection.execute(
        "INSERT INTO customers VALUES "
        "(1, 'INDIVIDUAL', 'MASS', 'Test Person', '10000000146', NULL, "
        "'Ankara', 'LOW', '2025-01-10')"
    )
    connection.execute(
        "INSERT INTO accounts VALUES (1, 1, 1, '2025-02-01', NULL, 'ACTIVE')"
    )
    yield connection
    connection.close()


def insert_customer(conn: sqlite3.Connection, **overrides: object) -> None:
    row: dict[str, object] = {
        "customer_id": 2,
        "customer_type": "CORPORATE",
        "segment": "SME",
        "full_name": "Test Ltd",
        "national_id": None,
        "tax_no": "1234567890",
        "city": "Istanbul",
        "risk_band": "MEDIUM",
        "onboarding_date": "2025-03-01",
    } | overrides
    columns = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO customers ({columns}) VALUES ({placeholders})", tuple(row.values()))


def test_valid_corporate_customer_is_accepted(conn: sqlite3.Connection) -> None:
    insert_customer(conn)


def test_individual_without_national_id_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        insert_customer(conn, customer_type="INDIVIDUAL", segment="MASS", tax_no=None)


def test_corporate_with_national_id_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        insert_customer(conn, national_id="10000000078")


def test_segment_must_match_customer_type(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        insert_customer(conn, segment="PRIVATE")


@pytest.mark.parametrize("bad_date", ["01.03.2025", "2025-02-30", "2025-3-1", "2025-13-01"])
def test_invalid_or_non_iso_dates_are_rejected(conn: sqlite3.Connection, bad_date: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        insert_customer(conn, onboarding_date=bad_date)


def test_leap_day_is_accepted(conn: sqlite3.Connection) -> None:
    insert_customer(conn, onboarding_date="2024-02-29")


def test_negative_balance_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO account_daily_balances VALUES (1, '2025-02-01', -0.01)")


def test_close_date_before_open_date_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO accounts VALUES (2, 1, 1, '2025-05-01', '2025-04-01', 'CLOSED')"
        )


def test_closed_status_requires_close_date(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO accounts VALUES (2, 1, 1, '2025-05-01', NULL, 'CLOSED')")


def test_transaction_amount_must_be_positive(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO transactions VALUES "
            "(1, 1, '2025-02-02 10:00:00', 'WITHDRAWAL', 'ATM', -50)"
        )


def test_foreign_key_to_missing_account_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO account_daily_balances VALUES (999, '2025-02-01', 10)")
