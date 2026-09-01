# MEXC MultiTrader

Self-hosted MEXC Futures monitoring and risk-control panel. By default it is a safe monitoring application: `LIVE_TRADING=false` and submitted signals are recorded as dry-run operations only.

## Included controls

- Encrypted local storage of multiple API accounts.
- Balance aggregation, account status and group filtering.
- Open-position monitoring through the MEXC Futures positions endpoint.
- Signal dry-run with per-account execution records.
- Duplicate signal protection using `request_id`.
- Global symbol, volume and leverage limits.
- Optional protection against submitting a signal when a target account already has a position on the same symbol.
- Daily realized loss limit in USDT.
- Persistent kill switch that blocks new real entries; it never closes positions automatically.
- Trade execution journal and risk-event journal API.
- Startup self-check and manual UI check for database, Fernet encryption, MEXC connectivity, clock drift, balance access and position access.

## Important safety model

This code does **not** send real orders. `/api/signals` is implemented as dry-run logging even when a signal declares `dry_run=false`. Do not replace that behavior with live order submission until the exact current MEXC endpoint schema has been verified, test accounts have been used, and an independent code review has been completed.

A real future execution path must fail closed when any of the following is true:

- `LIVE_TRADING=false`;
- kill switch is enabled;
- latest self-check failed or is older than `SYSTEM_CHECK_MAX_AGE_SECONDS`;
- daily loss limit is reached;
- a target account failed API checks;
- symbol, leverage, volume or open-position rules reject the request.

## Configure

```bash
cp .env.example .env
chmod 600 .env
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
openssl rand -hex 32
```

Put the generated Fernet key and admin token into `.env`. Keep this file outside Git. Use separate MEXC keys without withdrawal permission and bind every key to the fixed public IP of the server.

## Start

```bash
docker compose up -d --build
docker compose logs -f
curl http://127.0.0.1:8000/health
```

The application starts a background self-check when `RUN_STARTUP_SELF_CHECK=true`. Open the dashboard and click **Проверить систему** after adding or changing an API account.

## Protected API

All operational endpoints require:

```text
X-Admin-Token: value-from-.env
```

Useful endpoints:

```text
GET  /api/balances
GET  /api/positions
GET  /api/risk/status
POST /api/risk/kill-switch/enable
POST /api/risk/kill-switch/disable
GET  /api/system/check
POST /api/system/check
POST /api/signals
GET  /api/trades
GET  /api/risk/events
```

## Dry-run signal example

```bash
curl -X POST http://127.0.0.1:8000/api/signals \
  -H 'Content-Type: application/json' \
  -H "X-Admin-Token: $TOKEN" \
  -d '{
    "request_id": "btc-check-0001",
    "symbol": "BTC_USDT",
    "direction": "LONG",
    "volume": 1,
    "leverage": 2,
    "account_group": "main",
    "dry_run": true
  }'
```

The response reports whether each account would be accepted or blocked because of an existing same-symbol position. It records the attempt in `/api/trades` but does not place an exchange order.

## Daily loss calculation

The application queries MEXC historical positions and totals realized values from the current UTC day. The exact MEXC response schema and fee semantics should be validated against the exchange documentation before treating this number as the sole production safety control. A network/API failure must be treated as a reason to keep real trading disabled.

## Deployment

Keep the compose port bound to `127.0.0.1`. Expose the dashboard via WireGuard or a TLS reverse proxy with an additional access-control layer. Back up both `.env` and `data/mexc_multitrader.db` securely; the database contains encrypted API secrets, and the Fernet key is needed to decrypt them.
