-- Grant the vhgwc-inbody-web container app's managed identity read/write on schema `inbody` and on
-- the site's account tables (schema `vojtah`).
--
-- Run as the Entra admin against db `vojtah`, AFTER the container app exists (its system-assigned
-- managed identity must already be in Entra). Not part of 001 because the principal does not exist
-- at schema-creation time.
--
-- $(WEB_MI_OBJECT_ID) is a sqlcmd-style variable, filled by apply.py (--var) or `sqlcmd -v`:
--     az containerapp show -n vhgwc-inbody-web -g vh-personal --query identity.principalId -o tsv
-- Kept out of the file because it changes whenever the app is recreated.

-- DROP first, never IF NOT EXISTS: MI users pair by SID, not by name. After an app is deleted and
-- recreated the old row survives under the old SID, and an IF NOT EXISTS guard would silently skip
-- the recreate -- leaving the new app with "login failed" (monorepo memory sql-create-user-drop-first).
DROP USER IF EXISTS [vhgwc-inbody-web];
GO

-- WITH OBJECT_ID pins the managed identity even if another Entra principal (e.g. a future EasyAuth
-- app registration) shares the display name -- a bare FROM EXTERNAL PROVIDER then fails with 33131.
CREATE USER [vhgwc-inbody-web] FROM EXTERNAL PROVIDER
    WITH OBJECT_ID = '$(WEB_MI_OBJECT_ID)';
GO

-- Schema-scoped, not db_datareader/db_datawriter: the app owns `inbody` and nothing else.
GRANT SELECT, INSERT, UPDATE, DELETE, EXECUTE ON SCHEMA::inbody TO [vhgwc-inbody-web];
GO

-- The site's ACCOUNTS (schema vojtah, owned by the site; see src/web/db.py): object grants only,
-- nothing else of schema vojtah (its project list stays invisible to this app). Run after the
-- site's vojtah_tables.sql has created them.
GRANT SELECT, INSERT, UPDATE, DELETE ON vojtah.app_user TO [vhgwc-inbody-web];
GRANT SELECT, INSERT, UPDATE, DELETE ON vojtah.user_session TO [vhgwc-inbody-web];
GRANT SELECT, UPDATE ON vojtah.sso_ticket TO [vhgwc-inbody-web];
GO
