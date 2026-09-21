"""단일 운영자용 Admin Web과 관리 API."""
from __future__ import annotations

import secrets
import hashlib
import ipaddress
from pathlib import Path
from typing import Any, Literal

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from app.core.credential_crypto import CredentialCipher
from app.core.security import hash_gateway_credential

router=APIRouter(prefix="/custom/admin",include_in_schema=False)
_INDEX=Path(__file__).resolve().parents[2]/"static"/"custom"/"admin"/"index.html"
_ASSET_DIR = _INDEX.parent
_ASSETS = {
    "settings.js": (_ASSET_DIR / "settings.js", "text/javascript"),
    "settings.css": (_ASSET_DIR / "settings.css", "text/css"),
}
_ph=PasswordHasher()

class PasswordBody(BaseModel): password:str=Field(min_length=8,max_length=200)
class SystemBody(BaseModel):
    code: str = Field(min_length=2, max_length=50, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=200)

class AgentBody(BaseModel):
    system_code: str = Field(min_length=2, max_length=50)
    name: str = Field(min_length=1, max_length=200)
    agent_type: Literal["GENIE", "SUPERVISOR"] = "GENIE"
    mode: Literal["CHAT", "AGENT", "BOTH", "SUPERVISOR"] = "CHAT"
    space_id: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{32}$")
    agent_id: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{32}$")
    serving_endpoint_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    long_task_enabled: bool = False
    first_event_timeout_seconds: int = Field(default=90, ge=1, le=600)
    idle_timeout_seconds: int = Field(default=180, ge=1, le=3_600)
    total_timeout_seconds: int = Field(default=1_800, ge=1, le=86_400)
    max_continue_count: int = Field(default=20, ge=0, le=100)
    max_active_run_count: int = Field(default=15, ge=1, le=1_000)

    @model_validator(mode="after")
    def validate_mode_ids(self) -> "AgentBody":
        if self.agent_type == "SUPERVISOR":
            if self.mode != "SUPERVISOR":
                raise ValueError("mode must be SUPERVISOR for a Supervisor Agent")
            if self.serving_endpoint_name is None:
                raise ValueError("serving_endpoint_name is required for Supervisor Agent")
            if self.space_id is not None or self.agent_id is not None:
                raise ValueError("space_id and agent_id are not supported for Supervisor Agent")
            return self
        if self.mode == "SUPERVISOR":
            raise ValueError("SUPERVISOR mode requires agent_type SUPERVISOR")
        if self.serving_endpoint_name is not None:
            raise ValueError("serving_endpoint_name is only supported for Supervisor Agent")
        if self.long_task_enabled:
            raise ValueError("long_task_enabled is only supported for Supervisor Agent")
        if self.mode in {"CHAT", "BOTH"} and self.space_id is None:
            raise ValueError("space_id is required for CHAT or BOTH mode")
        if self.mode in {"AGENT", "BOTH"} and self.agent_id is None:
            raise ValueError("agent_id is required for AGENT or BOTH mode")
        return self

class KeyBody(BaseModel):
    system_code: str = Field(min_length=2, max_length=50)

class SupervisorLimitsBody(BaseModel):
    gateway_max_active_runs: int = Field(default=40, ge=1, le=10_000)
    user_max_active_runs: int = Field(default=10, ge=1, le=1_000)
    user_agent_max_active_runs: int = Field(default=5, ge=1, le=1_000)

    @model_validator(mode="after")
    def validate_hierarchy(self) -> "SupervisorLimitsBody":
        if self.user_agent_max_active_runs > self.user_max_active_runs:
            raise ValueError("user_agent_max_active_runs cannot exceed user_max_active_runs")
        if self.user_max_active_runs > self.gateway_max_active_runs:
            raise ValueError("user_max_active_runs cannot exceed gateway_max_active_runs")
        return self

def _repo(r:Request): return r.app.state.admin_repository
def _source_ip(r: Request) -> str | None:
    value = r.client.host if r.client else None
    try:
        return str(ipaddress.ip_address(value)) if value else None
    except ValueError:
        return None

