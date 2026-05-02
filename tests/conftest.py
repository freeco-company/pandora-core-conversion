"""Pytest fixtures.

Tests run against an in-memory aiosqlite database. Models are dialect-portable
(JSON columns, UUID stored as CHAR(36)) so the same code runs against MariaDB
in prod / CI and sqlite in unit tests. Tables created via `Base.metadata` so
no alembic dependency in tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.jwt_verifier import JwtVerifier, get_jwt_verifier
from app.config import get_settings
from app.db import Base, get_session
from app.main import app


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        future=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncIterator:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
    async with sessionmaker() as session:
        yield session


# ── RSA keypair for JWT tests ──────────────────────────────────────────


@pytest.fixture(scope="session")
def rsa_keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture
def make_jwt(rsa_keypair):
    private_pem, _ = rsa_keypair
    settings = get_settings()

    def _make(
        sub: str,
        product_code: str = "doudou",
        scopes: list[str] | None = None,
        ttl: int = 300,
        **extra,
    ) -> str:
        now = datetime.now(tz=UTC)
        claims: dict = {
            "iss": settings.pandora_core_issuer,
            "sub": sub,
            "aud": product_code,
            "product_code": product_code,
            "scopes": scopes or [],
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(seconds=ttl),
            "jti": "test-" + sub,
        }
        claims.update(extra)
        return jwt.encode(claims, private_pem, algorithm="RS256")

    return _make


@pytest_asyncio.fixture
async def client(db_engine, rsa_keypair) -> AsyncIterator[httpx.AsyncClient]:
    """FastAPI test client with DB + JWT verifier overrides."""
    _, public_pem = rsa_keypair

    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)

    async def override_get_session():
        async with sessionmaker() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = override_get_session

    # Inject public key without HTTP fetch
    verifier: JwtVerifier = get_jwt_verifier()
    verifier._set_public_key_for_testing(public_pem)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
