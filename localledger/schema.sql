-- =============================================================================
-- LocalLedger OCR — SQLite schema (schema version 3, tracked via PRAGMA user_version)
-- Fresh databases are created from this file; older ones are upgraded by the
-- migrations in db.py (v2 added fx_rates, v3 added staging_rows).
--
-- Conventions
--   * Money is stored as INTEGER minor units (pence / cents) to avoid float drift.
--   * Amounts are stored POSITIVE. The sign is implied by doc_type:
--       PI / SI = invoices (+),  PC / SC = credit notes (-).
--   * exchange_rate = units of document currency per 1 unit of base currency
--     (the Sage 50 and ECB/Frankfurter convention: GBP->USD 1.27 means 1 GBP = 1.27 USD).
--     base_amount = amount / exchange_rate.
--   * Dates are ISO-8601 TEXT (YYYY-MM-DD); timestamps are UTC ISO-8601.
--   * Account codes are compared case-insensitively (COLLATE NOCASE).
-- =============================================================================

PRAGMA foreign_keys = ON;

-- -----------------------------------------------------------------------------
-- settings: key/value store, values are JSON-encoded
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- -----------------------------------------------------------------------------
-- vat_codes: tax codes and their rates (seeded with Sage 50 UK defaults)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vat_codes (
    code               TEXT PRIMARY KEY COLLATE NOCASE,
    description        TEXT NOT NULL,
    rate               REAL NOT NULL CHECK (rate >= 0 AND rate <= 100),   -- percent
    -- Reverse-charge codes carry a nominal rate (e.g. 20%) but the supplier's
    -- document shows 0 VAT, so the validator expects 0 VAT on the invoice.
    is_reverse_charge  INTEGER NOT NULL DEFAULT 0 CHECK (is_reverse_charge IN (0, 1)),
    is_active          INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    sort_order         INTEGER NOT NULL DEFAULT 0
);

-- -----------------------------------------------------------------------------
-- entities: Client / Supplier master list (+ learned "vendor memory" defaults)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entities (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type           TEXT NOT NULL CHECK (entity_type IN ('Client', 'Supplier')),
    company_name          TEXT NOT NULL,
    normalized_name       TEXT NOT NULL,                 -- for fuzzy matching
    account_code          TEXT NOT NULL COLLATE NOCASE,
    vat_reg_no            TEXT,                          -- normalised: upper-case alphanumeric, GB prefix removed
    default_nominal_code  TEXT,
    default_vat_code      TEXT,
    default_currency      TEXT,
    source                TEXT NOT NULL DEFAULT 'import' CHECK (source IN ('import', 'manual', 'learned')),
    is_active             INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (entity_type, account_code)
);
CREATE INDEX IF NOT EXISTS idx_entities_vat  ON entities (vat_reg_no) WHERE vat_reg_no IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_entities_norm ON entities (normalized_name);

-- Alternative names learned from documents ("ACME UK Ltd" -> ACME001).
CREATE TABLE IF NOT EXISTS entity_aliases (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id         INTEGER NOT NULL REFERENCES entities (id) ON DELETE CASCADE,
    alias             TEXT NOT NULL,
    normalized_alias  TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (entity_id, normalized_alias)
);
CREATE INDEX IF NOT EXISTS idx_alias_norm ON entity_aliases (normalized_alias);

