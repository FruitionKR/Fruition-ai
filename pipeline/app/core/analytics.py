"""Allowlisted AI analytics, committed with the product and relayed after commit."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import os
import re
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.core.ai_database import ai_database_url

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 8


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    token: str = field(default="", repr=False)
    environment: str = "local"
    app_version: str = ""
    host: str = "https://us.i.posthog.com"

    @property
    def relay_enabled(self) -> bool:
        return (self.enabled and self.environment in {"local", "staging", "production"}
                and bool(re.fullmatch(r"phc_[A-Za-z0-9_-]+", self.token))
                and _valid_host(self.host, self.environment))

    @classmethod
    def load(cls):
        environment = os.getenv("ANALYTICS_ENVIRONMENT", "local")
        enabled = os.getenv("ANALYTICS_ENABLED", "false").lower()
        if environment not in {"local", "staging", "production"} or enabled not in {"true", "false"}:
            logger.warning("analytics_config_invalid recording_disabled")
            return cls()
        settings = cls(enabled == "true", os.getenv("POSTHOG_PROJECT_TOKEN", "").strip(),
                       environment, os.getenv("APP_VERSION", ""),
                       os.getenv("POSTHOG_HOST", "https://us.i.posthog.com"))
        if settings.enabled and settings.token and not settings.relay_enabled:
            logger.warning("analytics_config_invalid relay_disabled")
        return settings


def _valid_host(host: str, environment: str) -> bool:
    try:
        url = urlsplit(host)
        local_mock = (environment == "local" and url.scheme == "http"
                      and url.hostname in {"localhost", "127.0.0.1", "::1", "host.docker.internal"})
        secure_origin = url.scheme == "https" and url.port in {None, 443}
        # Accessing port also validates malformed/non-numeric port values.
        _ = url.port
        return ((local_mock or secure_origin) and bool(url.hostname) and not url.username
                and not url.password and not url.query and not url.fragment
                and url.path in {"", "/"})
    except ValueError:
        return False


def skill_created(*, skill_id: str, user_id: str, scope: str,
                  workspace_id: str | None, occurred_at: datetime, settings: Settings) -> dict:
    if scope not in {"personal", "team"} or not skill_id or not user_id:
        raise ValueError("Invalid skill analytics identity")
    if scope == "team" and not workspace_id:
        raise ValueError("Team skill requires workspace")
    if occurred_at.tzinfo is None:
        raise ValueError("Occurrence must be timezone aware")
    event_id = str(uuid5(NAMESPACE_URL, f"fruition.analytics.v1:skill_created:{skill_id}"))
    properties = {"event_id": event_id, "schema_version": 1, "event_source": "worker",
                  "environment": settings.environment, "app_version": settings.app_version,
                  "skill_id": skill_id, "scope": scope, "user_id": user_id}
    if scope == "team":
        properties["workspace_id"] = workspace_id
        properties["$groups"] = {"workspace": workspace_id}
    return {"event": "skill_created", "uuid": event_id, "distinct_id": user_id,
            "timestamp": occurred_at.astimezone(timezone.utc).isoformat(), "properties": properties}


def record_skill_created(conn, skill, user_id: str) -> None:
    # Product INSERT has already started the outer transaction. psycopg nests via SAVEPOINT.
    try:
        settings = Settings.load()
        if not settings.enabled:
            return
        with conn.transaction():
            payload = skill_created(skill_id=skill.id, user_id=user_id, scope=skill.scope_type,
                                    workspace_id=skill.workspace_id,
                                    occurred_at=datetime.now(timezone.utc), settings=settings)
            conn.execute("INSERT INTO analytics_outbox (event_id, payload) VALUES (%s, %s) "
                         "ON CONFLICT (event_id) DO NOTHING", (payload["uuid"], Jsonb(payload)))
    except Exception:
        # No exception text: database errors can contain SQL parameters or product data.
        logger.warning("analytics_outbox_insert_failed event=skill_created")


async def relay_once(settings: Settings, client: httpx.AsyncClient) -> bool:
    if not settings.relay_enabled:
        return False
    async with await psycopg.AsyncConnection.connect(ai_database_url(), row_factory=dict_row,
                                                    connect_timeout=5) as conn:
        await conn.execute("SET LOCAL statement_timeout = '5s'")
        row = await (await conn.execute(
            "SELECT event_id, payload, attempts FROM analytics_outbox "
            "WHERE state = 'pending' AND next_attempt_at <= now() "
            "ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED LIMIT 1")).fetchone()
        if row is None:
            return False
        attempts = row["attempts"] + 1
        try:
            response = await client.post(settings.host.rstrip("/") + "/batch/",
                                         json={"api_key": settings.token, "batch": [row["payload"]]},
                                         timeout=5.0)
            response.raise_for_status()
        except (httpx.HTTPError, ValueError):
            state = "dead" if attempts >= MAX_ATTEMPTS else "pending"
            await conn.execute("UPDATE analytics_outbox SET attempts = %s, state = %s, "
                               "next_attempt_at = now() + %s * interval '1 second' WHERE event_id = %s",
                               (attempts, state, min(3600, 2 ** attempts), row["event_id"]))
            logger.warning("analytics_relay_delivery_failed state=%s attempts=%s", state, attempts)
        else:
            await conn.execute("UPDATE analytics_outbox SET state = 'sent', attempts = %s, "
                               "sent_at = now() WHERE event_id = %s", (attempts, row["event_id"]))
        # Crash after HTTP success before commit deliberately retries the immutable envelope.
        return True


async def relay_loop(settings: Settings, client: httpx.AsyncClient) -> None:
    while True:
        try:
            if await relay_once(settings, client):
                continue
        except Exception:
            logger.warning("analytics_relay_database_failed")
        await asyncio.sleep(1)


@asynccontextmanager
async def analytics_lifespan():
    settings = Settings.load()
    if not settings.relay_enabled:
        yield
        return
    async with httpx.AsyncClient() as client:
        task = asyncio.create_task(relay_loop(settings, client), name="analytics-relay")
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
