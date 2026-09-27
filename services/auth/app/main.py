"""Auth service: accounts and tokens. Emits user.registered."""
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from jose import jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, field_validator
from pydantic_settings import BaseSettings
from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, declarative_base

from lagent_common.bus import EventBus
from lagent_common.db import make_db
from lagent_common.internal import current_user_id, require_internal
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
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class RegisterIn(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None

    @field_validator("password")
    @classmethod
    def strong_enough(cls, v: str) -> str:
        if len(v) < 8 or v.isalpha() or v.isdigit():
            raise ValueError("Use at least 8 characters with letters and numbers.")
        return v


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
        "user": {"id": str(user.id), "email": user.email, "full_name": user.full_name},
    }


router = APIRouter(dependencies=[Depends(require_internal)])


@router.post("/register", status_code=201)
async def register(data: RegisterIn, db: Session = Depends(get_db)):
    user = User(email=data.email.lower(), hashed_password=pwd.hash(data.password), full_name=data.full_name)
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


@router.get("/me")
def me(user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found.")
    return {"id": str(user.id), "email": user.email, "full_name": user.full_name}


app = create_service(
    "auth", routers=[router], bus=bus, engine=engine, migrations_dir=Path(__file__).parent.parent / "migrations"
)
