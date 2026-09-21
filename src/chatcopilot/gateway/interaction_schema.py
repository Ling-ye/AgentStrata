"""SQL for Gateway-owned structured input requests (schema v3)."""

INTERACTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS input_requests(
 interaction_id TEXT PRIMARY KEY, kind TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(session_id),
 run_id TEXT NOT NULL REFERENCES runs(run_id), actor_ref TEXT NOT NULL, payload TEXT NOT NULL,
 payload_digest TEXT NOT NULL, policy_revision TEXT NOT NULL, created_at REAL NOT NULL,
 expires_at REAL NOT NULL, state TEXT, decision TEXT, responder TEXT,
 approval_id TEXT UNIQUE REFERENCES approvals(approval_id), generation INTEGER NOT NULL,
 CHECK((kind='approval' AND state IS NULL AND approval_id IS NOT NULL)
    OR (kind IN ('user_input','mcp_elicitation') AND approval_id IS NULL
        AND state IN ('pending','answered','denied','expired','cancelled'))));
CREATE INDEX IF NOT EXISTS input_requests_actor ON input_requests(actor_ref,session_id,created_at);
"""
