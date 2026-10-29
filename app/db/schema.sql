-- Synthetic banking schema (analytics replica).
-- Conventions:
--   * Dates are ISO-8601 TEXT ('YYYY-MM-DD'); timestamps 'YYYY-MM-DD HH:MM:SS'.
--     CHECK (col IS date(col, '+0 days')) rejects invalid or non-canonical values.
--     It must be IS, not =: date() returns NULL for garbage input, NULL = x is
--     NULL, and SQLite treats a NULL CHECK result as passing.
--     The '+0 days' modifier forces calendar normalization: without it SQLite
--     echoes impossible dates like '2025-02-30' back unchanged.
--   * Money is REAL rounded to 2 decimals, all in TRY (Phase 1).
--     A core banking system would use DECIMAL or integer minor units instead.
--   * Transaction amounts are always positive; direction comes from txn_type.
--   * Cross-table rules (e.g. account product must be a DEPOSIT product,
--     balance dates must be on/after account open_date) are enforced by the
--     generator and verified by a post-generation validation step.
-- Foreign keys are only enforced when each connection runs PRAGMA foreign_keys = ON.

CREATE TABLE customers (
    customer_id     INTEGER PRIMARY KEY,
    customer_type   TEXT NOT NULL CHECK (customer_type IN ('INDIVIDUAL', 'CORPORATE')),
    segment         TEXT NOT NULL,
    full_name       TEXT NOT NULL,                 -- PII: person name or company title
    national_id     TEXT UNIQUE,                   -- PII: TCKN, individuals only
    tax_no          TEXT UNIQUE,                   -- PII: tax number, corporates only
    city            TEXT NOT NULL,
    risk_band       TEXT NOT NULL CHECK (risk_band IN ('LOW', 'MEDIUM', 'HIGH')),
    onboarding_date TEXT NOT NULL CHECK (onboarding_date IS date(onboarding_date, '+0 days')),

    CHECK (
        (customer_type = 'INDIVIDUAL' AND segment IN ('MASS', 'AFFLUENT', 'PRIVATE'))
     OR (customer_type = 'CORPORATE'  AND segment IN ('MICRO', 'SME', 'COMMERCIAL', 'CORPORATE'))
    ),
    CHECK (
        (customer_type = 'INDIVIDUAL' AND national_id IS NOT NULL AND tax_no IS NULL)
     OR (customer_type = 'CORPORATE'  AND tax_no IS NOT NULL AND national_id IS NULL)
    )
);

CREATE TABLE products (
    product_id     INTEGER PRIMARY KEY,
    product_code   TEXT NOT NULL UNIQUE,
    product_name   TEXT NOT NULL,
    product_family TEXT NOT NULL CHECK (product_family IN ('DEPOSIT', 'LOAN')),
    customer_type  TEXT NOT NULL CHECK (customer_type IN ('INDIVIDUAL', 'CORPORATE', 'ALL')),
    currency       TEXT NOT NULL DEFAULT 'TRY' CHECK (currency = 'TRY')
);

-- Deposit accounts only; loans live in their own table.
CREATE TABLE accounts (
    account_id  INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers (customer_id),
    product_id  INTEGER NOT NULL REFERENCES products (product_id),
    open_date   TEXT NOT NULL CHECK (open_date IS date(open_date, '+0 days')),
    close_date  TEXT CHECK (close_date IS date(close_date, '+0 days')),
    status      TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CLOSED')),

    CHECK (close_date IS NULL OR close_date >= open_date),
    CHECK ((status = 'CLOSED') = (close_date IS NOT NULL))
);

-- End-of-day snapshot, one row per account per day.
-- Use this table (not transactions) for average balance questions.
CREATE TABLE account_daily_balances (
    account_id   INTEGER NOT NULL REFERENCES accounts (account_id),
    balance_date TEXT NOT NULL CHECK (balance_date IS date(balance_date, '+0 days')),
    balance      REAL NOT NULL CHECK (balance >= 0),

    PRIMARY KEY (account_id, balance_date)
);

CREATE TABLE loans (
    loan_id           INTEGER PRIMARY KEY,
    customer_id       INTEGER NOT NULL REFERENCES customers (customer_id),
    product_id        INTEGER NOT NULL REFERENCES products (product_id),
    principal_amount  REAL NOT NULL CHECK (principal_amount > 0),
    interest_rate     REAL NOT NULL CHECK (interest_rate > 0 AND interest_rate <= 200), -- annual %, e.g. 45.5
    term_months       INTEGER NOT NULL CHECK (term_months > 0),
    disbursement_date TEXT NOT NULL CHECK (disbursement_date IS date(disbursement_date, '+0 days')),
    status            TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CLOSED', 'DEFAULTED', 'CANCELLED'))
);

CREATE TABLE transactions (
    txn_id     INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts (account_id),
    txn_ts     TEXT NOT NULL CHECK (txn_ts IS datetime(txn_ts, '+0 days')),
    txn_type   TEXT NOT NULL CHECK (txn_type IN
                   ('DEPOSIT', 'WITHDRAWAL', 'TRANSFER_IN', 'TRANSFER_OUT', 'PAYMENT')),
    channel    TEXT NOT NULL CHECK (channel IN ('BRANCH', 'MOBILE', 'INTERNET', 'ATM')),
    amount     REAL NOT NULL CHECK (amount > 0)
);

-- Indexes for the most common access paths (joins and date-range filters).
CREATE INDEX idx_accounts_customer       ON accounts (customer_id);
CREATE INDEX idx_balances_date           ON account_daily_balances (balance_date);
CREATE INDEX idx_loans_customer          ON loans (customer_id);
CREATE INDEX idx_loans_disbursement_date ON loans (disbursement_date);
CREATE INDEX idx_transactions_account_ts ON transactions (account_id, txn_ts);
CREATE INDEX idx_transactions_ts         ON transactions (txn_ts);
