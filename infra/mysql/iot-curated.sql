-- Curated views and a read-only user for the IoT platform's MariaDB.
--
-- Target: iot_db on the aaPanel host (MariaDB 10.5).
--
-- READ THIS BEFORE RUNNING IT. This script runs against a LIVE production
-- database that also serves other clients. It is designed to be additive only:
--
--   * It CREATES a new database (`iot_curated`) holding views.
--   * It CREATES one new user with SELECT on those views and nothing else.
--   * It does NOT alter, drop, or write to any existing table.
--   * It does NOT modify any existing user.
--
-- A view is a stored SELECT. Creating one reads nothing and changes no data;
-- dropping one later removes the view and leaves the underlying table
-- untouched. That is what makes this safe to run on production.
--
-- To undo everything:
--   DROP DATABASE iot_curated;
--   DROP USER 'eaip_readonly'@'localhost';
--
--
-- WHY A SEPARATE DATABASE RATHER THAN VIEWS INSIDE iot_db
--
-- The read-only user is granted SELECT on `iot_curated.*` — a whole-database
-- grant. If the views lived in `iot_db`, that same grant would have to name
-- each view individually, and a view added later would be exposed or missed
-- depending on whether someone remembered to update the grant.
--
-- A separate database means the boundary is structural: the user holds nothing
-- at all on `iot_db`, so a table added there tomorrow is unreachable by
-- default. Fail closed, not fail open.
--
--
-- WHAT IS DELIBERATELY EXCLUDED, AND WHY
--
-- These tables are NOT exposed, and the omission is the control:
--
--   users, device_users, clients        real people
--   notification_email_recipients       email addresses
--   notification_telegram_recipients    chat identifiers
--   notification_settings, notifications, notification_deliveries
--                                       message content and routing
--   push_tokens, telegram_link_tokens   CREDENTIALS. Never exposed, at all.
--   role_permissions                    the authorization model itself
--   schema_migrations                   internal
--   backup_*, *_backup_20260622         stale snapshots; a question answered
--                                       from one would be quietly wrong
--
-- docs/DATA_POLICY.md is the reason this list matters: retrieved content is
-- sent to a third-party model provider. A column that never enters a view can
-- never be retrieved, so it can never be sent. That is an architectural
-- guarantee rather than a promise of restraint.
--
-- The exposed tables — devices, device_metrics, alert_events, oee_*,
-- production_* — contain no personal data in the columns selected below.


CREATE DATABASE IF NOT EXISTS iot_curated
  DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;


-- --- devices ----------------------------------------------------------------
--
-- `deleted_by` is omitted: it holds a username, which is a person.
--
-- The is_deleted filter is not cosmetic. The table uses soft deletes, so
-- without it the platform would answer questions about equipment that has been
-- removed — and would do so confidently, which is worse than failing.

CREATE OR REPLACE VIEW iot_curated.v_devices AS
SELECT
  d.device_id,
  d.name          AS device_name,
  d.status,
  d.last_seen,
  d.firmware_version,
  d.usage_type,
  d.created_at
FROM iot_db.devices d
WHERE d.is_deleted = 0;


-- --- telemetry --------------------------------------------------------------
--
-- The largest table (~308k rows) and the reason this integration is worth
-- doing. Shape is already ideal for questions: one row per reading, with
-- device, metric name, value, and time.
--
-- Joined to devices rather than exposed raw, for two reasons: it applies the
-- is_deleted filter to metrics too, and it gives the model a device NAME to
-- group by instead of an opaque identifier.

CREATE OR REPLACE VIEW iot_curated.v_device_metrics AS
SELECT
  m.device_id,
  d.name    AS device_name,
  m.metric,
  m.value,
  m.event_time
FROM iot_db.device_metrics m
JOIN iot_db.devices d
  ON d.device_id = m.device_id
 AND d.is_deleted = 0;


-- --- alerts -----------------------------------------------------------------
--
-- `rule_id` is omitted: it is a foreign key into a table not exposed here, so
-- to the model it would be a number with no meaning — an invitation to write a
-- join that gets refused.
--
-- `cleared_at IS NULL` distinguishes an ongoing alert from a resolved one, so
-- both "what is wrong now" and "what went wrong last week" are answerable.

CREATE OR REPLACE VIEW iot_curated.v_alert_events AS
SELECT
  a.device_id,
  d.name    AS device_name,
  a.metric,
  a.operator,
  a.threshold,
  a.value,
  a.level,
  a.started_at,
  a.cleared_at,
  a.active
FROM iot_db.alert_events a
JOIN iot_db.devices d
  ON d.device_id = a.device_id
 AND d.is_deleted = 0;


-- --- OEE configuration ------------------------------------------------------
--
-- The MES-adjacent numbers: planned time, ideal cycle time, target output.
-- These are the inputs an OEE question needs, and they are configuration
-- rather than personal data.

CREATE OR REPLACE VIEW iot_curated.v_oee_config AS
SELECT
  c.device_id,
  d.name    AS device_name,
  c.planned_time_minutes,
  c.planned_downtime_minutes,
  c.ideal_cycle_time_seconds,
  c.target_output,
  c.shift_name,
  c.product_code,
  c.availability_enabled,
  c.performance_enabled,
  c.quality_enabled
FROM iot_db.oee_device_config c
JOIN iot_db.devices d
  ON d.device_id = c.device_id
 AND d.is_deleted = 0;


-- --- production processes ---------------------------------------------------

CREATE OR REPLACE VIEW iot_curated.v_production_processes AS
SELECT
  p.id      AS process_id,
  p.name    AS process_name,
  p.description,
  p.enabled
FROM iot_db.production_processes p;


-- --- the read-only user -----------------------------------------------------
--
-- THIS IS THE CONTROL. Everything the application does in front of it — the
-- AST validator, the table allowlist, the LIMIT injection — is a fast,
-- informative filter. The grant is what makes a write impossible.
--
-- Scoped to 'localhost' because EAIP will run on this same host and reach
-- MariaDB over the loopback interface. A '%' host would make this user
-- reachable from anywhere the server is, which given the current *:3306 bind
-- would mean from the internet.
--
-- CHANGE THE PASSWORD BELOW before running. Then put it in EAIP's .env on the
-- server, never in this file, never in git, and never in a chat message.

CREATE USER IF NOT EXISTS 'eaip_readonly'@'localhost'
  IDENTIFIED BY 'CHANGE-ME-BEFORE-RUNNING';

-- SELECT on the curated views, and nothing else. No INSERT, UPDATE, DELETE,
-- CREATE, DROP, FILE, PROCESS, or SUPER.
GRANT SELECT ON iot_curated.* TO 'eaip_readonly'@'localhost';

-- A view reads its base tables as the view's DEFINER, so this user needs no
-- rights on `iot_db` — and must not have any.
--
-- GRANT USAGE first, then REVOKE: MariaDB raises ERROR 1141 when revoking a
-- privilege the user does not hold, and unlike MySQL 8 it has no
-- `REVOKE IF EXISTS`. Verified against MariaDB 10.5 rather than assumed.
-- USAGE means "no privileges" — it only creates the grant record that REVOKE
-- then needs to find.
GRANT USAGE ON iot_db.* TO 'eaip_readonly'@'localhost';
REVOKE ALL PRIVILEGES ON iot_db.* FROM 'eaip_readonly'@'localhost';

FLUSH PRIVILEGES;
