import asyncio
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class Settings(BaseSettings):
    app_name: str = "MEXC MultiTrader"
    database_url: str = "sqlite:////data/mexc_multitrader.db"
    fernet_key: str
    admin_token: str
    live_trading: bool = False
    max_leverage: int = 3
    max_order_vol: float = 10.0
    allowed_symbols: str = "BTC_USDT,ETH_USDT,SOL_USDT"
    max_daily_loss_usdt: float = 50.0
    block_if_position_open: bool = True
    allow_dry_run_when_kill_switched: bool = True
    run_startup_self_check: bool = True
    max_clock_drift_ms: int = 5000
    mexc_check_concurrency: int = 5
    system_check_max_age_seconds: int = 300
    mexc_base_url: str = "https://contract.mexc.com"
    gate_base_url: str = "https://api.gateio.ws/api/v4"
    request_timeout_seconds: int = 15

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    @property
    def allowed_symbols_set(self) -> set[str]:
        return {item.strip().upper() for item in self.allowed_symbols.split(",") if item.strip()}


settings = Settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


class TradingAccount(Base):
    __tablename__ = "trading_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    exchange: Mapped[str] = mapped_column(String(16), default="MEXC", index=True)
    account_group: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    api_key: Mapped[str] = mapped_column(String(255))
    api_secret_encrypted: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    max_order_vol: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_leverage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RuntimeSetting(Base):
    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TradeBatch(Base):
    __tablename__ = "trade_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(50), index=True)
    direction: Mapped[str] = mapped_column(String(10))
    volume: Mapped[float] = mapped_column(Float)
    leverage: Mapped[int] = mapped_column(Integer)
    dry_run: Mapped[bool] = mapped_column(Boolean)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TradeExecution(Base):
    __tablename__ = "trade_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(Integer, index=True)
    account_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(20))
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SystemCheckRun(Base):
    __tablename__ = "system_check_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    overall_status: Mapped[str] = mapped_column(String(20), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class SystemCheckItem(Base):
    __tablename__ = "system_check_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(120))
    scope: Mapped[str] = mapped_column(String(160), default="system")
    status: Mapped[str] = mapped_column(String(20), index=True)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AccountCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    exchange: Literal["MEXC", "GATE"] = "MEXC"
    account_group: str | None = Field(default=None, max_length=100)
    api_key: str = Field(min_length=10)
    api_secret: str = Field(min_length=10)
    enabled: bool = True
    max_order_vol: float | None = Field(default=None, gt=0)
    max_leverage: int | None = Field(default=None, ge=1, le=125)


class AccountEnabledUpdate(BaseModel):
    enabled: bool


class SignalIn(BaseModel):
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=8, max_length=80)
    symbol: str = Field(min_length=3, max_length=50)
    direction: Literal["LONG", "SHORT"]
    volume: float = Field(gt=0)
    leverage: int = Field(ge=1, le=125)
    margin_mode: Literal["ISOLATED", "CROSS"] = "ISOLATED"
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    price: float | None = Field(default=None, gt=0)
    account_ids: list[int] | None = None
    account_group: str | None = None
    dry_run: bool = True

    @model_validator(mode="after")
    def validate_input(self):
        self.symbol = self.symbol.upper().replace("-", "_").replace("/", "_")
        if self.order_type == "LIMIT" and self.price is None:
            raise ValueError("price is required for LIMIT orders")
        if self.account_ids and self.account_group:
            raise ValueError("use account_ids or account_group, not both")
        return self


