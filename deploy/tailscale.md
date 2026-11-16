# Tailscale Serve Setup (Tailnet-Only)

Agent Board binds strictly to `127.0.0.1:8790` and relies on `tailscale serve` (never `tailscale funnel`) so only machines joined to the owner's tailnet (`<tailnet>.ts.net`) can reach it.

## 1. Publish over HTTPS on the host

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8790
tailscale serve status
```

This exposes:
- Web UI: `https://<host>.<tailnet>.ts.net/`
- REST API: `https://<host>.<tailnet>.ts.net/api/v1/`
- MCP (Streamable HTTP): `https://<host>.<tailnet>.ts.net/mcp`

## 2. Verify Remote Agent Tailscale Connector

1. Set `AGENT_BOARD_PUBLIC_URL=https://<host>.<tailnet>.ts.net` in the launchd plist (`deploy/launchd/io.agentboard.server.plist`).
2. Confirm your tailnet ACLs allow the remote agent's Tailscale connector node to reach port `443` on the server host.
3. Verify from the client box:
   ```bash
   curl -sS https://<host>.<tailnet>.ts.net/healthz
   ```
   Expected: `{"status":"ok"}`.
4. Ensure Mac host sleep is disabled:
   ```bash
   sudo pmset -a sleep 0 disksleep 0 standby 0
   ```
