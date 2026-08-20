#!/bin/bash
# Creates the demo analytics database the SQL connector queries.
#
# Separate from the platform database on purpose: a connector reaching its own
# platform's tables would make every isolation guarantee elsewhere pointless.
# The role here can read three curated views and nothing else — not the base
# tables, not another schema, not the eaip database.
set -euo pipefail

# Runs in two places: inside the Postgres container at init (where psql connects
# over the local socket) and from CI (where it must reach a service container
# over TCP). PGHOST is set by the caller in the second case and empty in the
# first, and psql treats an empty PGHOST as "use the socket".
PSQL="psql -v ON_ERROR_STOP=1 --username ${POSTGRES_USER}"
if [ -n "${PGHOST:-}" ]; then
  PSQL="${PSQL} --host ${PGHOST} --port ${PGPORT:-5432}"
fi

$PSQL --dbname postgres <<SQL
SELECT 'CREATE DATABASE analytics'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'analytics')
\gexec
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname analytics \
     -v ro_password="$POSTGRES_READONLY_PASSWORD" <<'SQL'

CREATE TABLE IF NOT EXISTS customers (
  id serial PRIMARY KEY, name text NOT NULL, country text NOT NULL,
  segment text NOT NULL, created_at date NOT NULL);
CREATE TABLE IF NOT EXISTS products (
  id serial PRIMARY KEY, sku text UNIQUE NOT NULL, name text NOT NULL,
  category text NOT NULL, unit_price numeric(10,2) NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
  id serial PRIMARY KEY, customer_id int NOT NULL REFERENCES customers(id),
  status text NOT NULL, ordered_at date NOT NULL, shipped_at date);
CREATE TABLE IF NOT EXISTS order_lines (
  id serial PRIMARY KEY, order_id int NOT NULL REFERENCES orders(id),
  product_id int NOT NULL REFERENCES products(id), quantity int NOT NULL,
  unit_price numeric(10,2) NOT NULL);

INSERT INTO customers (name, country, segment, created_at) VALUES
 ('Northwind Ltd','UK','enterprise','2024-03-11'),
 ('Acme Industrial','US','enterprise','2024-06-02'),
 ('Bluefin Systems','DE','midmarket','2025-01-19'),
 ('Corvus Data','UK','midmarket','2025-04-27'),
 ('Delta Robotics','SG','smb','2025-09-08'),
 ('Ember Analytics','US','smb','2026-01-14')
ON CONFLICT DO NOTHING;

INSERT INTO products (sku, name, category, unit_price) VALUES
 ('XR-7742-B','Hydraulic seal kit','parts',284.50),
 ('QN-1183-A','Thermal sensor array','sensors',1290.00),
 ('ZK-9021-C','Control board revision C','electronics',3410.75),
 ('MB-4460-D','Mounting bracket, steel','parts',62.20),
 ('LP-3390-E','Pressure relief valve','parts',845.00)
ON CONFLICT (sku) DO NOTHING;

INSERT INTO orders (customer_id, status, ordered_at, shipped_at) VALUES
 (1,'shipped','2026-05-02','2026-05-05'),(1,'shipped','2026-06-14','2026-06-24'),
 (2,'shipped','2026-06-20','2026-06-22'),(3,'cancelled','2026-07-01',NULL),
 (2,'shipped','2026-07-08','2026-07-19'),(4,'pending','2026-07-30',NULL),
 (5,'shipped','2026-08-01','2026-08-03'),(6,'pending','2026-08-12',NULL)
ON CONFLICT DO NOTHING;

INSERT INTO order_lines (order_id, product_id, quantity, unit_price) VALUES
 (1,1,4,284.50),(1,4,10,62.20),(2,2,2,1290.00),(3,3,1,3410.75),(3,5,3,845.00),
 (4,1,2,284.50),(5,2,5,1290.00),(5,4,20,62.20),(6,5,1,845.00),(7,3,2,3410.75),
 (8,1,1,284.50)
ON CONFLICT DO NOTHING;

-- Curated views. Join paths are decided once here rather than re-derived by a
-- model on every question, and the role is granted rights to these only.
CREATE SCHEMA IF NOT EXISTS curated;

CREATE OR REPLACE VIEW curated.v_orders AS
SELECT o.id AS order_id, o.status AS order_status, o.ordered_at, o.shipped_at,
       c.id AS customer_id, c.name AS customer_name, c.country AS customer_country,
       c.segment AS customer_segment, (o.shipped_at - o.ordered_at) AS days_to_ship
FROM orders o JOIN customers c ON c.id = o.customer_id;

CREATE OR REPLACE VIEW curated.v_order_lines AS
SELECT ol.id AS order_line_id, ol.order_id, o.status AS order_status, o.ordered_at,
       p.sku AS product_sku, p.name AS product_name, p.category AS product_category,
       ol.quantity, ol.unit_price, (ol.quantity * ol.unit_price) AS line_total
FROM order_lines ol JOIN orders o ON o.id = ol.order_id
JOIN products p ON p.id = ol.product_id;

CREATE OR REPLACE VIEW curated.v_customers AS
SELECT id AS customer_id, name AS customer_name, country, segment, created_at
FROM customers;

SELECT format(
  'CREATE ROLE analytics_readonly LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L',
  :'ro_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'analytics_readonly')
\gexec

SELECT format(
  'ALTER ROLE analytics_readonly LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L',
  :'ro_password')
\gexec

-- Layer 2: read-only by default at the session level, so a connection that
-- forgets to ask still starts read-only. MySQL has no equivalent of this.
ALTER ROLE analytics_readonly SET default_transaction_read_only = on;
ALTER ROLE analytics_readonly SET statement_timeout = '5s';

GRANT CONNECT ON DATABASE analytics TO analytics_readonly;
GRANT USAGE ON SCHEMA curated TO analytics_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA curated TO analytics_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA curated GRANT SELECT ON TABLES TO analytics_readonly;

-- Layer 1: and explicitly NOT the base tables. A query naming 'orders' rather
-- than 'curated.v_orders' fails at the database, not at a string check.
REVOKE ALL ON SCHEMA public FROM analytics_readonly;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM analytics_readonly;

SQL

echo "init-analytics.sh: analytics database, curated views, and read-only role ready."