def normalize_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class MexcFuturesClient:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret

    def _headers(self, body: dict[str, Any] | None = None) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        payload = "" if body is None else json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        signature = hmac.new(
            self.api_secret.encode(),
            f"{self.api_key}{timestamp}{payload}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "ApiKey": self.api_key,
            "Request-Time": timestamp,
            "Signature": signature,
            "Recv-Window": "10000",
            "Content-Type": "application/json",
            "Language": "en-US",
        }

    async def _get(self, endpoint: str, params: dict[str, Any] | None = None, private: bool = True) -> dict[str, Any]:
        headers = self._headers() if private else {}
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.get(f"{settings.mexc_base_url}{endpoint}", headers=headers, params=params)
        response.raise_for_status()
        return response.json()

    async def get_assets(self) -> dict[str, Any]:
        return await self._get("/api/v1/private/account/assets")

    async def get_open_positions(self) -> dict[str, Any]:
        return await self._get("/api/v1/private/position/open_positions")

    async def get_history_positions(self, page_num: int = 1, page_size: int = 100) -> dict[str, Any]:
        return await self._get(
            "/api/v1/private/position/list/history_positions",
            params={"pageNum": page_num, "pageSize": page_size},
        )


async def mexc_ping() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        response = await client.get(f"{settings.mexc_base_url}/api/v1/contract/ping")
    response.raise_for_status()
    return response.json()


class GateFuturesClient:
    def __init__(self, api_key: str, api_secret: str, settle: str = "usdt"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.settle = settle

    def _sign(self, method: str, path: str, query: str = "", body: str = "") -> dict[str, str]:
        timestamp = str(int(time.time()))
        body_hash = hashlib.sha512(body.encode()).hexdigest()
        signing_string = "\n".join([method.upper(), f"/api/v4{path}", query, body_hash, timestamp])
        signature = hmac.new(self.api_secret.encode(), signing_string.encode(), hashlib.sha512).hexdigest()
        return {"KEY": self.api_key, "Timestamp": timestamp, "SIGN": signature, "Content-Type": "application/json"}

    async def _get(self, path: str, params: dict[str, Any] | None = None, private: bool = True) -> Any:
        query = ""
        if params:
            query = "&".join(f"{key}={value}" for key, value in params.items())
        headers = self._sign("GET", path, query) if private else {}
        url = f"{settings.gate_base_url}{path}"
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()

    async def get_assets(self) -> Any:
        return await self._get(f"/futures/{self.settle}/accounts")

    async def get_open_positions(self) -> Any:
        return await self._get(f"/futures/{self.settle}/positions")


async def gate_ping() -> Any:
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        response = await client.get(f"{settings.gate_base_url}/futures/usdt/contracts", params={"limit": 1})
    response.raise_for_status()
    return response.json()


def make_client(account: "TradingAccount", secret: str):
    if account.exchange == "GATE":
        return GateFuturesClient(account.api_key, secret)
    return MexcFuturesClient(account.api_key, secret)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_token(x_admin_token: str = Header(default="")):
    if x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Invalid X-Admin-Token")


def serialize_account(row: TradingAccount) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "exchange": row.exchange,
        "account_group": row.account_group,
        "enabled": row.enabled,
        "max_order_vol": row.max_order_vol,
        "max_leverage": row.max_leverage,
        "created_at": row.created_at.isoformat(),
    }


def safe_error(exc: Exception) -> str:
    return str(exc).replace(settings.admin_token, "[redacted]").replace(settings.fernet_key, "[redacted]")[:500]


def runtime_bool(db: Session, key: str, default: bool = False) -> bool:
    item = db.get(RuntimeSetting, key)
    return default if not item else item.value.lower() == "true"


def set_runtime_bool(db: Session, key: str, value: bool) -> None:
    item = db.get(RuntimeSetting, key)
    if item is None:
        db.add(RuntimeSetting(key=key, value=str(value).lower()))
    else:
        item.value = str(value).lower()
        item.updated_at = datetime.utcnow()
    db.commit()


def write_risk_event(db: Session, level: str, event_type: str, message: str, details: dict[str, Any] | None = None) -> None:
    db.add(RiskEvent(level=level, event_type=event_type, message=message, details=details))
    db.commit()


