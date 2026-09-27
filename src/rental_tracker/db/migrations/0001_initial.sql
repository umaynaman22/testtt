-- =============================================================================
-- Rental Tracker — SQLite schema, migration 0001
-- See docs/BLUEPRINT.md for the reasoning behind each table and rule.
--
-- Conventions
--   * Money is INTEGER cents ($1,250.00 -> 125000). Never REAL.
--   * Percentages are INTEGER basis points (5.00% -> 500) unless noted.
--   * Dates are TEXT 'YYYY-MM-DD'; timestamps are TEXT ISO-8601 UTC.
--   * Rent periods are TEXT 'YYYY-MM'.
--   * Financial rows (charges, payments, expenses, deposit transactions) are
--     never deleted — they are voided (voided_at + void_reason).
--   * Balances are always derived from the ledger, never stored.
--
-- Applied by rental_tracker.db.migrate(), which wraps it in a transaction and
-- sets PRAGMA user_version. Every connection runs foreign_keys=ON, busy_timeout
-- and journal_mode=WAL (see rental_tracker/db/__init__.py).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Portfolio
-- -----------------------------------------------------------------------------

-- Who legally owns the property: you, an LLC, a trust, or a client you manage for.
CREATE TABLE owners (
    id                 INTEGER PRIMARY KEY,
    name               TEXT NOT NULL,
    entity_type        TEXT NOT NULL DEFAULT 'individual'
                       CHECK (entity_type IN ('individual','llc','corporation','partnership','trust','other')),
    tax_id_last4       TEXT,
    email              TEXT,
    phone              TEXT,
    mailing_address    TEXT,
    management_fee_bp  INTEGER CHECK (management_fee_bp >= 0),  -- only if you manage for this owner
    notes              TEXT,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE properties (
    id                      INTEGER PRIMARY KEY,
    owner_id                INTEGER NOT NULL REFERENCES owners(id),
    code                    TEXT NOT NULL UNIQUE,   -- short human key, e.g. 'MAPLE-12'; used by CSV import
    name                    TEXT NOT NULL,
    property_type           TEXT NOT NULL
                            CHECK (property_type IN ('single_family','multi_family','condo','townhouse',
                                                     'mobile_home','commercial','mixed_use','other')),
    address_line1           TEXT NOT NULL,
    address_line2           TEXT,
    city                    TEXT NOT NULL,
    state                   TEXT NOT NULL,
    postal_code             TEXT NOT NULL,
    parcel_number           TEXT,
    year_built              INTEGER,
    hoa_name                TEXT,
    purchase_date           TEXT,
    purchase_price_cents    INTEGER,
    land_value_cents        INTEGER,               -- land is not depreciable
    placed_in_service_date  TEXT,                  -- depreciation start
    cash_invested_cents     INTEGER,               -- for cash-on-cash return
    estimated_value_cents   INTEGER,               -- for cap rate / equity
    value_as_of             TEXT,
    status                  TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','sold','archived')),
    sold_date               TEXT,
    notes                   TEXT,
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX ix_properties_owner ON properties(owner_id);

-- Free-form grouping for large portfolios: 'North side', 'Section 8', 'Duplexes', ...
CREATE TABLE tags (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE property_tags (
    property_id  INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    tag_id       INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (property_id, tag_id)
);

-- Every property has at least one unit (a single-family home has one unit, 'Main').
CREATE TABLE units (
    id                 INTEGER PRIMARY KEY,
    property_id        INTEGER NOT NULL REFERENCES properties(id),
    unit_label         TEXT NOT NULL,              -- 'Main', 'A', '2B'
    bedrooms           REAL,
    bathrooms          REAL,
    square_feet        INTEGER,
    market_rent_cents  INTEGER,                    -- what it would rent for today
    status             TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','offline','archived')),  -- offline = renovation, not rentable
    notes              TEXT,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (property_id, unit_label)
);

-- -----------------------------------------------------------------------------
-- Tenants & leases
-- -----------------------------------------------------------------------------

-- Do NOT store SSNs, full ID numbers or full bank numbers. Screening reports go in documents.
CREATE TABLE tenants (
    id                       INTEGER PRIMARY KEY,
    first_name               TEXT NOT NULL,
    last_name                TEXT NOT NULL,
    email                    TEXT,
    phone                    TEXT,
    alt_phone                TEXT,
    external_ref             TEXT UNIQUE,          -- your own ID from the import spreadsheet (tenant_key)
    emergency_contact_name   TEXT,
    emergency_contact_phone  TEXT,
    forwarding_address       TEXT,                 -- needed to return the deposit after move-out
    notes                    TEXT,
    created_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX ix_tenants_name ON tenants(last_name, first_name);

CREATE TABLE leases (
    id                     INTEGER PRIMARY KEY,
    unit_id                INTEGER NOT NULL REFERENCES units(id),
    status                 TEXT NOT NULL DEFAULT 'draft'
                           CHECK (status IN ('draft','future','active','month_to_month','ended','terminated')),
    start_date             TEXT NOT NULL,
    end_date               TEXT,                   -- NULL = open-ended month-to-month
    move_in_date           TEXT,
    move_out_date          TEXT,
    notice_given_date      TEXT,
    billing_start_date     TEXT,                   -- first day rent is billed in this app (import cutover); NULL = start_date
    rent_cents             INTEGER NOT NULL CHECK (rent_cents >= 0),   -- starting rent; later changes in lease_rent_changes
    rent_due_day           INTEGER NOT NULL DEFAULT 1 CHECK (rent_due_day BETWEEN 1 AND 28),
    prorate_partial_months INTEGER NOT NULL DEFAULT 1 CHECK (prorate_partial_months IN (0,1)),
    deposit_cents          INTEGER NOT NULL DEFAULT 0 CHECK (deposit_cents >= 0),  -- agreed amount; money held is in deposit_transactions
    late_fee_type          TEXT NOT NULL DEFAULT 'flat' CHECK (late_fee_type IN ('none','flat','percent')),
    late_fee_grace_days    INTEGER NOT NULL DEFAULT 5 CHECK (late_fee_grace_days >= 0),
    late_fee_flat_cents    INTEGER CHECK (late_fee_flat_cents >= 0),
    late_fee_percent_bp    INTEGER CHECK (late_fee_percent_bp >= 0),
    late_fee_max_cents     INTEGER CHECK (late_fee_max_cents >= 0),
    renewal_of_lease_id    INTEGER REFERENCES leases(id),
    notes                  TEXT,
    created_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    CHECK (end_date IS NULL OR end_date >= start_date),
    CHECK (late_fee_type <> 'flat'    OR late_fee_flat_cents IS NOT NULL),
    CHECK (late_fee_type <> 'percent' OR late_fee_percent_bp IS NOT NULL)
);
CREATE INDEX ix_leases_unit ON leases(unit_id);
CREATE INDEX ix_leases_status_end ON leases(status, end_date);
-- A unit can have only one current lease at a time.
CREATE UNIQUE INDEX ux_leases_one_current_per_unit
    ON leases(unit_id) WHERE status IN ('active','month_to_month');

CREATE TABLE lease_tenants (
    lease_id   INTEGER NOT NULL REFERENCES leases(id) ON DELETE CASCADE,
    tenant_id  INTEGER NOT NULL REFERENCES tenants(id),
    role       TEXT NOT NULL DEFAULT 'primary'
               CHECK (role IN ('primary','co_tenant','occupant','guarantor')),
    PRIMARY KEY (lease_id, tenant_id)
);
CREATE INDEX ix_lease_tenants_tenant ON lease_tenants(tenant_id);

-- Rent increases/decreases during a lease or month-to-month tenancy.
CREATE TABLE lease_rent_changes (
    id                INTEGER PRIMARY KEY,
    lease_id          INTEGER NOT NULL REFERENCES leases(id) ON DELETE CASCADE,
    effective_date    TEXT NOT NULL,
    rent_cents        INTEGER NOT NULL CHECK (rent_cents >= 0),
    notice_sent_date  TEXT,
    reason            TEXT,
    UNIQUE (lease_id, effective_date)
);

-- Pet rent, parking, storage, flat utility fee, housing-authority split, etc.
CREATE TABLE lease_recurring_charges (
    id            INTEGER PRIMARY KEY,
    lease_id      INTEGER NOT NULL REFERENCES leases(id) ON DELETE CASCADE,
    charge_type   TEXT NOT NULL CHECK (charge_type IN ('pet_rent','parking','storage','utility','other')),
    description   TEXT NOT NULL,
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
    start_date    TEXT NOT NULL,
    end_date      TEXT
);

-- -----------------------------------------------------------------------------
-- Banking (for importing statements and reconciling)
-- -----------------------------------------------------------------------------

CREATE TABLE bank_accounts (
    id                     INTEGER PRIMARY KEY,
    owner_id               INTEGER REFERENCES owners(id),
    name                   TEXT NOT NULL,
    institution            TEXT,
    account_last4          TEXT,
    is_trust_account       INTEGER NOT NULL DEFAULT 0 CHECK (is_trust_account IN (0,1)),
    opening_balance_cents  INTEGER NOT NULL DEFAULT 0,
    opening_date           TEXT,
    is_active              INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1))
);

-- Lines imported from a bank CSV/OFX export. Payments/expenses point here when matched.
-- One bank deposit can match many payments (e.g. five checks deposited together).
CREATE TABLE bank_transactions (
    id               INTEGER PRIMARY KEY,
    bank_account_id  INTEGER NOT NULL REFERENCES bank_accounts(id),
    posted_date      TEXT NOT NULL,
    amount_cents     INTEGER NOT NULL,             -- signed: + money in, - money out
    description      TEXT,
    external_id      TEXT NOT NULL,                -- OFX FITID, or hash of (date, amount, description, running #)
    status           TEXT NOT NULL DEFAULT 'unmatched' CHECK (status IN ('unmatched','matched','ignored')),
    import_batch     TEXT,
    UNIQUE (bank_account_id, external_id)
);
CREATE INDEX ix_bank_txn_status ON bank_transactions(status, posted_date);

-- -----------------------------------------------------------------------------
-- Tenant ledger: charges (what is owed) and payments (what was received)
-- balance = SUM(non-voided charges) - SUM(non-voided payments)
-- -----------------------------------------------------------------------------

CREATE TABLE charges (
    id                   INTEGER PRIMARY KEY,
    lease_id             INTEGER NOT NULL REFERENCES leases(id),
    charge_type          TEXT NOT NULL
                         CHECK (charge_type IN ('rent','late_fee','pet_rent','parking','storage','utility',
                                                'damage','repair_billback','nsf_fee','legal_fee',
                                                'opening_balance','credit','other')),
    period               TEXT,                     -- 'YYYY-MM' for recurring charges
    recurring_charge_id  INTEGER REFERENCES lease_recurring_charges(id),
    source               TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('auto','manual','import')),
    description          TEXT,
    amount_cents         INTEGER NOT NULL,
    due_date             TEXT NOT NULL,
    work_order_id        INTEGER REFERENCES work_orders(id),
    voided_at            TEXT,
    void_reason          TEXT,
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    -- credits/concessions are negative; an opening balance may be either sign; everything else positive
    CHECK (
        (charge_type = 'credit' AND amount_cents < 0) OR
        (charge_type = 'opening_balance' AND amount_cents <> 0) OR
        (charge_type NOT IN ('credit','opening_balance') AND amount_cents > 0)
    ),
    CHECK (voided_at IS NULL OR void_reason IS NOT NULL)
);
CREATE INDEX ix_charges_lease_due ON charges(lease_id, due_date);
-- Makes automatic posting idempotent: re-running "post rent" can never double-bill.
-- Voided auto charges still occupy their slot so they are not re-posted.
CREATE UNIQUE INDEX ux_charges_auto_period
    ON charges(lease_id, charge_type, period, COALESCE(recurring_charge_id, 0))
    WHERE source = 'auto';

CREATE TABLE payments (
    id                   INTEGER PRIMARY KEY,
    lease_id             INTEGER NOT NULL REFERENCES leases(id),
    paid_by_tenant_id    INTEGER REFERENCES tenants(id),
    received_date        TEXT NOT NULL,
    amount_cents         INTEGER NOT NULL CHECK (amount_cents > 0),
    method               TEXT NOT NULL
                         CHECK (method IN ('cash','check','money_order','bank_transfer','card','app_transfer',
                                           'housing_assistance','deposit_applied','other')),
    reference            TEXT,                     -- check #, confirmation #
    receipt_number       TEXT UNIQUE,              -- sequential, e.g. 'R-2026-000123'
    bank_transaction_id  INTEGER REFERENCES bank_transactions(id),
    voided_at            TEXT,                     -- bounced check / entry error
    void_reason          TEXT,
    notes                TEXT,
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    CHECK (voided_at IS NULL OR void_reason IS NOT NULL)
);
CREATE INDEX ix_payments_lease_date ON payments(lease_id, received_date);
CREATE INDEX ix_payments_date ON payments(received_date);
CREATE INDEX ix_payments_bank_txn ON payments(bank_transaction_id);

-- Security deposits are a liability (money you hold), not income.
-- held = received + interest - deduction - refund - applied_to_balance
CREATE TABLE deposit_transactions (
    id            INTEGER PRIMARY KEY,
    lease_id      INTEGER NOT NULL REFERENCES leases(id),
    txn_date      TEXT NOT NULL,
    txn_type      TEXT NOT NULL
                  CHECK (txn_type IN ('received','interest','deduction','refund','applied_to_balance')),
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
    description   TEXT,                            -- itemize deductions: 'Carpet replacement, bedroom 2'
    payment_id    INTEGER REFERENCES payments(id), -- set for applied_to_balance (method = deposit_applied)
    voided_at     TEXT,
    void_reason   TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    CHECK (voided_at IS NULL OR void_reason IS NOT NULL)
);
CREATE INDEX ix_deposit_txn_lease ON deposit_transactions(lease_id);

-- -----------------------------------------------------------------------------
-- Vendors, maintenance & expenses
-- -----------------------------------------------------------------------------

CREATE TABLE vendors (
    id                 INTEGER PRIMARY KEY,
    name               TEXT NOT NULL,
    trade              TEXT,                       -- plumber, electrician, HVAC, landscaping...
    contact_name       TEXT,
    phone              TEXT,
    email              TEXT,
    address            TEXT,
    tax_id_last4       TEXT,
    needs_1099         INTEGER NOT NULL DEFAULT 0 CHECK (needs_1099 IN (0,1)),
    license_number     TEXT,
    insurance_expires  TEXT,
    is_active          INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    notes              TEXT
);

-- Seeded with US Schedule E lines; rename/re-map for other countries.
CREATE TABLE expense_categories (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,
    tax_line         TEXT,                         -- e.g. 'Sch E line 14'
    is_capital       INTEGER NOT NULL DEFAULT 0 CHECK (is_capital IN (0,1)),  -- depreciated, not expensed
    is_active        INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1))
);

