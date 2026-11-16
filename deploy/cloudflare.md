# Optional Cloudflare Tunnel & Access Exposure

Use this **only** if a future client cannot join the owner's tailnet. By default, Agent Board stays tailnet-only.

## Architecture
- `cloudflared` runs on the host pointing to `http://127.0.0.1:8790`.
- Protect `/api/v1/*` and `/mcp` with a **Cloudflare Access Service Auth** policy (`CF-Access-Client-Id` / `CF-Access-Client-Secret`) in addition to Agent Board's `Authorization: Bearer ab_...` token.
- Protect `/*` (Web UI) with a **Cloudflare Access Email OTP / IdP** policy allowing only the owner's email.

## Quick Tunnel Ingress Example (`config.yml`)

```yaml
tunnel: <TUNNEL_UUID>
credentials-file: /Users/<username>/.cloudflared/<TUNNEL_UUID>.json

ingress:
  - hostname: board.example.com
    service: http://127.0.0.1:8790
  - service: http_status:404
```