def utc_day_start_ms() -> int:
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def item_time_ms(item: dict[str, Any]) -> int:
    for key in ("updateTime", "closeTime", "createTime"):
        value = item.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def realized_pnl(item: dict[str, Any]) -> float:
    return (
        normalize_float(item.get("realised"))
        + normalize_float(item.get("closeProfitLoss"))
        + normalize_float(item.get("fee"))
        + normalize_float(item.get("totalFee"))
        + normalize_float(item.get("holdFee"))
    )


async def account_daily_pnl(account: TradingAccount) -> dict[str, Any]:
    if account.exchange != "MEXC":
        return {"account_id": account.id, "account_name": account.name, "pnl": 0.0, "status": "SKIPPED"}
    try:
        secret = Fernet(settings.fernet_key.encode()).decrypt(account.api_secret_encrypted.encode()).decode()
        raw = await MexcFuturesClient(account.api_key, secret).get_history_positions()
        data = raw.get("data") or {}
        rows = data.get("resultList") if isinstance(data, dict) else data
        rows = rows or []
        start_ms = utc_day_start_ms()
        pnl = sum(realized_pnl(item) for item in rows if item_time_ms(item) >= start_ms)
        return {"account_id": account.id, "account_name": account.name, "pnl": pnl, "status": "SUCCESS"}
    except Exception as exc:
        return {"account_id": account.id, "account_name": account.name, "pnl": 0.0, "status": "ERROR", "error": safe_error(exc)}


async def daily_loss_status(db: Session) -> dict[str, Any]:
    accounts = list(db.scalars(select(TradingAccount).where(TradingAccount.enabled.is_(True))).all())
    results = await asyncio.gather(*(account_daily_pnl(account) for account in accounts))
    total_pnl = sum(item["pnl"] for item in results)
    loss = max(0.0, -total_pnl)
    limit = settings.max_daily_loss_usdt
    return {
        "limit_usdt": limit,
        "realized_pnl_usdt": round(total_pnl, 8),
        "loss_used_usdt": round(loss, 8),
        "blocked": limit > 0 and loss >= limit,
        "accounts": results,
        "day_start_utc_ms": utc_day_start_ms(),
    }