async def _auth(r:Request)->None:
    token=r.cookies.get("gw_admin_session","")
    if not token or not await _repo(r).valid_session(token): raise HTTPException(401,"Admin login required")

@router.get("")
@router.get("/login")
@router.get("/setup")
async def page(r:Request):
    if not r.app.state.settings.admin_ui_enabled: raise HTTPException(404)
    return FileResponse(_INDEX,headers={"Cache-Control":"no-store"})

@router.get("/assets/{asset_name}")
async def asset(r: Request, asset_name: str):
    if not r.app.state.settings.admin_ui_enabled: raise HTTPException(404)
    item = _ASSETS.get(asset_name)
    if item is None: raise HTTPException(404)
    return FileResponse(item[0], media_type=item[1], headers={"Cache-Control": "no-store"})

@router.get("/api/status")
async def status(r:Request):
    token=r.cookies.get("gw_admin_session","")
    return {"setup_required":not await _repo(r).admin_exists(),"authenticated":bool(token and await _repo(r).valid_session(token))}

@router.post("/api/setup",status_code=201)
async def setup(r:Request,b:PasswordBody):
    if await _repo(r).admin_exists(): raise HTTPException(409,"Admin already configured")
    host=r.client.host if r.client else ""
    if host not in {"127.0.0.1","::1","testclient"}:
        allowed=(r.app.state.settings.admin_bootstrap_email or "").strip().lower()
        forwarded=r.headers.get("x-forwarded-email","").strip().lower()
        if not allowed or not secrets.compare_digest(forwarded,allowed):
            raise HTTPException(403,"Only the configured administrator can initialize admin")
    await _repo(r).create_admin(_ph.hash(b.password)); return {"admin_id":"shnam"}

@router.post("/api/login")
async def login(r:Request,b:PasswordBody,response:Response):
    h=await _repo(r).admin_password_hash()
    try:
        if not h: raise VerifyMismatchError
        _ph.verify(h,b.password)
    except VerifyMismatchError: raise HTTPException(401,"Invalid credentials")
    token=secrets.token_urlsafe(48); await _repo(r).create_session(token,r.app.state.settings.admin_session_ttl_seconds,_source_ip(r))
    response.set_cookie("gw_admin_session",token,httponly=True,secure=r.url.scheme=="https",samesite="strict",max_age=r.app.state.settings.admin_session_ttl_seconds,path="/custom/admin")
    return {"admin_id":"shnam"}

@router.post("/api/logout",status_code=204)
async def logout(r:Request,response:Response):
    token=r.cookies.get("gw_admin_session","")
    if token: await _repo(r).revoke_session(token)
    response.delete_cookie("gw_admin_session",path="/custom/admin")

@router.get("/api/dashboard")
async def dashboard(r:Request): await _auth(r); return await _repo(r).dashboard()

@router.get("/api/settings/supervisor-limits")
async def supervisor_limits(r: Request):
    await _auth(r)
    settings = r.app.state.settings
    values = await _repo(r).supervisor_limits({
        "SUPERVISOR_GATEWAY_MAX_ACTIVE_RUNS": settings.supervisor_gateway_max_active_runs,
        "SUPERVISOR_USER_MAX_ACTIVE_RUNS": settings.supervisor_user_max_active_runs,
        "SUPERVISOR_USER_AGENT_MAX_ACTIVE_RUNS": settings.supervisor_user_agent_max_active_runs,
    })
    return {
        "gateway_max_active_runs": values["SUPERVISOR_GATEWAY_MAX_ACTIVE_RUNS"],
        "user_max_active_runs": values["SUPERVISOR_USER_MAX_ACTIVE_RUNS"],
        "user_agent_max_active_runs": values["SUPERVISOR_USER_AGENT_MAX_ACTIVE_RUNS"],
        "conversation_max_active_runs": 1,
    }

@router.put("/api/settings/supervisor-limits")
async def update_supervisor_limits(r: Request, b: SupervisorLimitsBody):
    await _auth(r)
    await _repo(r).update_supervisor_limits({
        "SUPERVISOR_GATEWAY_MAX_ACTIVE_RUNS": b.gateway_max_active_runs,
        "SUPERVISOR_USER_MAX_ACTIVE_RUNS": b.user_max_active_runs,
        "SUPERVISOR_USER_AGENT_MAX_ACTIVE_RUNS": b.user_agent_max_active_runs,
    })
    await _repo(r).audit("SUPERVISOR_LIMIT_UPDATE", "GATEWAY_CONFIG", "SUPERVISOR", _source_ip(r))
    return {"ok": True}