CREATE TABLE work_orders (
    id                     INTEGER PRIMARY KEY,
    property_id            INTEGER NOT NULL REFERENCES properties(id),
    unit_id                INTEGER REFERENCES units(id),
    reported_by_tenant_id  INTEGER REFERENCES tenants(id),
    vendor_id              INTEGER REFERENCES vendors(id),
    title                  TEXT NOT NULL,
    description            TEXT,
    priority               TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('emergency','high','normal','low')),
    status                 TEXT NOT NULL DEFAULT 'open'
                           CHECK (status IN ('open','scheduled','in_progress','waiting_parts','completed','cancelled')),
    reported_date          TEXT NOT NULL,
    scheduled_date         TEXT,
    completed_date         TEXT,
    estimated_cost_cents   INTEGER,
    bill_to_tenant         INTEGER NOT NULL DEFAULT 0 CHECK (bill_to_tenant IN (0,1)),
    created_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX ix_work_orders_status ON work_orders(status, priority);
CREATE INDEX ix_work_orders_property ON work_orders(property_id);

-- Insurance, HOA dues, property tax, lawn service, utilities you pay...
CREATE TABLE recurring_expenses (
    id               INTEGER PRIMARY KEY,
    property_id      INTEGER REFERENCES properties(id),  -- NULL = portfolio overhead
    vendor_id        INTEGER REFERENCES vendors(id),
    category_id      INTEGER NOT NULL REFERENCES expense_categories(id),
    description      TEXT NOT NULL,
    amount_cents     INTEGER NOT NULL CHECK (amount_cents > 0),
    interval_months  INTEGER NOT NULL CHECK (interval_months > 0),
    next_due_date    TEXT NOT NULL,
    end_date         TEXT,
    auto_post        INTEGER NOT NULL DEFAULT 0 CHECK (auto_post IN (0,1)),  -- 0 = remind only
    is_active        INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1))
);