def normalize_mexc_positions(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    positions = []
    for item in raw:
        volume = normalize_float(item.get("holdVol"))
        if volume <= 0:
            continue
        position_type = item.get("positionType")
        positions.append({
            "symbol": item.get("symbol"),
            "side": "LONG" if position_type == 1 else "SHORT" if position_type == 2 else str(position_type),
            "volume": volume,
            "entry_price": normalize_float(item.get("holdAvgPrice")),
            "leverage": item.get("leverage"),
            "liquidation_price": normalize_float(item.get("liquidatePrice")),
            "unrealized_pnl": normalize_float(item.get("unRealizedPnl")),
        })
    return positions


def normalize_gate_positions(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    positions = []
    for item in raw:
        size = normalize_float(item.get("size"))
        if size == 0:
            continue
        positions.append({
            "symbol": item.get("contract"),
            "side": "LONG" if size > 0 else "SHORT",
            "volume": abs(size),
            "entry_price": normalize_float(item.get("entry_price")),
            "leverage": item.get("leverage"),
            "liquidation_price": normalize_float(item.get("liq_price")),
            "unrealized_pnl": normalize_float(item.get("unrealised_pnl")),
        })
    return positions


async def load_open_positions_for_account(account: TradingAccount) -> dict[str, Any]:
    try:
        secret = Fernet(settings.fernet_key.encode()).decrypt(account.api_secret_encrypted.encode()).decode()
        client = make_client(account, secret)
        response = await client.get_open_positions()
        if account.exchange == "GATE":
            positions = normalize_gate_positions(response if isinstance(response, list) else [])
        else:
            positions = normalize_mexc_positions((response.get("data") or []) if isinstance(response, dict) else [])
        return {"account_id": account.id, "account_name": account.name, "exchange": account.exchange, "account_group": account.account_group, "status": "SUCCESS", "positions": positions}
    except Exception as exc:
        return {"account_id": account.id, "account_name": account.name, "exchange": account.exchange, "account_group": account.account_group, "status": "ERROR", "error": safe_error(exc), "positions": []}


async def load_all_positions(db: Session, account_group: str | None = None, exchange: str | None = None) -> dict[str, Any]:
    query = select(TradingAccount).where(TradingAccount.enabled.is_(True))
    if account_group:
        query = query.where(TradingAccount.account_group == account_group)
    if exchange:
        query = query.where(TradingAccount.exchange == exchange)
    accounts = list(db.scalars(query).all())
    results = await asyncio.gather(*(load_open_positions_for_account(account) for account in accounts))
    return {"generated_at": datetime.utcnow().isoformat() + "Z", "accounts": results}


def get_latest_check(db: Session) -> SystemCheckRun | None:
    return db.scalar(select(SystemCheckRun).order_by(SystemCheckRun.id.desc()))


def serialize_check(db: Session, run: SystemCheckRun | None) -> dict[str, Any]:
    if run is None:
        return {"status": "NOT_RUN", "items": []}
    items = list(db.scalars(select(SystemCheckItem).where(SystemCheckItem.run_id == run.id).order_by(SystemCheckItem.id)).all())
    return {
        "id": run.id,
        "status": run.overall_status,
        "started_at": run.started_at.isoformat() + "Z",
        "completed_at": run.completed_at.isoformat() + "Z" if run.completed_at else None,
        "summary": run.summary or {},
        "items": [{"name": item.name, "scope": item.scope, "status": item.status, "message": item.message, "details": item.details} for item in items],
    }


def add_check_item(db: Session, run_id: int, name: str, status: str, message: str, scope: str = "system", details: dict[str, Any] | None = None) -> None:
    db.add(SystemCheckItem(run_id=run_id, name=name, status=status, message=message, scope=scope, details=details))
    db.commit()


async def run_system_check() -> dict[str, Any]:
    db = SessionLocal()
    try:
        run = SystemCheckRun(overall_status="RUNNING")
        db.add(run)
        db.commit()
        db.refresh(run)
        failures = 0
        warnings = 0

        try:
            Fernet(settings.fernet_key.encode())
            add_check_item(db, run.id, "Fernet encryption", "PASS", "FERNET_KEY is valid")
        except Exception as exc:
            failures += 1
            add_check_item(db, run.id, "Fernet encryption", "FAIL", safe_error(exc))

        try:
            db.execute(select(TradingAccount).limit(1))
            add_check_item(db, run.id, "SQLite database", "PASS", "Database connection is available")
        except Exception as exc:
            failures += 1
            add_check_item(db, run.id, "SQLite database", "FAIL", safe_error(exc))

        try:
            started = time.perf_counter()
            pong = await mexc_ping()
            latency_ms = round((time.perf_counter() - started) * 1000)
            raw_data = pong.get("data") if isinstance(pong, dict) else None
            server_time = 0.0
            if isinstance(raw_data, dict):
                server_time = normalize_float(raw_data.get("serverTime") or raw_data.get("time"))
            elif isinstance(raw_data, (int, float, str)):
                server_time = normalize_float(raw_data)
            if not server_time and isinstance(pong, dict):
                server_time = normalize_float(pong.get("serverTime") or pong.get("time"))
            drift_ms = int(abs(time.time() * 1000 - server_time)) if server_time else None
            add_check_item(db, run.id, "MEXC public API", "PASS", f"MEXC ping succeeded in {latency_ms} ms", details={"latency_ms": latency_ms})
            if drift_ms is None:
                warnings += 1
                add_check_item(db, run.id, "MEXC clock synchronization", "WARN", "MEXC response did not contain a recognizable timestamp")
            elif drift_ms > settings.max_clock_drift_ms:
                failures += 1
                add_check_item(db, run.id, "MEXC clock synchronization", "FAIL", f"Clock drift is {drift_ms} ms; limit is {settings.max_clock_drift_ms} ms", details={"drift_ms": drift_ms})
            else:
                add_check_item(db, run.id, "MEXC clock synchronization", "PASS", f"Clock drift is {drift_ms} ms", details={"drift_ms": drift_ms})
        except Exception as exc:
            failures += 1
            add_check_item(db, run.id, "MEXC public API", "FAIL", safe_error(exc))

        try:
            started = time.perf_counter()
            await gate_ping()
            latency_ms = round((time.perf_counter() - started) * 1000)
            add_check_item(db, run.id, "Gate public API", "PASS", f"Gate ping succeeded in {latency_ms} ms", details={"latency_ms": latency_ms})
        except Exception as exc:
            failures += 1
            add_check_item(db, run.id, "Gate public API", "FAIL", safe_error(exc))

        accounts = list(db.scalars(select(TradingAccount).order_by(TradingAccount.id)).all())
        enabled = [account for account in accounts if account.enabled]
        if not enabled:
            warnings += 1
            add_check_item(db, run.id, "API accounts", "WARN", "No enabled accounts to check")
        else:
            semaphore = asyncio.Semaphore(max(1, settings.mexc_check_concurrency))

            async def check_account(account: TradingAccount) -> list[tuple[str, str, str, str, dict[str, Any] | None]]:
                try:
                    secret = Fernet(settings.fernet_key.encode()).decrypt(account.api_secret_encrypted.encode()).decode()
                    client = make_client(account, secret)
                    async with semaphore:
                        assets = await client.get_assets()
                        positions = await client.get_open_positions()
                    assets_count = len(assets) if isinstance(assets, list) else len((assets or {}).get("data") or [])
                    positions_count = len(positions) if isinstance(positions, list) else len((positions or {}).get("data") or [])
                    return [
                        (f"{account.exchange} balance access", "PASS", "Balance query succeeded", account.name, {"items_received": assets_count}),
                        (f"{account.exchange} positions access", "PASS", "Positions query succeeded", account.name, {"items_received": positions_count}),
                    ]
                except InvalidToken:
                    return [(f"{account.exchange} encryption", "FAIL", "Stored API secret cannot be decrypted with current FERNET_KEY", account.name, None)]
                except Exception as exc:
                    return [(f"{account.exchange} API access", "FAIL", safe_error(exc), account.name, None)]

            account_checks = await asyncio.gather(*(check_account(account) for account in enabled))
            for entries in account_checks:
                for name, status, message, scope, details in entries:
                    if status == "FAIL":
                        failures += 1
                    add_check_item(db, run.id, name, status, message, scope=scope, details=details)

        kill_switch = runtime_bool(db, "kill_switch", False)
        add_check_item(db, run.id, "Kill switch", "WARN" if kill_switch else "PASS", "Kill switch is enabled; real entries are blocked" if kill_switch else "Kill switch is disabled")
        if kill_switch:
            warnings += 1

        overall = "FAIL" if failures else ("WARN" if warnings else "PASS")
        run.overall_status = overall
        run.completed_at = datetime.utcnow()
        run.summary = {"failures": failures, "warnings": warnings, "accounts_total": len(accounts), "accounts_enabled": len(enabled)}
        db.commit()
        return serialize_check(db, run)
    finally:
        db.close()


async def account_balance(account: TradingAccount) -> dict[str, Any]:
    try:
        secret = Fernet(settings.fernet_key.encode()).decrypt(account.api_secret_encrypted.encode()).decode()
        client = make_client(account, secret)
        response = await client.get_assets()

        normalized: list[dict[str, Any]] = []
        if account.exchange == "GATE":
            if isinstance(response, dict):
                normalized = [{
                    "currency": "USDT",
                    "equity": normalize_float(response.get("total")),
                    "available_balance": normalize_float(response.get("available")),
                    "cash_balance": normalize_float(response.get("available")),
                    "position_margin": normalize_float(response.get("position_margin")),
                    "frozen_balance": normalize_float(response.get("order_margin")),
                    "unrealized_pnl": normalize_float(response.get("unrealised_pnl")),
                }]
        else:
            for asset in (response.get("data") or []) if isinstance(response, dict) else []:
                normalized.append({
                    "currency": asset.get("currency"),
                    "equity": normalize_float(asset.get("equity")),
                    "available_balance": normalize_float(asset.get("availableBalance")),
                    "cash_balance": normalize_float(asset.get("cashBalance")),
                    "position_margin": normalize_float(asset.get("positionMargin")),
                    "frozen_balance": normalize_float(asset.get("frozenBalance")),
                    "unrealized_pnl": normalize_float(asset.get("unrealized")),
                })

        return {"account_id": account.id, "account_name": account.name, "exchange": account.exchange, "account_group": account.account_group, "status": "SUCCESS", "assets": normalized}
    except Exception as exc:
        return {"account_id": account.id, "account_name": account.name, "exchange": account.exchange, "account_group": account.account_group, "status": "ERROR", "error": safe_error(exc), "assets": []}


async def balances(db: Session, account_group: str | None = None, exchange: str | None = None) -> dict[str, Any]:
    query = select(TradingAccount).where(TradingAccount.enabled.is_(True))
    if account_group:
        query = query.where(TradingAccount.account_group == account_group)
    if exchange:
        query = query.where(TradingAccount.exchange == exchange)
    accounts = list(db.scalars(query).all())
    results = await asyncio.gather(*(account_balance(account) for account in accounts))
    usdt = [asset for result in results for asset in result["assets"] if asset.get("currency") == "USDT"]
    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "accounts_total": len(accounts),
        "accounts_success": sum(item["status"] == "SUCCESS" for item in results),
        "accounts_failed": sum(item["status"] != "SUCCESS" for item in results),
        "totals_usdt": {
            "equity": round(sum(item["equity"] for item in usdt), 8),
            "available_balance": round(sum(item["available_balance"] for item in usdt), 8),
            "position_margin": round(sum(item["position_margin"] for item in usdt), 8),
            "unrealized_pnl": round(sum(item["unrealized_pnl"] for item in usdt), 8),
        },
        "accounts": results,
    }


def risk_gate(db: Session, dry_run: bool) -> tuple[bool, str | None]:
    if dry_run:
        if runtime_bool(db, "kill_switch", False) and not settings.allow_dry_run_when_kill_switched:
            return False, "Kill switch blocks dry-run by configuration"
        return True, None
    if not settings.live_trading:
        return False, "LIVE_TRADING=false"
    if runtime_bool(db, "kill_switch", False):
        return False, "Kill switch is enabled"
    check = get_latest_check(db)
    if not check or check.overall_status == "FAIL" or not check.completed_at:
        return False, "No successful system self-check"
    age = (datetime.utcnow() - check.completed_at).total_seconds()
    if age > settings.system_check_max_age_seconds:
        return False, "System self-check is stale"
    return True, None


app = FastAPI(title=settings.app_name)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.on_event("startup")
async def init_database():
    Base.metadata.create_all(bind=engine)
    if settings.run_startup_self_check:
        asyncio.create_task(run_system_check())


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "live_trading": settings.live_trading, "allowed_symbols": sorted(settings.allowed_symbols_set)})