@router.get("/api/{kind}")
async def rows(r:Request,kind:str):
    await _auth(r)
    if kind == "keys":
        rows=await _repo(r).list_keys(); cipher:CredentialCipher=r.app.state.credential_cipher
        for row in rows:
            encrypted=row.pop("key_enc_val",None); nonce=row.pop("key_nonce_val",None)
            row["api_key"]=cipher.decrypt(encrypted,nonce) if encrypted and nonce else None
        await _repo(r).audit("API_KEY_LIST_REVEAL","API_KEY","ALL",_source_ip(r))
        return {"keys":rows}
    if kind not in {"systems","agents"}: raise HTTPException(404)
    return {kind:await _repo(r).list_rows(kind)}

@router.post("/api/systems",status_code=201)
async def system(r:Request,b:SystemBody):
    await _auth(r); p=b.model_dump(); p["code"]=b.code.upper(); await _repo(r).register_system(p); return {"ok":True}
@router.post("/api/agents",status_code=201)
async def agent(r:Request,b:AgentBody):
    await _auth(r); p=b.model_dump(exclude={"agent_id"}); p["system_code"]=b.system_code.upper(); p["space_id"]=b.space_id.lower() if b.space_id else None; p["dbx_agent_id"]=b.agent_id.lower() if b.agent_id else None
    route_id=p["space_id"] or p["dbx_agent_id"] or p["serving_endpoint_name"]; p["agent_code"]="AGT_"+hashlib.sha256(route_id.encode()).hexdigest()[:12].upper()
    try: await _repo(r).register_agent_for_system(p)
    except ValueError as e: raise HTTPException(404,str(e))
    return {"ok":True}

@router.patch("/api/agents/{agent_id}")
async def edit_agent(r:Request,agent_id:int,b:AgentBody):
    await _auth(r); p=b.model_dump(exclude={"agent_id"}); p["system_code"]=b.system_code.upper(); p["space_id"]=b.space_id.lower() if b.space_id else None; p["dbx_agent_id"]=b.agent_id.lower() if b.agent_id else None
    try: updated=await _repo(r).update_agent(agent_id,p)
    except ValueError as e: raise HTTPException(404,str(e))
    if not updated: raise HTTPException(404,"Agent not found")
    return {"ok":True}

@router.post("/api/keys",status_code=201)
async def issue(r:Request,b:KeyBody):
    await _auth(r); client_id=await _repo(r).client_id_for_system(b.system_code.upper())
    if client_id is None: raise HTTPException(404,"System client not found")
    raw="gw_"+secrets.token_urlsafe(32); cipher:CredentialCipher=r.app.state.credential_cipher
    try: enc,nonce=cipher.encrypt(raw)
    except ValueError as e: raise HTTPException(503,str(e))
    kid=await _repo(r).issue_key(client_id,hash_gateway_credential(raw),enc,nonce,raw[-6:]); await _repo(r).audit("API_KEY_ISSUE","API_KEY",str(kid),_source_ip(r)); return {"key_id":kid,"api_key":raw}

@router.get("/api/keys/{key_id}/plaintext")
async def reveal(r:Request,key_id:int):
    await _auth(r); value=await _repo(r).encrypted_key(key_id)
    if not value: raise HTTPException(404,"Key not found or legacy key is not recoverable")
    await _repo(r).audit("API_KEY_REVEAL","API_KEY",str(key_id),_source_ip(r))
    return {"key_id":key_id,"api_key":r.app.state.credential_cipher.decrypt(*value)}

@router.delete("/api/keys/{key_id}")
async def revoke(r:Request,key_id:int):
    await _auth(r); await _repo(r).revoke_key(key_id); await _repo(r).audit("API_KEY_REVOKE","API_KEY",str(key_id),_source_ip(r)); return {"ok":True}
