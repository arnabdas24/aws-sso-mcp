"""Vulture whitelist: names that look unused to static analysis but are live.

Each entry says *why* it is live. Anything new that vulture reports should be
deleted, not added here, unless it has an equally concrete reason.
"""

import sqlite3

from aws_sso_mcp import audit, awscfg, server
from aws_sso_mcp.cache import accounts

# MCP tools: registered by `@app.tool()` and invoked by the client over the
# wire, never called from Python.
server.orgs_list
server.use_org
server.current_org
server.org_status
server.refresh_accounts
server.cache_status
server.list_roles
server.explain_role_selection
server.aws_query
server.config_show
server.config_validate
server.perf_stats

# Serialised through `dataclasses.asdict` into the JSONL audit line.
audit.AuditEntry.timestamp

# Test isolation hook (tests/conftest.py) - clears the cached boto3 clients.
awscfg.reset_client_cache

# Public one-call convenience over `state_is_stale`; exercised by the cache tests.
accounts.AccountCache.is_stale

# sqlite3 reads this attribute when building rows.
sqlite3.Connection.row_factory
