-- Schema for inbody-aggregator.
-- Target: Azure SQL Database `vojtah` on server vhgwcsql1 (shared with the site's web users,
-- schema `vojtah`). Creates this app's schema; the tables are inbody_tables.sql (generated),
-- the grant for the app's managed identity is inbody_grant_web.sql. Re-runnable (IF NOT EXISTS).
-- Provisioning is written from scratch for the TARGET state: no numbered migration chain. A change
-- to a live database is a one-off ALTER script that is applied once and then deleted.

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'inbody')
    EXEC('CREATE SCHEMA inbody');
GO