CREATE TABLE expenses (
    id                    INTEGER PRIMARY KEY,
    property_id           INTEGER REFERENCES properties(id),   -- NULL = portfolio overhead (allocated in reports)
    unit_id               INTEGER REFERENCES units(id),
    vendor_id             INTEGER REFERENCES vendors(id),
    category_id           INTEGER NOT NULL REFERENCES expense_categories(id),
    work_order_id         INTEGER REFERENCES work_orders(id),
    recurring_expense_id  INTEGER REFERENCES recurring_expenses(id),
    bank_transaction_id   INTEGER REFERENCES bank_transactions(id),
    expense_date          TEXT NOT NULL,
    amount_cents          INTEGER NOT NULL CHECK (amount_cents > 0),
    payment_method        TEXT,
    reference             TEXT,                    -- invoice #, check #
    description           TEXT,
    voided_at             TEXT,
    void_reason           TEXT,
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    CHECK (voided_at IS NULL OR void_reason IS NOT NULL)
);
CREATE INDEX ix_expenses_property_date ON expenses(property_id, expense_date);
CREATE INDEX ix_expenses_date ON expenses(expense_date);
CREATE INDEX ix_expenses_category ON expenses(category_id);
CREATE INDEX ix_expenses_vendor ON expenses(vendor_id);