@app.get("/health")
def health(db: Session = Depends(get_db)):
    latest = get_latest_check(db)
    return {"status": "ok", "live_trading": settings.live_trading, "self_check": latest.overall_status if latest else "NOT_RUN"}


@app.post("/api/accounts", dependencies=[Depends(require_token)])
def create_account(payload: AccountCreate, db: Session = Depends(get_db)):
    if db.scalar(select(TradingAccount).where(TradingAccount.name == payload.name)):
        raise HTTPException(status_code=409, detail="Account name already exists")
    encrypted_secret = Fernet(settings.fernet_key.encode()).encrypt(payload.api_secret.encode()).decode()
    account = TradingAccount(
        name=payload.name,
        exchange=payload.exchange,
        account_group=payload.account_group,
        api_key=payload.api_key,
        api_secret_encrypted=encrypted_secret,
        enabled=payload.enabled,
        max_order_vol=payload.max_order_vol,
        max_leverage=payload.max_leverage,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return serialize_account(account)


@app.get("/api/accounts", dependencies=[Depends(require_token)])
def list_accounts(db: Session = Depends(get_db)):
    return [serialize_account(row) for row in db.scalars(select(TradingAccount).order_by(TradingAccount.id)).all()]


@app.patch("/api/accounts/{account_id}/enabled", dependencies=[Depends(require_token)])
def set_account_enabled(account_id: int, payload: AccountEnabledUpdate, db: Session = Depends(get_db)):
    account = db.get(TradingAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    account.enabled = payload.enabled
    db.commit()
    db.refresh(account)
    return serialize_account(account)


@app.delete("/api/accounts/{account_id}", dependencies=[Depends(require_token)])
async def delete_account(account_id: int, db: Session = Depends(get_db)):
    account = db.get(TradingAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    position_check = await load_open_positions_for_account(account)
    if position_check["status"] == "SUCCESS" and position_check["positions"]:
        raise HTTPException(
            status_code=409,
            detail="Account has open positions on the exchange; close them before deleting",
        )

    name = account.name
    db.delete(account)
    db.commit()
    write_risk_event(db, "WARN", "ACCOUNT_DELETED", f"Account {name} was deleted", {"account_id": account_id})
    return {"deleted": True, "account_id": account_id}


@app.get("/api/balances", dependencies=[Depends(require_token)])
async def list_balances(account_group: str | None = None, exchange: str | None = None, db: Session = Depends(get_db)):
    return await balances(db, account_group, exchange)


@app.get("/api/positions", dependencies=[Depends(require_token)])
async def list_positions(account_group: str | None = None, exchange: str | None = None, db: Session = Depends(get_db)):
    return await load_all_positions(db, account_group, exchange)


@app.get("/api/risk/status", dependencies=[Depends(require_token)])
async def risk_status(db: Session = Depends(get_db)):
    return {"kill_switch": runtime_bool(db, "kill_switch", False), "daily_loss": await daily_loss_status(db), "live_trading": settings.live_trading}


@app.post("/api/risk/kill-switch/enable", dependencies=[Depends(require_token)])
def enable_kill_switch(db: Session = Depends(get_db)):
    set_runtime_bool(db, "kill_switch", True)
    write_risk_event(db, "CRITICAL", "KILL_SWITCH_ENABLED", "Kill switch enabled; new real entries are blocked")
    return {"kill_switch": True}


@app.post("/api/risk/kill-switch/disable", dependencies=[Depends(require_token)])
def disable_kill_switch(db: Session = Depends(get_db)):
    set_runtime_bool(db, "kill_switch", False)
    write_risk_event(db, "WARN", "KILL_SWITCH_DISABLED", "Kill switch disabled")
    return {"kill_switch": False}


@app.get("/api/system/check", dependencies=[Depends(require_token)])
def system_check_status(db: Session = Depends(get_db)):
    return serialize_check(db, get_latest_check(db))


@app.post("/api/system/check", dependencies=[Depends(require_token)])
async def system_check_now():
    return await run_system_check()


@app.post("/api/signals", dependencies=[Depends(require_token)])
async def submit_signal(payload: SignalIn, db: Session = Depends(get_db)):
    if db.scalar(select(TradeBatch).where(TradeBatch.request_id == payload.request_id)):
        raise HTTPException(status_code=409, detail="request_id has already been processed")
    if payload.symbol not in settings.allowed_symbols_set:
        raise HTTPException(status_code=400, detail="Symbol is not allowed")
    if payload.leverage > settings.max_leverage or payload.volume > settings.max_order_vol:
        raise HTTPException(status_code=400, detail="Global risk limit exceeded")
    allowed, reason = risk_gate(db, payload.dry_run)
    if not allowed:
        write_risk_event(db, "CRITICAL", "SIGNAL_BLOCKED", reason or "Signal blocked", {"request_id": payload.request_id})
        raise HTTPException(status_code=423, detail=reason)
    query = select(TradingAccount).where(TradingAccount.enabled.is_(True))
    if payload.account_ids:
        query = query.where(TradingAccount.id.in_(payload.account_ids))
    elif payload.account_group:
        query = query.where(TradingAccount.account_group == payload.account_group)
    accounts = list(db.scalars(query).all())
    if not accounts:
        raise HTTPException(status_code=404, detail="No enabled target accounts")
    for account in accounts:
        if account.max_leverage and payload.leverage > account.max_leverage:
            raise HTTPException(status_code=400, detail=f"Leverage exceeds limit for {account.name}")
        if account.max_order_vol and payload.volume > account.max_order_vol:
            raise HTTPException(status_code=400, detail=f"Volume exceeds limit for {account.name}")
    daily = await daily_loss_status(db)
    if not payload.dry_run and daily["blocked"]:
        write_risk_event(db, "CRITICAL", "DAILY_LOSS_LIMIT", "Daily loss limit reached", daily)
        raise HTTPException(status_code=423, detail="Daily loss limit reached")
    positions = await load_all_positions(db, payload.account_group)
    position_index = {item["account_id"]: item for item in positions["accounts"]}
    batch = TradeBatch(request_id=payload.request_id, symbol=payload.symbol, direction=payload.direction, volume=payload.volume, leverage=payload.leverage, dry_run=True if not settings.live_trading else payload.dry_run, request_payload=payload.model_dump())
    db.add(batch)
    db.commit()
    db.refresh(batch)
    results = []
    for account in accounts:
        open_same_symbol = [item for item in position_index.get(account.id, {}).get("positions", []) if item["symbol"] == payload.symbol]
        blocked = settings.block_if_position_open and bool(open_same_symbol)
        status = "BLOCKED_POSITION" if blocked else "DRY_RUN"
        error = "Open position already exists for this symbol" if blocked else None
        execution = TradeExecution(batch_id=batch.id, account_id=account.id, status=status, request_payload={"symbol": payload.symbol, "direction": payload.direction, "volume": payload.volume, "leverage": payload.leverage, "margin_mode": payload.margin_mode, "order_type": payload.order_type, "price": payload.price}, error=error)
        db.add(execution)
        results.append({"account_id": account.id, "account_name": account.name, "status": status, "error": error, "existing_positions": open_same_symbol})
    db.commit()
    return {"batch_id": batch.id, "request_id": payload.request_id, "mode": "DRY_RUN", "daily_loss": daily, "results": results}


@app.get("/api/trades", dependencies=[Depends(require_token)])
def list_trades(limit: int = 100, db: Session = Depends(get_db)):
    rows = list(db.scalars(select(TradeExecution).order_by(TradeExecution.id.desc()).limit(max(1, min(limit, 500)))).all())
    accounts = {account.id: account.name for account in db.scalars(select(TradingAccount)).all()}
    return [{"id": row.id, "batch_id": row.batch_id, "account_id": row.account_id, "account_name": accounts.get(row.account_id, f"#{row.account_id}"), "status": row.status, "exchange_order_id": row.exchange_order_id, "error": row.error, "request_payload": row.request_payload, "created_at": row.created_at.isoformat() + "Z"} for row in rows]


@app.get("/api/risk/events", dependencies=[Depends(require_token)])
def list_risk_events(limit: int = 100, db: Session = Depends(get_db)):
    rows = list(db.scalars(select(RiskEvent).order_by(RiskEvent.id.desc()).limit(max(1, min(limit, 500)))).all())
    return [{"id": row.id, "level": row.level, "event_type": row.event_type, "message": row.message, "details": row.details, "created_at": row.created_at.isoformat() + "Z"} for row in rows]
