"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.llm import LLMClient
from app.config import Settings


def get_settings_dep(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    sm = cast(async_sessionmaker[AsyncSession], request.app.state.sessionmaker)
    async with sm() as session:
        yield session


def get_llm(request: Request) -> LLMClient:
    return cast(LLMClient, request.app.state.llm)


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
LLMDep = Annotated[LLMClient, Depends(get_llm)]