-- -----------------------------------------------------------------------------
-- templates: export templates (built-in presets + user templates)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS templates (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL UNIQUE COLLATE NOCASE,
    software         TEXT NOT NULL DEFAULT 'Custom',
    is_builtin       INTEGER NOT NULL DEFAULT 0 CHECK (is_builtin IN (0, 1)),
    builtin_version  INTEGER NOT NULL DEFAULT 0,
    config           TEXT NOT NULL,                      -- JSON, see rules.TemplateConfig
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- -----------------------------------------------------------------------------
-- invoice_logs: one row per exported document (header)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS invoice_logs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id              TEXT NOT NULL,
    group_id              TEXT NOT NULL,                 -- staging group id (links split lines)
    doc_type              TEXT NOT NULL CHECK (doc_type IN ('PI', 'PC', 'SI', 'SC')),
    entity_id             INTEGER REFERENCES entities (id) ON DELETE SET NULL,
    account_code          TEXT NOT NULL COLLATE NOCASE,
    company_name          TEXT,
    invoice_number        TEXT,
    invoice_number_norm   TEXT,                          -- upper-case alphanumeric only
    invoice_date          TEXT NOT NULL,
    service_period_start  TEXT,
    service_period_end    TEXT,
    service_month         TEXT NOT NULL,                 -- 'YYYY-MM' accrual month (falls back to invoice month)
    currency              TEXT NOT NULL DEFAULT 'GBP',
    exchange_rate         TEXT,                          -- decimal as text
    net_minor             INTEGER NOT NULL,
    vat_minor             INTEGER NOT NULL,
    gross_minor           INTEGER NOT NULL,
    base_net_minor        INTEGER NOT NULL,
    base_vat_minor        INTEGER NOT NULL,
    base_gross_minor      INTEGER NOT NULL,
    source_file_name      TEXT,
    file_hash             TEXT,                          -- SHA-256 of the source PDF
    archived_path         TEXT,
    extraction_method     TEXT,
    confidence            INTEGER,
    template_id           INTEGER REFERENCES templates (id) ON DELETE SET NULL,
    status                TEXT NOT NULL DEFAULT 'exported' CHECK (status IN ('exported', 'void')),
    void_reason           TEXT,
    exported_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_logs_hash        ON invoice_logs (file_hash) WHERE file_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_logs_acct_invno  ON invoice_logs (account_code, invoice_number_norm);
CREATE INDEX IF NOT EXISTS idx_logs_acct_gross  ON invoice_logs (account_code, gross_minor, invoice_date);
CREATE INDEX IF NOT EXISTS idx_logs_date        ON invoice_logs (invoice_date);
CREATE INDEX IF NOT EXISTS idx_logs_service     ON invoice_logs (service_month);
CREATE INDEX IF NOT EXISTS idx_logs_batch       ON invoice_logs (batch_id);

-- -----------------------------------------------------------------------------
-- invoice_lines: posting lines of each document (1 line, or N after a split)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS invoice_lines (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id            INTEGER NOT NULL REFERENCES invoice_logs (id) ON DELETE CASCADE,
    line_no           INTEGER NOT NULL,
    nominal_code      TEXT,
    description       TEXT,
    vat_code          TEXT,
    net_minor         INTEGER NOT NULL,
    vat_minor         INTEGER NOT NULL,
    gross_minor       INTEGER NOT NULL,
    base_net_minor    INTEGER NOT NULL,
    base_vat_minor    INTEGER NOT NULL,
    base_gross_minor  INTEGER NOT NULL,
    UNIQUE (log_id, line_no)
);
CREATE INDEX IF NOT EXISTS idx_lines_nominal ON invoice_lines (nominal_code);
CREATE INDEX IF NOT EXISTS idx_lines_vat     ON invoice_lines (vat_code);

-- -----------------------------------------------------------------------------
-- fx_rates: exchange-rate cache (API) and user-entered manual rates (v2)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fx_rates (
    base_currency   TEXT NOT NULL,
    currency        TEXT NOT NULL,
    rate_date       TEXT NOT NULL,                       -- date requested (the invoice date)
    effective_date  TEXT NOT NULL,                       -- date the published rate is for (prior business day at weekends)
    rate            TEXT NOT NULL,                       -- units of currency per 1 base unit, decimal as text
    source          TEXT NOT NULL CHECK (source IN ('api', 'manual')),
    fetched_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (base_currency, currency, rate_date, source)
);
CREATE INDEX IF NOT EXISTS idx_fx_effective ON fx_rates (base_currency, currency, effective_date);

-- -----------------------------------------------------------------------------
-- staging_rows: the current (not yet exported) batch, so work survives a refresh (v3)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staging_rows (
    row_id      TEXT PRIMARY KEY,
    group_id    TEXT NOT NULL,
    position    INTEGER NOT NULL,
    data        TEXT NOT NULL,                           -- JSON of models.StagingRow.to_dict()
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_staging_position ON staging_rows (position);

-- -----------------------------------------------------------------------------
-- v_ledger_lines: signed, base-currency view used by the analytics dashboard
-- -----------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_ledger_lines AS
SELECT
    l.id                                                        AS line_id,
    h.id                                                        AS log_id,
    h.batch_id,
    h.doc_type,
    CASE WHEN h.doc_type IN ('SI', 'SC') THEN 'Sales' ELSE 'Purchase' END AS ledger,
    CASE WHEN h.doc_type IN ('PC', 'SC') THEN -1 ELSE 1 END     AS sign,
    h.entity_id,
    h.account_code,
    h.company_name,
    h.invoice_number,
    h.invoice_date,
    substr(h.invoice_date, 1, 7)                                AS invoice_month,
    h.service_month,
    h.currency,
    l.line_no,
    l.nominal_code,
    l.vat_code,
    l.description,
    (CASE WHEN h.doc_type IN ('PC', 'SC') THEN -1 ELSE 1 END) * l.base_net_minor   / 100.0 AS net,
    (CASE WHEN h.doc_type IN ('PC', 'SC') THEN -1 ELSE 1 END) * l.base_vat_minor   / 100.0 AS vat,
    (CASE WHEN h.doc_type IN ('PC', 'SC') THEN -1 ELSE 1 END) * l.base_gross_minor / 100.0 AS gross
FROM invoice_lines AS l
JOIN invoice_logs  AS h ON h.id = l.log_id
WHERE h.status = 'exported';
