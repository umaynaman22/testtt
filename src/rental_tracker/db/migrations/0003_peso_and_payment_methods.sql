-- =============================================================================
-- Migration 0003: GCash, free-text "Other" payment methods, and pesos.
--
-- * Payment methods are now cash, check, bank transfer, GCash and other.
--   method_other holds what was typed for "Other" (e.g. 'PayMaya').
--   Older methods stay valid so existing payments keep their history.
-- * SQLite can't change a CHECK constraint in place, so the payments table is
--   rebuilt (https://sqlite.org/lang_altertable.html#otheralter). The app runs
--   this with foreign keys off and checks them before committing.
-- * Money is shown in Philippine pesos.
-- =============================================================================
-- requires: foreign_keys=off

CREATE TABLE payments_new (
    id                   INTEGER PRIMARY KEY,
    lease_id             INTEGER NOT NULL REFERENCES leases(id),
    paid_by_tenant_id    INTEGER REFERENCES tenants(id),
    received_date        TEXT NOT NULL,
    amount_cents         INTEGER NOT NULL CHECK (amount_cents > 0),
    method               TEXT NOT NULL
                         CHECK (method IN ('cash','check','bank_transfer','gcash','other',
                                           'money_order','card','app_transfer','housing_assistance',
                                           'deposit_applied')),
    method_other         TEXT,                     -- what was typed when method = 'other'
    reference            TEXT,                     -- check #, confirmation #
    receipt_number       TEXT UNIQUE,              -- sequential, e.g. 'R-2026-000123'
    bank_transaction_id  INTEGER REFERENCES bank_transactions(id),
    voided_at            TEXT,                     -- bounced check / entry error
    void_reason          TEXT,
    notes                TEXT,
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    CHECK (voided_at IS NULL OR void_reason IS NOT NULL)
);

INSERT INTO payments_new (id, lease_id, paid_by_tenant_id, received_date, amount_cents, method, reference,
                          receipt_number, bank_transaction_id, voided_at, void_reason, notes, created_at)
SELECT id, lease_id, paid_by_tenant_id, received_date, amount_cents, method, reference,
       receipt_number, bank_transaction_id, voided_at, void_reason, notes, created_at
  FROM payments;

DROP TABLE payments;
-- Plain rename: views such as v_lease_balances point at "payments" by name and
-- work again once the new table has that name.
PRAGMA legacy_alter_table = ON;
ALTER TABLE payments_new RENAME TO payments;
PRAGMA legacy_alter_table = OFF;

CREATE INDEX ix_payments_lease_date ON payments(lease_id, received_date);
CREATE INDEX ix_payments_date ON payments(received_date);
CREATE INDEX ix_payments_bank_txn ON payments(bank_transaction_id);

UPDATE settings SET value = 'PHP' WHERE key = 'currency';
