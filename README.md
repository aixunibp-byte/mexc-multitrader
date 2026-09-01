# MEXC MultiTrader

Self-hosted control panel for operating MEXC Futures trading across several API accounts.

## Included now

- Multiple API accounts stored in SQLite.
- Encryption of API secrets at rest using a Fernet key kept outside Git.
- Per-account group, enabled state and risk-limit fields.
- Aggregated Futures balances from `GET /api/v1/private/account/assets`.
- FastAPI REST API and a minimal web UI.
- `LIVE_TRADING=false` by default. This starter version does not submit orders.

## Security

- Never commit `.env`, API keys, API secrets, Fernet keys or admin tokens.
- Use API keys without withdrawal permission.
- Restrict MEXC API keys to the server's fixed outbound IP.
- Put the panel behind a VPN or HTTPS reverse proxy; its Docker port binds to `127.0.0.1` by default.
- Start with read-only API keys to validate balance retrieval.

## Deploy

```bash
cp .env.example .env
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Put the generated value into FERNET_KEY and set a long random ADMIN_TOKEN in .env
docker compose up -d --build
curl http://127.0.0.1:8000/health
```

## Create an account

```bash
export TOKEN='your-admin-token'

curl -X POST http://127.0.0.1:8000/api/accounts \
  -H 'Content-Type: application/json' \
  -H "X-Admin-Token: $TOKEN" \
  -d '{
    "name": "mexc-main-01",
    "account_group": "main",
    "api_key": "MEXC_API_KEY",
    "api_secret": "MEXC_API_SECRET",
    "enabled": true,
    "max_order_vol": 10,
    "max_leverage": 3
  }'
```

## Request balances

All enabled accounts:

```bash
curl http://127.0.0.1:8000/api/balances \
  -H "X-Admin-Token: $TOKEN"
```

An account group:

```bash
curl 'http://127.0.0.1:8000/api/balances?account_group=main' \
  -H "X-Admin-Token: $TOKEN"
```

## Notes

The implementation signs MEXC private GET requests with HMAC-SHA256 over `apiKey + timestamp + requestBody`, using an empty request body for GET. Before enabling any future order submission, verify endpoint names and the exact request schema against the current official MEXC Futures API documentation.