-- -----------------------------------------------------------------------------
-- Inspections
-- -----------------------------------------------------------------------------

CREATE TABLE inspections (
    id                 INTEGER PRIMARY KEY,
    unit_id            INTEGER NOT NULL REFERENCES units(id),
    lease_id           INTEGER REFERENCES leases(id),
    inspection_type    TEXT NOT NULL CHECK (inspection_type IN ('move_in','move_out','periodic','drive_by')),
    inspection_date    TEXT NOT NULL,
    inspector          TEXT,
    overall_condition  TEXT,
    notes              TEXT
);

CREATE TABLE inspection_items (
    id             INTEGER PRIMARY KEY,
    inspection_id  INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    area           TEXT NOT NULL,                  -- 'Kitchen'
    item           TEXT NOT NULL,                  -- 'Stove'
    condition      TEXT NOT NULL CHECK (condition IN ('new','good','fair','poor','damaged','missing','n/a')),
    notes          TEXT
);

-- -----------------------------------------------------------------------------
-- Documents, templates, communication
-- -----------------------------------------------------------------------------

-- Files live on disk under documents/<first 2 hex of sha256>/<sha256>.<ext>; this table is the index.
-- (related_type, related_id) is a polymorphic link, so integrity is enforced by the app.
CREATE TABLE documents (
    id                 INTEGER PRIMARY KEY,
    related_type       TEXT NOT NULL
                       CHECK (related_type IN ('owner','property','unit','tenant','lease','vendor','expense',
                                               'work_order','inspection','inspection_item','insurance_policy',
                                               'loan','communication')),
    related_id         INTEGER NOT NULL,
    doc_type           TEXT NOT NULL DEFAULT 'other'
                       CHECK (doc_type IN ('lease','addendum','id','screening','receipt','invoice','photo',
                                           'insurance','notice','letter','statement','other')),
    title              TEXT NOT NULL,
    stored_path        TEXT NOT NULL,              -- relative to the documents folder
    original_filename  TEXT,
    mime_type          TEXT,
    size_bytes         INTEGER,
    sha256             TEXT NOT NULL,
    expires_on         TEXT,                       -- triggers an alert (insurance certificates, licenses...)
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX ix_documents_related ON documents(related_type, related_id);
CREATE INDEX ix_documents_expires ON documents(expires_on) WHERE expires_on IS NOT NULL;

-- Jinja2 templates with merge fields ({{ tenant.first_name }}, {{ balance }}, ...). Review for local law.
CREATE TABLE letter_templates (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    category    TEXT NOT NULL
                CHECK (category IN ('late_notice','renewal_offer','rent_increase','entry_notice',
                                    'deposit_disposition','move_out_instructions','receipt','general')),
    body        TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Record of every call, text, letter, email and conversation with a tenant.
CREATE TABLE communications (
    id           INTEGER PRIMARY KEY,
    tenant_id    INTEGER REFERENCES tenants(id),
    lease_id     INTEGER REFERENCES leases(id),
    occurred_at  TEXT NOT NULL,
    channel      TEXT NOT NULL CHECK (channel IN ('phone','sms','email','letter','in_person','posted_notice','other')),
    direction    TEXT NOT NULL CHECK (direction IN ('in','out')),
    summary      TEXT NOT NULL
);
CREATE INDEX ix_communications_tenant ON communications(tenant_id, occurred_at);

-- User-created and recurring reminders. Lease expirations, insurance renewals, etc. are
-- computed from their own tables at runtime and do not need rows here.
CREATE TABLE reminders (
    id                      INTEGER PRIMARY KEY,
    title                   TEXT NOT NULL,
    due_date                TEXT NOT NULL,
    repeat_interval_months  INTEGER CHECK (repeat_interval_months > 0),  -- NULL = one-off
    category                TEXT,                  -- 'smoke detectors', 'HVAC filter', 'tax'...
    related_type            TEXT,
    related_id              INTEGER,
    completed_at            TEXT,
    notes                   TEXT
);
CREATE INDEX ix_reminders_open ON reminders(due_date) WHERE completed_at IS NULL;

-- -----------------------------------------------------------------------------
-- Financing, insurance, mileage
-- -----------------------------------------------------------------------------

CREATE TABLE loans (
    id                       INTEGER PRIMARY KEY,
    property_id              INTEGER NOT NULL REFERENCES properties(id),
    lender                   TEXT NOT NULL,
    loan_number_last4        TEXT,
    original_principal_cents INTEGER NOT NULL CHECK (original_principal_cents > 0),
    annual_interest_rate     TEXT NOT NULL,        -- decimal percent string, e.g. '6.875'; parse with Decimal
    term_months              INTEGER NOT NULL CHECK (term_months > 0),
    first_payment_date       TEXT NOT NULL,
    monthly_pi_cents         INTEGER NOT NULL,     -- principal + interest
    monthly_escrow_cents     INTEGER NOT NULL DEFAULT 0,
    status                   TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paid_off','refinanced')),
    notes                    TEXT
);

-- Source of truth for mortgage interest in the P&L (principal is not an expense).
CREATE TABLE loan_payments (
    id               INTEGER PRIMARY KEY,
    loan_id          INTEGER NOT NULL REFERENCES loans(id),
    payment_date     TEXT NOT NULL,
    principal_cents  INTEGER NOT NULL CHECK (principal_cents >= 0),
    interest_cents   INTEGER NOT NULL CHECK (interest_cents >= 0),
    escrow_cents     INTEGER NOT NULL DEFAULT 0 CHECK (escrow_cents >= 0),
    extra_principal_cents INTEGER NOT NULL DEFAULT 0 CHECK (extra_principal_cents >= 0),
    bank_transaction_id INTEGER REFERENCES bank_transactions(id),
    UNIQUE (loan_id, payment_date)
);

CREATE TABLE insurance_policies (
    id              INTEGER PRIMARY KEY,
    property_id     INTEGER REFERENCES properties(id),  -- NULL = umbrella / portfolio policy
    carrier         TEXT NOT NULL,
    policy_number   TEXT,
    policy_type     TEXT NOT NULL CHECK (policy_type IN ('landlord','umbrella','flood','earthquake','liability','other')),
    coverage_cents  INTEGER,
    premium_cents   INTEGER,
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    agent_name      TEXT,
    agent_phone     TEXT,
    notes           TEXT
);
CREATE INDEX ix_insurance_end ON insurance_policies(end_date);

CREATE TABLE mileage_trips (
    id                   INTEGER PRIMARY KEY,
    trip_date            TEXT NOT NULL,
    property_id          INTEGER REFERENCES properties(id),
    purpose              TEXT NOT NULL,
    miles                REAL NOT NULL CHECK (miles > 0),
    rate_cents_per_mile  INTEGER                   -- standard rate for that tax year
);

-- -----------------------------------------------------------------------------
-- System
-- -----------------------------------------------------------------------------

-- Optional; only needed for password lock or multi-user "office mode".
CREATE TABLE users (
    id             INTEGER PRIMARY KEY,
    username       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash  TEXT NOT NULL,                  -- argon2id
    role           TEXT NOT NULL DEFAULT 'admin' CHECK (role IN ('admin','manager','bookkeeper','read_only')),
    is_active      INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE audit_log (
    id            INTEGER PRIMARY KEY,
    occurred_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    user_id       INTEGER REFERENCES users(id),
    action        TEXT NOT NULL,                   -- insert, update, void, import, backup, restore, login...
    entity_type   TEXT,
    entity_id     INTEGER,
    changes_json  TEXT                             -- {"field": [old, new], ...}
);
CREATE INDEX ix_audit_entity ON audit_log(entity_type, entity_id);

CREATE TABLE settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Global search across properties, tenants, vendors, notes. Maintained by the app.
CREATE VIRTUAL TABLE search_index USING fts5(
    entity_type UNINDEXED,
    entity_id UNINDEXED,
    title,
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);

-- -----------------------------------------------------------------------------
-- Guard rails: financial history is voided, never deleted.
-- -----------------------------------------------------------------------------

CREATE TRIGGER trg_charges_no_delete BEFORE DELETE ON charges
BEGIN SELECT RAISE(ABORT, 'charges cannot be deleted; void them instead'); END;

CREATE TRIGGER trg_payments_no_delete BEFORE DELETE ON payments
BEGIN SELECT RAISE(ABORT, 'payments cannot be deleted; void them instead'); END;

CREATE TRIGGER trg_expenses_no_delete BEFORE DELETE ON expenses
BEGIN SELECT RAISE(ABORT, 'expenses cannot be deleted; void them instead'); END;

CREATE TRIGGER trg_deposit_txn_no_delete BEFORE DELETE ON deposit_transactions
BEGIN SELECT RAISE(ABORT, 'deposit transactions cannot be deleted; void them instead'); END;

-- -----------------------------------------------------------------------------
-- Views used by the dashboard and reports
-- -----------------------------------------------------------------------------

CREATE VIEW v_lease_current_rent AS
SELECT l.id AS lease_id,
       COALESCE((SELECT rc.rent_cents
                   FROM lease_rent_changes rc
                  WHERE rc.lease_id = l.id
                    AND rc.effective_date <= date('now','localtime')
                  ORDER BY rc.effective_date DESC
                  LIMIT 1),
                l.rent_cents) AS current_rent_cents
  FROM leases l;

CREATE VIEW v_lease_balances AS
SELECT l.id AS lease_id,
       COALESCE((SELECT SUM(c.amount_cents) FROM charges c
                  WHERE c.lease_id = l.id AND c.voided_at IS NULL), 0)  AS charged_cents,
       COALESCE((SELECT SUM(p.amount_cents) FROM payments p
                  WHERE p.lease_id = l.id AND p.voided_at IS NULL), 0) AS paid_cents,
       COALESCE((SELECT SUM(c.amount_cents) FROM charges c
                  WHERE c.lease_id = l.id AND c.voided_at IS NULL), 0)
     - COALESCE((SELECT SUM(p.amount_cents) FROM payments p
                  WHERE p.lease_id = l.id AND p.voided_at IS NULL), 0) AS balance_cents
  FROM leases l;

CREATE VIEW v_deposit_held AS
SELECT lease_id,
       SUM(CASE WHEN txn_type IN ('received','interest') THEN amount_cents ELSE -amount_cents END) AS held_cents
  FROM deposit_transactions
 WHERE voided_at IS NULL
 GROUP BY lease_id;

-- One row per rentable unit, occupied or vacant.
CREATE VIEW v_rent_roll AS
SELECT p.id                AS property_id,
       p.code              AS property_code,
       p.name              AS property_name,
       u.id                AS unit_id,
       u.unit_label,
       l.id                AS lease_id,
       l.status            AS lease_status,
       (SELECT group_concat(t.first_name || ' ' || t.last_name, ', ')
          FROM lease_tenants lt JOIN tenants t ON t.id = lt.tenant_id
         WHERE lt.lease_id = l.id AND lt.role IN ('primary','co_tenant')) AS tenants,
       l.start_date,
       l.end_date,
       cr.current_rent_cents,
       u.market_rent_cents,
       COALESCE(dh.held_cents, 0)   AS deposit_held_cents,
       COALESCE(b.balance_cents, 0) AS balance_cents,
       CASE WHEN l.id IS NULL THEN 'vacant' ELSE 'occupied' END AS occupancy
  FROM units u
  JOIN properties p ON p.id = u.property_id
  LEFT JOIN leases l ON l.unit_id = u.id AND l.status IN ('active','month_to_month')
  LEFT JOIN v_lease_current_rent cr ON cr.lease_id = l.id
  LEFT JOIN v_lease_balances b      ON b.lease_id = l.id
  LEFT JOIN v_deposit_held dh       ON dh.lease_id = l.id
 WHERE u.status = 'active' AND p.status = 'active';

CREATE VIEW v_property_occupancy AS
SELECT property_id,
       property_code,
       property_name,
       COUNT(*)                                             AS units,
       SUM(occupancy = 'occupied')                          AS occupied_units,
       ROUND(100.0 * SUM(occupancy = 'occupied') / COUNT(*), 1) AS occupancy_pct,
       SUM(COALESCE(current_rent_cents, 0))                 AS scheduled_rent_cents,
       SUM(CASE WHEN occupancy = 'vacant' THEN COALESCE(market_rent_cents, 0) ELSE 0 END) AS vacancy_loss_cents
  FROM v_rent_roll
 GROUP BY property_id, property_code, property_name;

-- -----------------------------------------------------------------------------
-- Seed data
-- -----------------------------------------------------------------------------

INSERT INTO expense_categories (name, tax_line, is_capital) VALUES
    ('Advertising',                     'Sch E line 5',  0),
    ('Auto and travel',                 'Sch E line 6',  0),
    ('Cleaning and maintenance',        'Sch E line 7',  0),
    ('Commissions',                     'Sch E line 8',  0),
    ('Insurance',                       'Sch E line 9',  0),
    ('Legal and professional fees',     'Sch E line 10', 0),
    ('Management fees',                 'Sch E line 11', 0),
    ('Mortgage interest',               'Sch E line 12', 0),
    ('Other interest',                  'Sch E line 13', 0),
    ('Repairs',                         'Sch E line 14', 0),
    ('Supplies',                        'Sch E line 15', 0),
    ('Property taxes',                  'Sch E line 16', 0),
    ('Utilities',                       'Sch E line 17', 0),
    ('HOA dues',                        'Sch E line 19', 0),
    ('Pest control',                    'Sch E line 19', 0),
    ('Landscaping and snow removal',    'Sch E line 19', 0),
    ('Other',                           'Sch E line 19', 0),
    ('Capital improvement',             NULL,            1),
    ('Appliances (capital)',            NULL,            1);

INSERT INTO settings (key, value) VALUES
    ('currency',                   'USD'),
    ('rent_post_days_before_due',  '0'),         -- post rent this many days before its due date
    ('proration_method',           'actual_days'), -- or 'thirty_day_month'
    ('late_fee_mode',              'review'),    -- 'review' = queue for approval, 'auto' = post immediately
    ('expired_lease_action',       'month_to_month'), -- or 'leave_active'
    ('payment_application_order',  'oldest_first_rent_before_fees'),
    ('late_fee_min_balance_cents', '0'),         -- unpaid rent must exceed this to trigger a fee
    ('deposit_return_days',        '30'),        -- legal deadline to return deposits after move-out
    ('books_locked_through',       ''),          -- 'YYYY-MM-DD'; entries on/before are read-only
    ('backup_keep_daily',          '14'),
    ('backup_keep_weekly',         '8'),
    ('backup_keep_monthly',        '24'),
    ('backup_external_path',       ''),
    ('auto_lock_minutes',          '15'),
    ('receipt_number_prefix',      'R-'),
    ('next_receipt_number',        '1');
