-- =============================================================================
-- Migration 0002: financial entries may now be deleted through the app.
--
-- Voiding is still available and keeps a visible, crossed-out record. Deleting
-- removes the row; the app first writes a full copy of it to audit_log and
-- refuses deletes dated inside the locked period (books_locked_through).
-- =============================================================================

DROP TRIGGER IF EXISTS trg_charges_no_delete;
DROP TRIGGER IF EXISTS trg_payments_no_delete;
DROP TRIGGER IF EXISTS trg_expenses_no_delete;
DROP TRIGGER IF EXISTS trg_deposit_txn_no_delete;
