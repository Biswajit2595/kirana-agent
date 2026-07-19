-- Supermarket Ops Agent — schema
-- Design rule: every mutation is append-only where it matters (stock_ledger,
-- khata_ledger). Cached columns (products.qty) exist only for fast reads and
-- are always updated inside the same transaction as the ledger insert.

PRAGMA journal_mode = WAL;   -- required for BEGIN IMMEDIATE concurrency pattern
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS owners (
  id INTEGER PRIMARY KEY,
  telegram_chat_id TEXT UNIQUE NOT NULL,
  shop_name TEXT,
  gstin TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS preferences (
  owner_id INTEGER NOT NULL REFERENCES owners(id),
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (owner_id, key)
);

CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY,
  owner_id INTEGER NOT NULL REFERENCES owners(id),
  name TEXT NOT NULL,
  brand TEXT,
  unit TEXT NOT NULL CHECK (unit IN ('kg','g','l','ml','packet','dozen','piece')),
  is_loose BOOLEAN NOT NULL DEFAULT 0,
  hsn_code TEXT,
  gst_rate REAL NOT NULL CHECK (gst_rate IN (0, 5, 12, 18)),
  cost_price REAL NOT NULL CHECK (cost_price >= 0),
  sell_price REAL NOT NULL CHECK (sell_price >= 0),
  qty REAL NOT NULL DEFAULT 0 CHECK (qty >= 0),   -- belt-and-suspenders on top of the WHERE-clause guard
  reorder_level REAL NOT NULL DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS stock_ledger (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id),
  delta_qty REAL NOT NULL,
  reason TEXT NOT NULL CHECK (reason IN ('receive','sale','adjustment')),
  ref_type TEXT,
  ref_id INTEGER,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bills (
  id INTEGER PRIMARY KEY,
  owner_id INTEGER NOT NULL REFERENCES owners(id),
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','finalized','cancelled')),
  customer_name TEXT,
  payment_mode TEXT CHECK (payment_mode IN ('cash','upi','card',NULL)),
  payment_ref TEXT,
  subtotal REAL, cgst_total REAL, sgst_total REAL, grand_total REAL,
  idempotency_key TEXT UNIQUE,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  finalized_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bill_items (
  id INTEGER PRIMARY KEY,
  bill_id INTEGER NOT NULL REFERENCES bills(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  product_name_snapshot TEXT NOT NULL,   -- so a bill still reads correctly if product is later renamed
  qty REAL NOT NULL CHECK (qty > 0),
  unit_price REAL NOT NULL,              -- SNAPSHOT at add-time — never live-joined to products.sell_price
  hsn_code TEXT,
  gst_rate REAL NOT NULL,
  cgst_amt REAL NOT NULL,
  sgst_amt REAL NOT NULL,
  line_total REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS khata_customers (
  id INTEGER PRIMARY KEY,
  owner_id INTEGER NOT NULL REFERENCES owners(id),
  name TEXT NOT NULL,
  phone TEXT,
  UNIQUE(owner_id, name)
);

CREATE TABLE IF NOT EXISTS khata_ledger (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES khata_customers(id),
  type TEXT NOT NULL CHECK (type IN ('credit_sale','payment')),
  amount REAL NOT NULL,   -- credit_sale: positive (increases what they owe); payment: negative
  ref_bill_id INTEGER,
  note TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Generic idempotency store, reused by every mutating tool (§4 of blueprint)
CREATE TABLE IF NOT EXISTS idempotency_log (
  idempotency_key TEXT PRIMARY KEY,
  tool_name TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_products_owner ON products(owner_id);
CREATE INDEX IF NOT EXISTS idx_stock_ledger_product ON stock_ledger(product_id);
CREATE INDEX IF NOT EXISTS idx_bill_items_bill ON bill_items(bill_id);
CREATE INDEX IF NOT EXISTS idx_khata_ledger_customer ON khata_ledger(customer_id);
CREATE INDEX IF NOT EXISTS idx_bills_owner_status ON bills(owner_id, status, finalized_at);
