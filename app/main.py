import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class Settings(BaseSettings):
    app_name: str = "MEXC MultiTrader"
    database_url: str = "sqlite:////data/mexc_multitrader.db"
    fernet_key: str
    admin_token: str
    live_trading: bool = False
    max_leverage: int = 10
    max_order_vol: float = 1000.0
    allowed_symbols: str = "BTC_USDT,ETH_USDT,SOL_USDT"
    mexc_base_url: str = "https://api.mexc.com"
    request_timeout_seconds: int = 15

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


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
    account_group: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    api_key: Mapped[str] = mapped_column(String(255))
    api_secret_encrypted: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    max_order_vol: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_leverage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AccountCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    account_group: str | None = Field(default=None, max_length=100)
    api_key: str = Field(min_length=10)
    api_secret: str = Field(min_length=10)
    enabled: bool = True
    max_order_vol: float | None = Field(default=None, gt=0)
    max_leverage: int | None = Field(default=None, ge=1, le=125)


class MexcFuturesClient:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret

    def _headers(self, body: dict[str, Any] | None = None) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        payload = "" if body is None else json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        message = f"{self.api_key}{timestamp}{payload}".encode()
        signature = hmac.new(self.api_secret.encode(), message, hashlib.sha256).hexdigest()
        return {
            "ApiKey": self.api_key,
            "Request-Time": timestamp,
            "Signature": signature,
            "Recv-Window": "10000",
            "Content-Type": "application/json",
            "Language": "en-US",
        }

    async def get_assets(self) -> dict[str, Any]:
        endpoint = "/api/v1/private/account/assets"
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
            response = await client.get(
                f"{settings.mexc_base_url}{endpoint}",
                headers=self._headers(),
            )
        response.raise_for_status()
        return response.json()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_token(x_admin_token: str = Header(default="")):
    if x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Invalid X-Admin-Token")


def normalize_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


app = FastAPI(title=settings.app_name)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.on_event("startup")
def init_database():
    Base.metadata.create_all(bind=engine)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "live_trading": settings.live_trading,
        },
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "live_trading": settings.live_trading,
    }


@app.post("/api/accounts", dependencies=[Depends(require_token)])
def create_account(payload: AccountCreate, db: Session = Depends(get_db)):
    exists = db.scalar(select(TradingAccount).where(TradingAccount.name == payload.name))
    if exists:
        raise HTTPException(status_code=409, detail="Account name already exists")

    encrypted_secret = Fernet(settings.fernet_key.encode()).encrypt(payload.api_secret.encode()).decode()
    account = TradingAccount(
        name=payload.name,
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

    return {
        "id": account.id,
        "name": account.name,
        "account_group": account.account_group,
        "enabled": account.enabled,
    }


@app.get("/api/accounts", dependencies=[Depends(require_token)])
def list_accounts(db: Session = Depends(get_db)):
    rows = list(db.scalars(select(TradingAccount).order_by(TradingAccount.id)).all())
    return [
        {
            "id": row.id,
            "name": row.name,
            "account_group": row.account_group,
            "enabled": row.enabled,
            "max_order_vol": row.max_order_vol,
            "max_leverage": row.max_leverage,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


@app.get("/api/balances", dependencies=[Depends(require_token)])
async def list_balances(account_group: str | None = None, db: Session = Depends(get_db)):
    query = select(TradingAccount).where(TradingAccount.enabled.is_(True))
    if account_group:
        query = query.where(TradingAccount.account_group == account_group)

    accounts = list(db.scalars(query).all())

    async def load_one(account: TradingAccount) -> dict[str, Any]:
        try:
            secret = Fernet(settings.fernet_key.encode()).decrypt(account.api_secret_encrypted.encode()).decode()
            response = await MexcFuturesClient(account.api_key, secret).get_assets()
            assets = response.get("data") or []
            normalized = [
                {
                    "currency": asset.get("currency"),
                    "equity": normalize_float(asset.get("equity")),
                    "available_balance": normalize_float(asset.get("availableBalance")),
                    "cash_balance": normalize_float(asset.get("cashBalance")),
                    "position_margin": normalize_float(asset.get("positionMargin")),
                    "frozen_balance": normalize_float(asset.get("frozenBalance")),
                    "unrealized_pnl": normalize_float(asset.get("unrealized")),
                }
                for asset in assets
            ]
            return {
                "account_id": account.id,
                "account_name": account.name,
                "account_group": account.account_group,
                "status": "SUCCESS",
                "assets": normalized,
            }
        except Exception as exc:
            return {
                "account_id": account.id,
                "account_name": account.name,
                "account_group": account.account_group,
                "status": "ERROR",
                "error": str(exc),
                "assets": [],
            }

    results = await asyncio.gather(*(load_one(account) for account in accounts))
    usdt = [asset for result in results for asset in result["assets"] if asset["currency"] == "USDT"]

    return {
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
