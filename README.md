# MEXC MultiTrader

Self-hosted monitoring and risk-control panel for MEXC Futures and Gate.io Futures (USDT-settled) accounts. This is not a trading-signal service by default: `LIVE_TRADING=false` and `/api/signals` only records dry-run intents.

## Included

- Multiple encrypted API accounts across two exchanges: `MEXC` and `GATE`.
- Aggregated USDT balances and open positions per account and per exchange.
- Account deletion, blocked automatically if the account still has open exchange positions.
- Daily realized loss limit, persistent kill switch, and a trade/risk-event journal.
- Startup self-check for SQLite, Fernet encryption, MEXC and Gate public APIs, clock drift and per-account balance/position access.

## Security model

- API secrets are encrypted with Fernet and never displayed after creation.
- `LIVE_TRADING=false` and dry-run signal handling remain the default; no order-submission code path is enabled.
- Never commit `.env`, `FERNET_KEY`, `ADMIN_TOKEN`, or any exchange API key/secret.
- Use exchange API keys without withdrawal permission and restrict them to your server's fixed outbound IP.

## Adding accounts

MEXC keys need **View Account Details** and **View Order Details** permissions. Gate.io Futures keys need read access to `futures/{settle}/accounts` and `futures/{settle}/positions`. Add a static outbound IP to each key's allow-list.

## API

```text
GET    /api/accounts
POST   /api/accounts
PATCH  /api/accounts/{id}/enabled
DELETE /api/accounts/{id}
GET    /api/balances?exchange=MEXC|GATE&account_group=...
GET    /api/positions?exchange=MEXC|GATE&account_group=...
GET    /api/risk/status
POST   /api/risk/kill-switch/enable
POST   /api/risk/kill-switch/disable
GET    /api/system/check
POST   /api/system/check
POST   /api/signals
GET    /api/trades
GET    /api/risk/events
```

`DELETE /api/accounts/{id}` first queries the exchange for open positions; if any exist, deletion is rejected with HTTP 409 so a key cannot be removed while it still controls an open position.

## Notes on the arbitrage roadmap

Cross-exchange spread scanning and dry-run pair construction are being implemented as a follow-up change and are intentionally not included yet: computing a tradable MEXC/Gate spread correctly requires order-book depth, contract-size normalization, fee tiers and funding on both venues, none of which is safe to approximate with placeholder numbers.
