-- =============================================================================
-- Migration 0004: a payment can be made toward one line of a tenant's history.
--
-- "Mark paid" on a line (e.g. Rent 2026-02) records a payment with charge_id
-- set; it pays that line first, and anything else is applied oldest first as
-- before. If the line is deleted later, the payment simply goes back to being
-- applied oldest first.
-- =============================================================================

ALTER TABLE payments ADD COLUMN charge_id INTEGER REFERENCES charges(id) ON DELETE SET NULL;
