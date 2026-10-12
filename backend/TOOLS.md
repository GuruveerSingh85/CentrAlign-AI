# Tool Framework

## Available Tools

The registry includes procurement policy checks, duplicate-vendor lookup, and vendor registration through the existing SQLite approval workflow. It also includes authorized file list/read/search/create/edit tools, PDF text extraction, text comparison, Gemini-backed text summarization and structured extraction, and deterministic Markdown report generation.

Tool handlers live in focused modules and are added through `register_tool`. Each registered contract includes an input schema, permission scopes, output fields/format, known errors, risk classification, expected consequences, and verification metadata. The central dispatcher validates parameters, resolves server-side scopes, applies risk approval requirements, checks cancellation, and audits task-bound invocations and outcomes before/after calling a handler. Tool writes are confined to authorized roots and require the `files:write` permission; file creation never overwrites, and edits require one unique exact match.

## Configuration

- `AUTHORIZED_FILE_ROOTS`: optional list of directories separated by the operating system path separator. If unset, file tools are confined to `backend/data`.
- `TOOL_PERMISSION_GRANTS`: comma-separated server-side scopes, for example `procurement:read,procurement:write,approval:decide,files:read,documents:process`. If unset, the backward-compatible single-user development scopes include procurement read/write, business read, and approval decisions. Add `files:write` only when file changes are intended.
- `TOOL_ROLE_GRANTS`: optional JSON object mapping role names to scope arrays, for example `{"reader":["procurement:read"],"approver":["procurement:read","procurement:write","approval:decide"]}`.
- `TRUSTED_USER_ROLES`: optional JSON object mapping trusted principal IDs to role names, for example `{"alice":"approver"}`. Configure this together with `TOOL_ROLE_GRANTS` and `TRUSTED_USER_HEADER`; if role mapping is enabled, unknown principals receive no scopes. Only the authenticated proxy may set the trusted identity header.
- `MAX_AUTONOMOUS_RISK`: maximum risk level allowed without human approval (`low`, `moderate`, `high`, or `critical`; default `low`). Irreversible, confidential/restricted-data, material-external-consequence, and contract-designated actions always require approval regardless of this ceiling.
- `APPROVAL_TTL_SECONDS`: lifetime for pending approvals (default 3600 seconds; minimum 60). Expired approvals are recorded as expired and cannot be executed.
- `WEB_ALLOWED_HOSTS`: optional comma-separated exact hostnames. When configured, registers `web.fetch_page`, which permits HTTPS retrieval only from those hosts and validates redirect destinations against the same allowlist. Grant `web:read` to enable it.
- `API_BEARER_TOKEN`: optional backend-only bearer token. When set, all `/api` routes except `/api/health` require it. Keep it on a trusted server or inject it through an authenticated proxy; never put it in frontend code. When unset, the app remains in unauthenticated local-development mode.
- `TRUSTED_USER_HEADER`: optional header name set by an authenticated reverse proxy, such as `X-Authenticated-User`. The proxy must remove client-supplied copies and set this header from its authenticated principal. When configured, API tasks, approvals, memory, and task-history retrieval are scoped to this identity; a missing or invalid identity is rejected. When unset, all requests use the backward-compatible single-user `default` namespace.
- `GEMINI_API_KEY` and `GEMINI_MODEL`: used by the planner and document language-model tools. Model requests have a 30-second request timeout and return structured timeout, rate-limit, and unavailable-service failures.

With no bearer token or trusted identity header configured, the app intentionally remains in backward-compatible single-user local-development mode. `API_BEARER_TOKEN` is a shared service credential, not a user role. Before exposing the app to multiple users, configure an authenticated proxy, a trusted identity header, and role mappings; do not grant `files:write` on an unauthenticated network-facing deployment. Keep `GEMINI_API_KEY` server-side, rotate it if exposed, and never include credentials in objectives, tool parameters, documents sent for model processing, or approval proposals; credential-like input/output is blocked.

## Persistent Memory

Memory uses the configured SQLModel database. `GET/POST /api/memories`, `GET/PATCH/DELETE /api/memories/{id}`, and `GET /api/memories/context?query=...` allow users to inspect and manage preferences, business context, and procedures. Records include provenance, timestamps, verification state, optional expiry, and conflict state. Credential-like values are rejected. Existing task objectives and execution outcomes remain in task history and are retrieved only as a small relevance-ranked subset; the full history is not sent to Gemini. Only workflows with a positive independent verification result create reusable procedure records. Retrieved memory, documents, and task outcomes are presented to the planner as untrusted data, with stale and unverified status preserved.

## Not Configured

No web-search provider or approved web-host allowlist is configured, so provider-backed web search and page retrieval are unavailable by default. Configure `WEB_ALLOWED_HOSTS` and the `web:read` scope to enable allowlisted page retrieval. No third-party API endpoint credentials, email transport/account, or calendar integration is configured, so those capabilities are not registered. The database tools remain the existing controlled procurement operations; arbitrary SQL access is intentionally not exposed.

## Workflow Execution Limits

- `MAX_WORKFLOW_STEPS` limits steps in a validated or replanned workflow (default: 50).
- `MAX_WORKFLOW_REPLANS` limits observation-driven replanning (default: 2).
- `MAX_PARALLEL_STEPS` limits concurrent independent read-only steps (default: 4).
- `WORKFLOW_TIMEOUT_SECONDS` limits total workflow execution time (default: 300 seconds).
- `TOOL_TIMEOUT_SECONDS` supplies the default per-tool timeout when a tool contract does not set one (default: 60 seconds).

Only independent tools without side effects run concurrently. Retries are bounded and restricted to known-safe capabilities with transient failure types; interrupted side-effecting steps are recorded as outcome-unknown and are not automatically repeated.

Persisted `running` and `processing` workflows are rescheduled from their checkpoints during application startup. Listing tasks is read-only and does not trigger recovery.

Sensitive registered actions use the existing `Approval` record and approval inbox. The proposal stores the capability, exact execution parameters, inferred target, expected consequences, risk dimensions, an immutable SHA-256 binding, decision identity, and expiry. A changed proposal, expired/cancelled approval, or reused approval cannot reach the handler. Gemini only plans; approval decisions are accepted through the authenticated human API path. Cancellation stops new dispatches, cancels unclaimed approvals, and preserves the outcome of work already in flight.