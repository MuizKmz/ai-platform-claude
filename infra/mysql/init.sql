-- A MySQL stand-in for a customer's ERP/WMS/MES database.
--
-- Mirrors what infra/postgres/init-analytics.sh builds for Postgres: base
-- tables the connector must NOT reach, curated views it may, and a user
-- granted SELECT on the views and nothing else.
--
-- This exists so `test_write_rejected_at_db_level_too` can be answered for
-- MySQL. That test bypasses the AST validator entirely and hands a write to
-- the connection — a mock cannot answer the question it asks, which is why
-- there is a real database here.

CREATE DATABASE IF NOT EXISTS analytics;
USE analytics;

-- --- base tables: the connector must never reach these ---------------------

CREATE TABLE IF NOT EXISTS customers (
  customer_id   INT PRIMARY KEY,
  customer_name VARCHAR(200) NOT NULL,
  country       VARCHAR(80),
  segment       VARCHAR(40),
  -- Deliberately present. If a curated view ever exposes this, the test that
  -- checks base tables are unreachable is the thing that should have caught it.
  credit_card   VARCHAR(32)
);

CREATE TABLE IF NOT EXISTS orders (
  order_id      INT PRIMARY KEY,
  customer_id   INT NOT NULL,
  order_status  VARCHAR(32) NOT NULL,
  order_date    DATE NOT NULL,
  total_amount  DECIMAL(12,2) NOT NULL
);

CREATE TABLE IF NOT EXISTS order_lines (
  line_id     INT PRIMARY KEY,
  order_id    INT NOT NULL,
  product     VARCHAR(200) NOT NULL,
  quantity    INT NOT NULL,
  line_total  DECIMAL(12,2) NOT NULL
);

INSERT IGNORE INTO customers VALUES
  (1, 'Acme Manufacturing', 'MY', 'enterprise', '4111111111111111'),
  (2, 'Borneo Logistics',   'MY', 'mid',        '4222222222222222'),
  (3, 'Cyberjaya Foods',    'MY', 'small',      '4333333333333333'),
  (4, 'Damansara Retail',   'SG', 'enterprise', '4444444444444444');

INSERT IGNORE INTO orders VALUES
  (1001, 1, 'shipped',   '2026-08-01', 12500.00),
  (1002, 2, 'shipped',   '2026-08-03',  4300.00),
  (1003, 1, 'pending',   '2026-08-10',  8800.00),
  (1004, 3, 'cancelled', '2026-08-11',  1200.00),
  (1005, 4, 'shipped',   '2026-08-14', 22000.00);

INSERT IGNORE INTO order_lines VALUES
  (1, 1001, 'Bearing assembly',  10, 7500.00),
  (2, 1001, 'Drive belt',        20, 5000.00),
  (3, 1002, 'Conveyor roller',    5, 4300.00),
  (4, 1003, 'Motor controller',   2, 8800.00),
  (5, 1005, 'Gearbox',            4, 22000.00);

-- --- curated views: the ONLY thing the connector may read -------------------
--
-- MySQL has no schemas in the Postgres sense — a "schema" IS a database. So
-- the curated layer is a separate DATABASE, and the grant is scoped to it.
-- That is the difference the connector's `schema` config accounts for.

CREATE DATABASE IF NOT EXISTS curated;

CREATE OR REPLACE VIEW curated.v_orders AS
  SELECT o.order_id, o.order_status, o.order_date, o.total_amount,
         c.customer_name, c.country, c.segment
  FROM analytics.orders o
  JOIN analytics.customers c ON c.customer_id = o.customer_id;

CREATE OR REPLACE VIEW curated.v_order_lines AS
  SELECT l.line_id, l.order_id, l.product, l.quantity, l.line_total
  FROM analytics.order_lines l;

CREATE OR REPLACE VIEW curated.v_customers AS
  -- Note what is ABSENT: credit_card. A curated view is where a column stops
  -- being visible, and the base table is where it still lives.
  SELECT c.customer_id, c.customer_name, c.country, c.segment
  FROM analytics.customers c;

-- --- the read-only user -----------------------------------------------------
--
-- THIS is the control. Everything the application does in front of it is a
-- fast, informative filter; the grant is what makes a write impossible.
--
-- MySQL has no `default_transaction_read_only`, so there is no role-level
-- read-only flag to set. SELECT-and-nothing-else is the whole guarantee, plus
-- `SET SESSION TRANSACTION READ ONLY` which the connector issues per
-- connection.

CREATE USER IF NOT EXISTS 'analytics_readonly'@'%' IDENTIFIED BY 'ci-readonly-password';

-- SELECT on the curated views. No INSERT, UPDATE, DELETE, CREATE, DROP, FILE.
GRANT SELECT ON curated.* TO 'analytics_readonly'@'%';

-- A view reads its base tables as the view's DEFINER, so the user needs no
-- rights on `analytics` — and must not have them. Revoking explicitly rather
-- than relying on never having granted it: the test that proves base tables
-- are unreachable is what this line exists for.
--
-- IF EXISTS is required, not decorative. Unlike Postgres, MySQL raises
-- ERROR 1141 when asked to revoke a privilege the user does not hold — which
-- is exactly the case on a fresh database, where the user was created four
-- lines ago and granted nothing on `analytics`. Without it this script fails
-- on any first run, which is every CI run.
REVOKE IF EXISTS ALL PRIVILEGES ON analytics.* FROM 'analytics_readonly'@'%';

FLUSH PRIVILEGES;
