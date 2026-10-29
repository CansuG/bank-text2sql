"""Generate a reproducible synthetic banking database.

Usage:
    uv run python -m scripts.generate_data
    uv run python -m scripts.generate_data --db-path data/generated/bank.db --seed 42 --force

Design notes:
    * Money amounts are log-normal (positive, right-skewed), parameterised per segment.
    * Demand-deposit balances are derived from transactions day by day, so
      balance(t) - balance(t-1) always equals the net flow of day t.
    * Cross-table rules that CHECK constraints cannot express are verified by
      `validate()` after generation; a database with violations is never written.
    * Everything is driven by one seed: same seed, same database.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from faker import Faker

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parents[1] / "app" / "db" / "schema.sql"
DEFAULT_DB_PATH = Path("data/generated/bank.db")

INFLOW_TYPES = ("DEPOSIT", "TRANSFER_IN")
TXN_SIGMA = 0.8
# Mean of a log-normal is median * exp(sigma^2 / 2); used to hit target monthly volumes.
TXN_MEAN_FACTOR = float(np.exp(TXN_SIGMA**2 / 2))
OUTFLOW_TO_INFLOW_RATIO = 0.97  # balances drift slightly upwards over time


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class GeneratorConfig:
    seed: int = 42
    n_individual: int = 2_000
    n_corporate: int = 200
    start: date = date(2025, 1, 1)
    end: date = date(2026, 9, 30)


@dataclass(frozen=True)
class SegmentProfile:
    customer_type: str
    share: float  # share within its customer type
    balance_median: float  # TRY, demand-deposit opening balance
    balance_sigma: float
    monthly_inflow_median: float  # TRY
    outflow_rate: float  # expected outflow transactions per day
    time_deposit_prob: float
    loan_principal_median: float  # TRY, before the product factor
    loan_probs: dict[str, float]  # product_code -> probability of holding it


SEGMENTS: dict[str, SegmentProfile] = {
    "MASS": SegmentProfile("INDIVIDUAL", 0.80, 15_000, 1.0, 35_000, 0.4, 0.15, 60_000,
                           {"LN-PERSONAL": 0.30, "LN-AUTO": 0.05, "LN-MORTGAGE": 0.03}),
    "AFFLUENT": SegmentProfile("INDIVIDUAL", 0.17, 150_000, 0.9, 120_000, 0.6, 0.55, 150_000,
                               {"LN-PERSONAL": 0.20, "LN-AUTO": 0.12, "LN-MORTGAGE": 0.10}),
    "PRIVATE": SegmentProfile("INDIVIDUAL", 0.03, 2_000_000, 0.8, 600_000, 0.8, 0.85, 500_000,
                              {"LN-PERSONAL": 0.05, "LN-AUTO": 0.10, "LN-MORTGAGE": 0.12}),
    "MICRO": SegmentProfile("CORPORATE", 0.50, 80_000, 1.0, 250_000, 1.5, 0.20, 400_000,
                            {"LN-SME": 0.45}),
    "SME": SegmentProfile("CORPORATE", 0.35, 600_000, 1.0, 1_500_000, 3.0, 0.40, 2_000_000,
                          {"LN-SME": 0.60}),
    "COMMERCIAL": SegmentProfile("CORPORATE", 0.12, 4_000_000, 0.9, 9_000_000, 5.0, 0.55,
                                 15_000_000, {"LN-COMMERCIAL": 0.70}),
    "CORPORATE": SegmentProfile("CORPORATE", 0.03, 25_000_000, 0.8, 50_000_000, 8.0, 0.70,
                                80_000_000, {"LN-COMMERCIAL": 0.80}),
}


@dataclass(frozen=True)
class Product:
    product_id: int
    code: str
    name: str
    family: str
    customer_type: str


PRODUCTS: dict[str, Product] = {
    p.code: p
    for p in [
        Product(1, "DEP-DEMAND-IND", "Vadesiz Mevduat - Bireysel", "DEPOSIT", "INDIVIDUAL"),
        Product(2, "DEP-TIME-IND", "Vadeli Mevduat - Bireysel", "DEPOSIT", "INDIVIDUAL"),
        Product(3, "DEP-DEMAND-CORP", "Vadesiz Mevduat - Tüzel", "DEPOSIT", "CORPORATE"),
        Product(4, "DEP-TIME-CORP", "Vadeli Mevduat - Tüzel", "DEPOSIT", "CORPORATE"),
        Product(5, "LN-PERSONAL", "İhtiyaç Kredisi", "LOAN", "INDIVIDUAL"),
        Product(6, "LN-AUTO", "Taşıt Kredisi", "LOAN", "INDIVIDUAL"),
        Product(7, "LN-MORTGAGE", "Konut Kredisi", "LOAN", "INDIVIDUAL"),
        Product(8, "LN-SME", "KOBİ Kredisi", "LOAN", "CORPORATE"),
        Product(9, "LN-COMMERCIAL", "Ticari Kredi", "LOAN", "CORPORATE"),
    ]
}


@dataclass(frozen=True)
class LoanSpec:
    base_rate: float  # annual %
    min_term: int  # months
    max_term: int
    principal_factor: float


LOAN_SPECS: dict[str, LoanSpec] = {
    "LN-PERSONAL": LoanSpec(48.0, 12, 36, 1.0),
    "LN-AUTO": LoanSpec(40.0, 12, 48, 2.5),
    "LN-MORTGAGE": LoanSpec(32.0, 60, 120, 8.0),
    "LN-SME": LoanSpec(42.0, 12, 36, 1.0),
    "LN-COMMERCIAL": LoanSpec(38.0, 6, 24, 1.0),
}

RISK_BANDS = {"LOW": 0.50, "MEDIUM": 0.35, "HIGH": 0.15}
RISK_PREMIUM = {"LOW": 0.0, "MEDIUM": 4.0, "HIGH": 9.0}
DEFAULT_PROB = {"LOW": 0.01, "MEDIUM": 0.03, "HIGH": 0.08}
CANCEL_PROB = 0.02

CITIES = {
    "İstanbul": 0.30, "Ankara": 0.12, "İzmir": 0.09, "Bursa": 0.05, "Antalya": 0.05,
    "Kocaeli": 0.04, "Konya": 0.04, "Adana": 0.04, "Gaziantep": 0.03, "Kayseri": 0.03,
    "Eskişehir": 0.03, "Samsun": 0.03, "Mersin": 0.03, "Trabzon": 0.02, "Diyarbakır": 0.02,
    "Denizli": 0.02,
}

DIGITAL_CHANNELS = {
    "INDIVIDUAL": {"MOBILE": 0.75, "INTERNET": 0.20, "BRANCH": 0.05},
    "CORPORATE": {"INTERNET": 0.70, "MOBILE": 0.20, "BRANCH": 0.10},
}
CASH_CHANNELS = {
    ("INDIVIDUAL", "DEPOSIT"): {"ATM": 0.60, "BRANCH": 0.40},
    ("INDIVIDUAL", "WITHDRAWAL"): {"ATM": 0.85, "BRANCH": 0.15},
    ("CORPORATE", "DEPOSIT"): {"BRANCH": 0.80, "ATM": 0.20},
    ("CORPORATE", "WITHDRAWAL"): {"BRANCH": 0.90, "ATM": 0.10},
}
INFLOW_MIX = {
    "INDIVIDUAL": {"TRANSFER_IN": 0.60, "DEPOSIT": 0.40},
    "CORPORATE": {"TRANSFER_IN": 0.85, "DEPOSIT": 0.15},
}
OUTFLOW_MIX = {
    "INDIVIDUAL": {"PAYMENT": 0.50, "WITHDRAWAL": 0.25, "TRANSFER_OUT": 0.25},
    "CORPORATE": {"TRANSFER_OUT": 0.60, "PAYMENT": 0.35, "WITHDRAWAL": 0.05},
}

INDIVIDUAL_MISC_INFLOW_RATE = 0.05  # small non-salary inflows per day
SALARY_SHARE = 0.80  # share of an individual's monthly inflow that arrives as salary
EXISTING_CUSTOMER_SHARE = 0.70  # customers onboarded before the simulation window
FIRST_ONBOARDING = date(2012, 1, 1)
LOAN_LOOKBACK_DAYS = 730  # loans disbursed up to two years before the window
CLOSE_PROB = {"DEMAND": 0.02, "TIME": 0.06}
# Holders of a loan product take 1 + Poisson(x) loans of it over the period.
# Corporates borrow repeatedly; without this, monthly corporate samples are too small.
LOAN_EXTRA_COUNT = {"INDIVIDUAL": 0.5, "CORPORATE": 3.0}


# --------------------------------------------------------------------------- entities


@dataclass(frozen=True)
class Customer:
    customer_id: int
    customer_type: str
    segment: str
    risk_band: str
    onboarding_date: date
    monthly_inflow: float
    payday: int  # day of month salary arrives (individuals)

    @property
    def profile(self) -> SegmentProfile:
        return SEGMENTS[self.segment]


@dataclass(frozen=True)
class Account:
    account_id: int
    customer: Customer
    product: Product
    kind: str  # "DEMAND" or "TIME"
    open_date: date
    close_date: date | None


# --------------------------------------------------------------------------- helpers


def pick(rng: np.random.Generator, weights: dict[str, float]) -> str:
    keys = list(weights)
    probs = np.array(list(weights.values()))
    return str(keys[rng.choice(len(keys), p=probs / probs.sum())])


def random_date(rng: np.random.Generator, lo: date, hi: date) -> date:
    return lo + timedelta(days=int(rng.integers(0, (hi - lo).days + 1)))


def lognormal(rng: np.random.Generator, median: float, sigma: float) -> float:
    return float(rng.lognormal(np.log(median), sigma))


def timestamp(rng: np.random.Generator, day: date, hour_lo: int, hour_hi: int) -> str:
    hour = int(rng.integers(hour_lo, hour_hi + 1))
    minute, second = int(rng.integers(0, 60)), int(rng.integers(0, 60))
    return f"{day.isoformat()} {hour:02d}:{minute:02d}:{second:02d}"


# --------------------------------------------------------------------------- generation


def generate_customers(
    rng: np.random.Generator, fake: Faker, config: GeneratorConfig
) -> tuple[list[Customer], list[tuple[object, ...]]]:
    customers: list[Customer] = []
    rows: list[tuple[object, ...]] = []
    used_tax_numbers: set[str] = set()
    plan = [("INDIVIDUAL", config.n_individual), ("CORPORATE", config.n_corporate)]

    customer_id = 0
    for customer_type, count in plan:
        segment_weights = {
            name: p.share for name, p in SEGMENTS.items() if p.customer_type == customer_type
        }
        for _ in range(count):
            customer_id += 1
            segment = pick(rng, segment_weights)
            if rng.random() < EXISTING_CUSTOMER_SHARE:
                onboarding = random_date(rng, FIRST_ONBOARDING, config.start - timedelta(days=1))
            else:
                onboarding = random_date(rng, config.start, config.end - timedelta(days=30))

            customer = Customer(
                customer_id=customer_id,
                customer_type=customer_type,
                segment=segment,
                risk_band=pick(rng, RISK_BANDS),
                onboarding_date=onboarding,
                monthly_inflow=lognormal(rng, SEGMENTS[segment].monthly_inflow_median, 0.5),
                payday=int(rng.integers(1, 29)),
            )
            customers.append(customer)

            if customer_type == "INDIVIDUAL":
                name, national_id, tax_no = fake.name(), fake.unique.ssn(), None
            else:
                tax_no = str(rng.integers(1_000_000_000, 9_999_999_999))
                while tax_no in used_tax_numbers:
                    tax_no = str(rng.integers(1_000_000_000, 9_999_999_999))
                used_tax_numbers.add(tax_no)
                name, national_id = fake.company(), None

            rows.append((
                customer_id, customer_type, segment, name, national_id, tax_no,
                pick(rng, CITIES), customer.risk_band, onboarding.isoformat(),
            ))
    return customers, rows


def generate_accounts(
    rng: np.random.Generator, customers: list[Customer], config: GeneratorConfig
) -> list[Account]:
    accounts: list[Account] = []
    for customer in customers:
        suffix = "IND" if customer.customer_type == "INDIVIDUAL" else "CORP"
        planned = [("DEMAND", PRODUCTS[f"DEP-DEMAND-{suffix}"])]
        if rng.random() < customer.profile.time_deposit_prob:
            planned.append(("TIME", PRODUCTS[f"DEP-TIME-{suffix}"]))

        for kind, product in planned:
            if kind == "DEMAND":
                open_date = min(
                    customer.onboarding_date + timedelta(days=int(rng.integers(0, 15))),
                    config.end,
                )
            else:
                open_date = random_date(rng, customer.onboarding_date, config.end)

            close_date = None
            earliest_close = max(open_date, config.start) + timedelta(days=1)
            if rng.random() < CLOSE_PROB[kind] and earliest_close <= config.end:
                close_date = random_date(rng, earliest_close, config.end)

            accounts.append(
                Account(len(accounts) + 1, customer, product, kind, open_date, close_date)
            )
    return accounts


def simulate_account(
    rng: np.random.Generator, account: Account, config: GeneratorConfig
) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    """Walk the account day by day; return (balance rows, transaction rows)."""
    customer, profile = account.customer, account.customer.profile
    ctype = customer.customer_type
    first_day = max(account.open_date, config.start)
    last_day = min(account.close_date or config.end, config.end)
    n_days = (last_day - first_day).days + 1

    balances: list[tuple[object, ...]] = []
    txns: list[tuple[object, ...]] = []
    opening_median = profile.balance_median * (3 if account.kind == "TIME" else 1)

    def record(day: date, txn_type: str, amount: float, hours: tuple[int, int]) -> None:
        channel_weights = CASH_CHANNELS.get((ctype, txn_type), DIGITAL_CHANNELS[ctype])
        txns.append((
            account.account_id, timestamp(rng, day, *hours), txn_type,
            pick(rng, channel_weights), amount,
        ))

    balance = 0.0
    opening_deposit = 0.0
    if account.open_date < config.start:
        balance = round(lognormal(rng, opening_median, profile.balance_sigma), 2)
    else:
        opening_deposit = round(lognormal(rng, opening_median, profile.balance_sigma), 2)

    is_demand = account.kind == "DEMAND"
    if ctype == "INDIVIDUAL":
        misc_in_rate = INDIVIDUAL_MISC_INFLOW_RATE
        misc_in_volume = customer.monthly_inflow * (1 - SALARY_SHARE)
    else:
        misc_in_rate = profile.outflow_rate * 0.8
        misc_in_volume = customer.monthly_inflow
    in_median = misc_in_volume / (misc_in_rate * 30 * TXN_MEAN_FACTOR)
    out_median = (customer.monthly_inflow * OUTFLOW_TO_INFLOW_RATIO
                  / (profile.outflow_rate * 30 * TXN_MEAN_FACTOR))

    in_counts = rng.poisson(misc_in_rate, n_days) if is_demand else np.zeros(n_days, int)
    out_counts = rng.poisson(profile.outflow_rate, n_days) if is_demand else np.zeros(n_days, int)

    for i in range(n_days):
        day = first_day + timedelta(days=i)

        # Inflows land in the morning, outflows later in the day.
        if i == 0 and opening_deposit > 0:
            record(day, "DEPOSIT", opening_deposit, (8, 10))
            balance = round(balance + opening_deposit, 2)

        if is_demand and ctype == "INDIVIDUAL" and day.day == customer.payday:
            salary = round(customer.monthly_inflow * SALARY_SHARE * lognormal(rng, 1.0, 0.05), 2)
            record(day, "TRANSFER_IN", salary, (7, 9))
            balance = round(balance + salary, 2)

        for _ in range(int(in_counts[i])):
            amount = round(lognormal(rng, in_median, TXN_SIGMA), 2)
            record(day, pick(rng, INFLOW_MIX[ctype]), amount, (8, 10))
            balance = round(balance + amount, 2)

        for _ in range(int(out_counts[i])):
            amount = round(lognormal(rng, out_median, TXN_SIGMA), 2)
            if amount > balance:  # never overdraw: shrink the payment to what is available
                amount = round(balance * float(rng.uniform(0.2, 0.9)), 2)
            if amount < 1:
                continue
            record(day, pick(rng, OUTFLOW_MIX[ctype]), amount, (11, 22))
            balance = round(balance - amount, 2)

        if account.close_date == day and balance > 0:
            record(day, "TRANSFER_OUT", balance, (23, 23))
            balance = 0.0

        balances.append((account.account_id, day.isoformat(), balance))

    return balances, txns


def generate_loans(
    rng: np.random.Generator, customers: list[Customer], config: GeneratorConfig
) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    for customer in customers:
        profile = customer.profile
        for code, prob in profile.loan_probs.items():
            if rng.random() >= prob:
                continue
            count = 1 + int(rng.poisson(LOAN_EXTRA_COUNT[customer.customer_type]))
            spec = LOAN_SPECS[code]
            earliest = max(
                customer.onboarding_date, config.start - timedelta(days=LOAN_LOOKBACK_DAYS)
            )
            for _ in range(count):
                disbursed = random_date(rng, earliest, config.end)
                principal = round(
                    lognormal(rng, profile.loan_principal_median * spec.principal_factor, 0.6),
                    -2,
                )
                rate = round(
                    max(5.0, spec.base_rate + RISK_PREMIUM[customer.risk_band]
                        + float(rng.normal(0, 1.5))),
                    2,
                )
                term = int(rng.integers(spec.min_term, spec.max_term + 1))

                draw = rng.random()
                if draw < CANCEL_PROB:
                    status = "CANCELLED"
                elif draw < CANCEL_PROB + DEFAULT_PROB[customer.risk_band]:
                    status = "DEFAULTED"
                elif disbursed + timedelta(days=30 * term) <= config.end:
                    status = "CLOSED"
                else:
                    status = "ACTIVE"

                rows.append((
                    len(rows) + 1, customer.customer_id, PRODUCTS[code].product_id,
                    principal, rate, term, disbursed.isoformat(), status,
                ))
    return rows


# --------------------------------------------------------------------------- validation

VALIDATION_QUERIES: dict[str, str] = {
    "account_product_mismatch": """
        SELECT COUNT(*) FROM accounts a
        JOIN products p ON p.product_id = a.product_id
        JOIN customers c ON c.customer_id = a.customer_id
        WHERE p.product_family != 'DEPOSIT'
           OR (p.customer_type != 'ALL' AND p.customer_type != c.customer_type)""",
    "loan_product_mismatch": """
        SELECT COUNT(*) FROM loans l
        JOIN products p ON p.product_id = l.product_id
        JOIN customers c ON c.customer_id = l.customer_id
        WHERE p.product_family != 'LOAN'
           OR (p.customer_type != 'ALL' AND p.customer_type != c.customer_type)""",
    "account_opened_before_onboarding": """
        SELECT COUNT(*) FROM accounts a JOIN customers c USING (customer_id)
        WHERE a.open_date < c.onboarding_date""",
    "loan_before_onboarding": """
        SELECT COUNT(*) FROM loans l JOIN customers c USING (customer_id)
        WHERE l.disbursement_date < c.onboarding_date""",
    "balance_outside_account_life": """
        SELECT COUNT(*) FROM account_daily_balances b JOIN accounts a USING (account_id)
        WHERE b.balance_date < a.open_date
           OR (a.close_date IS NOT NULL AND b.balance_date > a.close_date)""",
    "transaction_outside_account_life": """
        SELECT COUNT(*) FROM transactions t JOIN accounts a USING (account_id)
        WHERE date(t.txn_ts) < a.open_date
           OR (a.close_date IS NOT NULL AND date(t.txn_ts) > a.close_date)""",
    "balance_gaps": """
        SELECT COUNT(*) FROM (
            SELECT account_id FROM account_daily_balances GROUP BY account_id
            HAVING COUNT(*) != julianday(MAX(balance_date)) - julianday(MIN(balance_date)) + 1
        )""",
    "balance_does_not_match_flows": """
        WITH flows AS (
            SELECT account_id, date(txn_ts) AS day,
                   SUM(CASE WHEN txn_type IN ('DEPOSIT', 'TRANSFER_IN')
                            THEN amount ELSE -amount END) AS net
            FROM transactions GROUP BY account_id, date(txn_ts)
        ),
        b AS (
            SELECT account_id, balance_date, balance,
                   LAG(balance) OVER (PARTITION BY account_id ORDER BY balance_date) AS prev
            FROM account_daily_balances
        )
        SELECT COUNT(*) FROM b
        LEFT JOIN flows f ON f.account_id = b.account_id AND f.day = b.balance_date
        WHERE b.prev IS NOT NULL
          AND ABS(b.balance - b.prev - COALESCE(f.net, 0)) > 0.01""",
    "closed_account_with_balance": """
        SELECT COUNT(*) FROM accounts a JOIN account_daily_balances b
          ON b.account_id = a.account_id AND b.balance_date = a.close_date
        WHERE b.balance != 0""",
}


def validate(conn: sqlite3.Connection) -> dict[str, int]:
    """Return {rule_name: violation_count} for every rule that is broken."""
    violations = {}
    for name, query in VALIDATION_QUERIES.items():
        count = conn.execute(query).fetchone()[0]
        if count:
            violations[name] = count
    return violations


# --------------------------------------------------------------------------- build


def build_database(
    db_path: Path, config: GeneratorConfig | None = None, force: bool = False
) -> dict[str, int]:
    """Generate, validate and atomically write the database. Returns row counts."""
    config = config or GeneratorConfig()
    if db_path.exists() and not force:
        raise FileExistsError(f"{db_path} already exists; pass force=True to overwrite")

    rng = np.random.default_rng(config.seed)
    fake = Faker("tr_TR")
    fake.seed_instance(config.seed)

    customers, customer_rows = generate_customers(rng, fake, config)
    accounts = generate_accounts(rng, customers, config)
    balance_rows: list[tuple[object, ...]] = []
    txn_rows: list[tuple[object, ...]] = []
    for account in accounts:
        balances, txns = simulate_account(rng, account, config)
        balance_rows.extend(balances)
        txn_rows.extend(txns)
    loan_rows = generate_loans(rng, customers, config)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(db_path.name + ".tmp")
    tmp_path.unlink(missing_ok=True)

    conn = sqlite3.connect(tmp_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA_PATH.read_text())
        with conn:  # one transaction for all inserts
            conn.executemany(
                "INSERT INTO products VALUES (?, ?, ?, ?, ?, 'TRY')",
                [(p.product_id, p.code, p.name, p.family, p.customer_type)
                 for p in PRODUCTS.values()],
            )
            conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             customer_rows)
            conn.executemany(
                "INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?)",
                [(a.account_id, a.customer.customer_id, a.product.product_id,
                  a.open_date.isoformat(),
                  a.close_date.isoformat() if a.close_date else None,
                  "CLOSED" if a.close_date else "ACTIVE")
                 for a in accounts],
            )
            conn.executemany("INSERT INTO account_daily_balances VALUES (?, ?, ?)", balance_rows)
            conn.executemany(
                "INSERT INTO transactions (account_id, txn_ts, txn_type, channel, amount) "
                "VALUES (?, ?, ?, ?, ?)",
                txn_rows,
            )
            conn.executemany("INSERT INTO loans VALUES (?, ?, ?, ?, ?, ?, ?, ?)", loan_rows)

        violations = validate(conn)
    finally:
        conn.close()

    if violations:
        tmp_path.unlink(missing_ok=True)
        raise ValueError(f"Generated data failed validation: {violations}")

    tmp_path.replace(db_path)  # atomic: readers never see a half-written database
    return {
        "customers": len(customer_rows),
        "accounts": len(accounts),
        "account_daily_balances": len(balance_rows),
        "transactions": len(txn_rows),
        "loans": len(loan_rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--seed", type=int, default=GeneratorConfig.seed)
    parser.add_argument("--force", action="store_true", help="overwrite an existing database")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    counts = build_database(args.db_path, GeneratorConfig(seed=args.seed), force=args.force)
    for table, count in counts.items():
        logger.info("%-24s %10s rows", table, f"{count:,}")
    logger.info("written to %s", args.db_path)


if __name__ == "__main__":
    main()
