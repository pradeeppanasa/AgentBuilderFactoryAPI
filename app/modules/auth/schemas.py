"""Auth request/response schemas + the Role type (CLAUDE.md Phase 3)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, EmailStr

Role = Literal["admin", "developer", "analyst", "auditor"]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int  # seconds, access-token lifetime


class CurrentUser(BaseModel):
    id: str
    email: EmailStr
    role: Role
    tenant_id: str
    is_active: bool
