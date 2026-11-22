"""Auth service: accounts and tokens. Emits user.registered."""
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from jose import jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field, field_validator
from pydantic_settings import BaseSettings
from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, declarative_base

from lagent_common.bus import EventBus
from lagent_common.db import make_db
from lagent_common.internal import current_user_id, require_internal
from lagent_common.markets import CURRENCIES, MARKETS, currency_for
from lagent_common.service import create_service


class Settings(BaseSettings):
    DATABASE_URL: str
    REDIS_URL: str = "redis://redis:6379/0"
    JWT_SECRET: str
    ACCESS_TOKEN_MINUTES: int = 60
    MAX_FAILED_LOGINS: int = 5
    LOCKOUT_MINUTES: int = 15


settings = Settings()
engine, SessionLocal, get_db = make_db(settings.DATABASE_URL)
bus = EventBus(settings.REDIS_URL, "auth")
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, nullable=False)
    hashed_password = Column(String)
    full_name = Column(String)
    is_active = Column(Boolean, default=True, nullable=False)
    failed_logins = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime)
    country = Column(String(2), default="PK", nullable=False)     # where the seller sells
    currency = Column(String(3), default="PKR", nullable=False)
    shopify_shop = Column(String, unique=True)        # accounts made by installing the Shopify app
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


def _country_ok(v: str | None) -> str | None:
    if v is None:
        return v
    v = v.strip().upper()
    if v not in MARKETS:
        raise ValueError("Pick a country from the list.")
    return v


def _currency_ok(v: str | None) -> str | None:
    if v is None:
        return v
    v = v.strip().upper()
    if v not in CURRENCIES:
        raise ValueError("Pick a currency from the list.")
    return v


class RegisterIn(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None
    country: str | None = None       # defaults to Pakistan
    currency: str | None = None      # defaults to the country's currency

    @field_validator("country")
    @classmethod
    def _c(cls, v):
        return _country_ok(v)

    @field_validator("currency")
    @classmethod
    def _cur(cls, v):
        return _currency_ok(v)

    @field_validator("password")
    @classmethod
    def strong_enough(cls, v: str) -> str:
        if len(v) < 8 or v.isalpha() or v.isdigit():
            raise ValueError("Use at least 8 characters with letters and numbers.")
        return v


class MeIn(BaseModel):
    full_name: str | None = None
    country: str | None = None
    currency: str | None = None      # omitted with a new country = that country's currency

    @field_validator("country")
    @classmethod
    def _c(cls, v):
        return _country_ok(v)

    @field_validator("currency")
    @classmethod
    def _cur(cls, v):
        return _currency_ok(v)


def _user_out(user: "User") -> dict:
    return {"id": str(user.id), "email": user.email, "full_name": user.full_name,
            "country": user.country or "PK", "currency": user.currency or "PKR"}


class LoginIn(BaseModel):
    email: EmailStr
    password: str


def _token(user: User) -> dict:
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_MINUTES)
    token = jwt.encode({"sub": str(user.id), "type": "access", "exp": expires}, settings.JWT_SECRET, "HS256")
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": settings.ACCESS_TOKEN_MINUTES * 60,
        "user": _user_out(user),
    }


router = APIRouter(dependencies=[Depends(require_internal)])


@router.post("/register", status_code=201)
async def register(data: RegisterIn, db: Session = Depends(get_db)):
    country = data.country or "PK"
    user = User(email=data.email.lower(), hashed_password=pwd.hash(data.password), full_name=data.full_name,
                country=country, currency=data.currency or currency_for(country))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists.")
    db.refresh(user)
    await bus.publish("user.registered", {"user_id": str(user.id), "email": user.email, "full_name": user.full_name})
    return _token(user)


@router.post("/login")
def login(data: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == data.email.lower()).first()
    generic = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password.")
    if not user or not user.hashed_password:
        raise generic
    if user.locked_until and user.locked_until > datetime.utcnow():
        raise HTTPException(status.HTTP_423_LOCKED, "Too many attempts — try again in a few minutes.")
    if not pwd.verify(data.password, user.hashed_password):
        user.failed_logins += 1
        if user.failed_logins >= settings.MAX_FAILED_LOGINS:
            user.locked_until = datetime.utcnow() + timedelta(minutes=settings.LOCKOUT_MINUTES)
            user.failed_logins = 0
        db.commit()
        raise generic
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is deactivated.")
    user.failed_logins, user.locked_until = 0, None
    db.commit()
    return _token(user)


# ---------------- Shopify (internal: only the store service calls this) ----------------

class ShopifySessionIn(BaseModel):
    shop: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
    create: bool = True            # False: only look the account up (404 if the shop has none yet)
    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)     # the shop's contact email (notifications only)
    country: str | None = None
    currency: str | None = None


@router.post("/internal/shopify/session")
async def shopify_session(data: ShopifySessionIn, db: Session = Depends(get_db)):
    """The store service has verified a Shopify ID token for `shop`. Returns a normal Listing Agent
    session for the shop's own account (one per shop), creating it on first install."""
    user = db.query(User).filter(User.shopify_shop == data.shop).first()
    if user:
        if not user.is_active:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is deactivated.")
        return {**_token(user), "created": False}
    if not data.create:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No account for this shop yet.")
    country = (data.country or "").upper()
    country = country if country in MARKETS else "US"
    currency = (data.currency or "").upper()
    currency = currency if currency in CURRENCIES else currency_for(country)
    user = User(email=data.shop, hashed_password=None, full_name=(data.name or "").strip() or data.shop.split(".")[0],
                country=country, currency=currency, shopify_shop=data.shop)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:          # opened twice at the same moment: use the one that won
        db.rollback()
        user = db.query(User).filter(User.shopify_shop == data.shop).first()
        if not user:
            raise HTTPException(status.HTTP_409_CONFLICT, "Couldn't create the account — try again.")
        return {**_token(user), "created": False}
    db.refresh(user)
    contact = (data.email or "").strip().lower()
    await bus.publish("user.registered", {"user_id": str(user.id), "email": contact if "@" in contact else "",
                                          "full_name": user.full_name, "shopify_shop": data.shop})
    return {**_token(user), "created": True}


@router.get("/me")
def me(user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found.")
    return _user_out(user)


@router.patch("/me")
def update_me(data: MeIn, user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    """Settings: name, and where the seller sells (country + currency)."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found.")
    fields = data.model_dump(exclude_unset=True)
    if "full_name" in fields:
        user.full_name = (fields["full_name"] or "").strip() or None
    if fields.get("country"):
        user.country = fields["country"]
        user.currency = fields.get("currency") or currency_for(user.country)
    elif fields.get("currency"):
        user.currency = fields["currency"]
    db.commit()
    return _user_out(user)


app = create_service(
    "auth", routers=[router], bus=bus, engine=engine, migrations_dir=Path(__file__).parent.parent / "migrations"
)
