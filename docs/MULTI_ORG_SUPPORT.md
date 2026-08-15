You can set a default organization with `GRAFANA_ORG_ID`. Grafana MCP sends it as `X-Grafana-Org-Id` on requests to Grafana.

Add to `.env`:

```env
GRAFANA_ORG_ID=1
```

Then add it to the `grafana-mcp` environment in [compose.yaml](/sdx/mcp-codex-example1/compose.yaml):

```yaml
environment:
  GRAFANA_URL: ${GRAFANA_URL:-https://curiousmustard3132.grafana.net/}
  GRAFANA_SERVICE_ACCOUNT_TOKEN: ${GF_SERVICE_ACCOUNT_TOKEN:?Set GF_SERVICE_ACCOUNT_TOKEN in .env}
  GF_SERVICE_ACCOUNT_NAME: ${GF_SERVICE_ACCOUNT_NAME:-grafana-mcp}
  GRAFANA_ORG_ID: ${GRAFANA_ORG_ID:-1}
  MCP_GRAFANA_SERVER_TOKEN: ${GRAFANA_MCP_SERVER_TOKEN:?Set GRAFANA_MCP_SERVER_TOKEN in .env}
```

Recreate the service:

```bash
docker compose --profile grafana up -d --force-recreate grafana-mcp
```

Confirm from the logs:

```bash
docker compose --profile grafana logs --tail=50 grafana-mcp
```

The “No org ID found” warning should disappear.

## Supporting multiple organizations

Grafana MCP allows an incoming `X-Grafana-Org-Id` header to override the environment default. Because Codex and OpenCode MCP headers are configured per server rather than per individual tool call, the cleanest design is to define one MCP alias per organization.

For example:

```text
grafana-production → organization 1
grafana-staging    → organization 2
```

### Codex

In both the template and persistent Codex configuration:

```toml
[mcp_servers.grafana-production]
url = "http://grafana-mcp:8000/mcp"
bearer_token_env_var = "GRAFANA_MCP_SERVER_TOKEN"
http_headers = { "X-Grafana-Org-Id" = "1" }
startup_timeout_sec = 20
tool_timeout_sec = 120

[mcp_servers.grafana-staging]
url = "http://grafana-mcp:8000/mcp"
bearer_token_env_var = "GRAFANA_MCP_SERVER_TOKEN"
http_headers = { "X-Grafana-Org-Id" = "2" }
startup_timeout_sec = 20
tool_timeout_sec = 120
```

### OpenCode

```json
"grafana-production": {
  "type": "remote",
  "url": "http://grafana-mcp:8000/mcp",
  "enabled": true,
  "oauth": false,
  "headers": {
    "Authorization": "Bearer {env:GRAFANA_MCP_SERVER_TOKEN}",
    "X-Grafana-Org-Id": "1"
  },
  "timeout": 120000
},
"grafana-staging": {
  "type": "remote",
  "url": "http://grafana-mcp:8000/mcp",
  "enabled": true,
  "oauth": false,
  "headers": {
    "Authorization": "Bearer {env:GRAFANA_MCP_SERVER_TOKEN}",
    "X-Grafana-Org-Id": "2"
  },
  "timeout": 120000
}
```

Then agents can be explicit:

```text
Use grafana-production to query logs for uvoo.io.
```

## Downsides

- The service-account token must have access to every selected organization. The header does not grant additional permission.
- A broadly privileged cross-organization token increases the impact if it is compromised.
- Every organization alias exposes another copy of the Grafana tool catalog, consuming agent context and making tool selection more ambiguous.
- An agent might query the wrong organization if prompts use generic names such as `grafana`.
- Dashboards and datasource UIDs may differ between organizations even when they have similar names.
- Audit logs all originate from the same service account unless you operate separate identities.
- Organization isolation is weaker when one token can cross every organization.

For stronger isolation, run one Grafana MCP container per organization, each with its own narrowly scoped service account:

```text
grafana-prod-mcp    → prod token → org 1
grafana-staging-mcp → staging token → org 2
```

That is my recommendation for production versus staging or unrelated customers. Use header-based aliases only when the organizations share the same trust boundary and operators.

Grafana documents that `GRAFANA_ORG_ID` supplies the default while the incoming `X-Grafana-Org-Id` header takes precedence. [Grafana multi-organization documentation](https://grafana.com/docs/grafana/latest/developer-resources/mcp/configure/multi-organization-and-headers/).
