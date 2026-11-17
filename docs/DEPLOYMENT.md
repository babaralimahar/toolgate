# Deployment

Target: **Render free web service** (Docker, streamable HTTP) + **Neon free Postgres**, like
P1-P4. Desktop clients can instead run ToolGate locally over stdio. No paid services.

> Status: **live** at https://toolgate-a2or.onrender.com (Render free, Singapore; Neon free
> Postgres 17). Set `MCP_PUBLIC_HOSTS` to the public host name: the MCP SDK's DNS-rebinding
> protection otherwise refuses any Host but localhost. `DOWNSTREAM_TIMEOUT_S=60` covers
> downstream services waking from sleep.

## Steps

1. **Push to GitHub** from `D:\AI Projects\toolgate`.
2. **Neon**: create a project, copy the direct connection string.
3. **Migrate and create identities** from your machine:
   ```powershell
   $env:DATABASE_URL="<neon url>"
   uv run alembic upgrade head
   uv run python -m toolgate.cli check-policy
   uv run python -m toolgate.cli create-client analyst-desktop   # repeat per client; store tokens
   uv run python -m app.cli create-key --name ops --role admin   # admin API key
   ```
4. **Render**: New -> Web Service -> Docker. Environment:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | Neon URL |
   | `ENV` | `prod` |
   | `P1_URL`, `P1_API_KEY` | LedgerLens URL + a clerk key |
   | `P2_URL`, `P2_TOKEN` | PolicyPilot URL + a service JWT |
   | `P3_SHOP_URL`, `P3_SHOP_TOKEN` | shop URL + its service token |
   | `P4_URL`, `P4_API_KEY` | QueryWise URL + a viewer key |
   | `OTEL_ENDPOINT` | optional OTLP endpoint |
   | `WORKER_MODE` | `off` |

5. **Verify**: `/readyz` 200; `python scripts/mcp_e2e.py <token> https://<host>/mcp/`;
   `GET /admin/audit/verify` returns `ok: true`.

## Local stdio (desktop MCP clients)

```json
{"mcpServers": {"toolgate": {"command": "uv", "args": ["--directory", "D:\\AI Projects\\toolgate", "run", "python", "-m", "toolgate.cli", "stdio"],
  "env": {"STDIO_CLIENT_TOKEN": "<client token>"}}}}
```

## Production notes

- **Identity**: replace static bearer tokens with OAuth 2.1 (the SDK's `TokenVerifier` is the
  seam: verify the IdP's access token, map its client and scopes). Rotate tokens with
  `rotate-token`.
- **Several replicas**: move rate limits and caches to Redis; the audit chain stays correct
  across replicas because the advisory lock is in Postgres.
- **Anchor the audit head**: export the latest hash daily to write-once storage.
