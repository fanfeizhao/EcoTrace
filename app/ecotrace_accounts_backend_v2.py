from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/account", tags=["account"])

DB_PATH = Path(os.getenv("ECOTRACE_ACCOUNT_DB", "ecotrace_accounts.sqlite3"))
PBKDF2_ROUNDS = 240_000


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accounts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            full_name TEXT NOT NULL,
            nickname TEXT,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL,
            organization TEXT,
            education_work TEXT,
            bio TEXT,
            share_history INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions(
            token_hash TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS validation_reports(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter_account_id INTEGER,
            display_name TEXT,
            report_type TEXT NOT NULL,
            location TEXT,
            note TEXT NOT NULL,
            evidence_url TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            reviewer_note TEXT,
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            FOREIGN KEY(reporter_account_id) REFERENCES accounts(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS validation_checks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reviewer_account_id INTEGER NOT NULL,
            check_type TEXT NOT NULL,
            result TEXT NOT NULL,
            location TEXT,
            note TEXT,
            source_report_id INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(reviewer_account_id) REFERENCES accounts(id),
            FOREIGN KEY(source_report_id) REFERENCES validation_reports(id)
        )
    """)
    conn.commit()
    return conn


def _hash_password(password: str, salt_hex: str) -> str:
    salt = bytes.fromhex(salt_hex)
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS
    ).hex()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _trusted_reviewer(username: str) -> bool:
    allowed = {
        value.strip().lower()
        for value in os.getenv("ECOTRACE_REVIEWER_USERS", "").split(",")
        if value.strip()
    }
    return username.strip().lower() in allowed


def _public(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "full_name": row["full_name"],
        "nickname": row["nickname"],
        "display_name": row["display_name"],
        "role": row["role"],
        "organization": row["organization"],
        "education_work": row["education_work"],
        "bio": row["bio"],
        "share_history": bool(row["share_history"]),
        "trusted_reviewer": _trusted_reviewer(row["username"]),
    }


class Signup(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=80)
    nickname: str | None = Field(default=None, max_length=50)
    display_preference: str = Field(default="full_name", max_length=20)
    role: str = Field(default="student", max_length=40)
    organization: str | None = Field(default=None, max_length=120)
    education_work: str | None = Field(default=None, max_length=220)
    bio: str | None = Field(default=None, max_length=500)
    share_history: bool = False


class Signin(BaseModel):
    username: str
    password: str


class ProfileUpdate(BaseModel):
    full_name: str | None = Field(default=None, max_length=80)
    nickname: str | None = Field(default=None, max_length=50)
    display_name: str | None = Field(default=None, max_length=80)
    role: str | None = Field(default=None, max_length=40)
    organization: str | None = Field(default=None, max_length=120)
    education_work: str | None = Field(default=None, max_length=220)
    bio: str | None = Field(default=None, max_length=500)
    share_history: bool | None = None


def _auth(authorization: str | None) -> sqlite3.Row:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Sign in required.")
    token = authorization.split(" ", 1)[1].strip()
    with _db() as conn:
        row = conn.execute("""
            SELECT a.* FROM sessions s
            JOIN accounts a ON a.id=s.account_id
            WHERE s.token_hash=?
        """, (_token_hash(token),)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Session expired or invalid.")
    return row


@router.post("/signup")
async def signup(payload: Signup) -> dict[str, Any]:
    username = payload.username.strip().lower()
    full_name = payload.full_name.strip()
    nickname = (payload.nickname or "").strip() or None
    display_name = nickname if payload.display_preference == "nickname" and nickname else full_name

    salt = secrets.token_hex(16)
    password_hash = _hash_password(payload.password, salt)
    now = datetime.now(timezone.utc).isoformat()

    try:
        with _db() as conn:
            cur = conn.execute("""
                INSERT INTO accounts(
                    username,password_hash,salt,full_name,nickname,display_name,
                    role,organization,education_work,bio,share_history,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                username, password_hash, salt, full_name, nickname, display_name,
                payload.role, payload.organization, payload.education_work,
                payload.bio, int(payload.share_history), now
            ))
            account_id = cur.lastrowid
            token = secrets.token_urlsafe(32)
            conn.execute(
                "INSERT INTO sessions(token_hash,account_id,created_at) VALUES(?,?,?)",
                (_token_hash(token), account_id, now)
            )
            conn.commit()
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="That username is already in use.") from exc

    return {"token": token, "profile": _public(row)}


@router.post("/signin")
async def signin(payload: Signin) -> dict[str, Any]:
    username = payload.username.strip().lower()
    with _db() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="Incorrect username or password.")
        candidate = _hash_password(payload.password, row["salt"])
        if not hmac.compare_digest(candidate, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Incorrect username or password.")
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO sessions(token_hash,account_id,created_at) VALUES(?,?,?)",
            (_token_hash(token), row["id"], datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
    return {"token": token, "profile": _public(row)}


@router.get("/me")
async def me(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return _public(_auth(authorization))


@router.patch("/me")
async def update_me(
    payload: ProfileUpdate,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    row = _auth(authorization)
    values = payload.model_dump(exclude_none=True)
    allowed = {
        "full_name","nickname","display_name","role","organization",
        "education_work","bio","share_history"
    }
    values = {k:v for k,v in values.items() if k in allowed}
    if "share_history" in values:
        values["share_history"] = int(bool(values["share_history"]))
    if values:
        cols = ", ".join(f"{k}=?" for k in values)
        params = list(values.values()) + [row["id"]]
        with _db() as conn:
            conn.execute(f"UPDATE accounts SET {cols} WHERE id=?", params)
            conn.commit()
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (row["id"],)).fetchone()
    return _public(row)


@router.post("/signout")
async def signout(authorization: str | None = Header(default=None)) -> dict[str, bool]:
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        with _db() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
            conn.commit()
    return {"ok": True}


@router.get("/public-profile")
async def public_profile(display_name: str) -> dict[str, Any]:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE lower(display_name)=lower(?) ORDER BY id DESC LIMIT 1",
            (display_name.strip(),)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No EcoTrace account profile found.")
    return {
        "display_name": row["display_name"],
        "role": row["role"],
        "organization": row["organization"],
        "education_work": row["education_work"],
        "bio": row["bio"],
        "share_history": bool(row["share_history"]),
    }


class ValidationReportCreate(BaseModel):
    report_type: str = Field(max_length=40)
    location: str | None = Field(default=None, max_length=220)
    note: str = Field(min_length=3, max_length=1500)
    evidence_url: str | None = Field(default=None, max_length=500)
    display_name: str | None = Field(default=None, max_length=80)


class ValidationReview(BaseModel):
    status: str = Field(pattern="^(verified|rejected)$")
    reviewer_note: str | None = Field(default=None, max_length=1000)
    count_as_check: bool = True
    check_result: str | None = Field(default=None, pattern="^(pass|fail)$")


class ValidationCheckCreate(BaseModel):
    check_type: str = Field(max_length=40)
    result: str = Field(pattern="^(pass|fail)$")
    location: str | None = Field(default=None, max_length=220)
    note: str | None = Field(default=None, max_length=1000)


def _optional_auth(authorization: str | None) -> sqlite3.Row | None:
    if not authorization:
        return None
    try:
        return _auth(authorization)
    except HTTPException:
        return None


def _require_reviewer(authorization: str | None) -> sqlite3.Row:
    row = _auth(authorization)
    if not _trusted_reviewer(row["username"]):
        raise HTTPException(status_code=403, detail="Trusted reviewer access required.")
    return row


@router.post("/validation/reports")
async def create_validation_report(
    payload: ValidationReportCreate,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    account = _optional_auth(authorization)
    now = datetime.now(timezone.utc).isoformat()
    display_name = (
        account["display_name"] if account is not None
        else (payload.display_name or "Anonymous reporter").strip()
    )
    with _db() as conn:
        cur = conn.execute("""
            INSERT INTO validation_reports(
                reporter_account_id,display_name,report_type,location,note,evidence_url,status,created_at
            ) VALUES(?,?,?,?,?,?,?,?)
        """, (
            account["id"] if account is not None else None,
            display_name,
            payload.report_type,
            payload.location,
            payload.note,
            payload.evidence_url,
            "pending",
            now
        ))
        conn.commit()
    return {"ok": True, "id": cur.lastrowid, "status": "pending"}


@router.get("/validation/reports")
async def list_validation_reports(
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    _require_reviewer(authorization)
    with _db() as conn:
        rows = conn.execute("""
            SELECT * FROM validation_reports
            ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, id DESC
            LIMIT 500
        """).fetchall()
    return [dict(row) for row in rows]


@router.post("/validation/reports/{report_id}/review")
async def review_validation_report(
    report_id: int,
    payload: ValidationReview,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    reviewer = _require_reviewer(authorization)
    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        report = conn.execute(
            "SELECT * FROM validation_reports WHERE id=?", (report_id,)
        ).fetchone()
        if not report:
            raise HTTPException(status_code=404, detail="Validation report not found.")
        conn.execute("""
            UPDATE validation_reports
            SET status=?, reviewer_note=?, reviewed_at=?
            WHERE id=?
        """, (payload.status, payload.reviewer_note, now, report_id))
        if payload.count_as_check and payload.check_result in {"pass","fail"}:
            conn.execute("""
                INSERT INTO validation_checks(
                    reviewer_account_id,check_type,result,location,note,source_report_id,created_at
                ) VALUES(?,?,?,?,?,?,?)
            """, (
                reviewer["id"],
                report["report_type"],
                payload.check_result,
                report["location"],
                payload.reviewer_note or report["note"],
                report_id,
                now
            ))
        conn.commit()
    return {"ok": True, "status": payload.status}


@router.post("/validation/checks")
async def create_validation_check(
    payload: ValidationCheckCreate,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    reviewer = _require_reviewer(authorization)
    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        cur = conn.execute("""
            INSERT INTO validation_checks(
                reviewer_account_id,check_type,result,location,note,created_at
            ) VALUES(?,?,?,?,?,?)
        """, (
            reviewer["id"], payload.check_type, payload.result,
            payload.location, payload.note, now
        ))
        conn.commit()
    return {"ok": True, "id": cur.lastrowid}


@router.get("/validation/stats")
async def validation_stats() -> dict[str, Any]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT check_type,result,COUNT(*) AS n FROM validation_checks GROUP BY check_type,result"
        ).fetchall()
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM validation_reports WHERE status='pending'"
        ).fetchone()["n"]
    grouped: dict[str, dict[str, int]] = {}
    for row in rows:
        grouped.setdefault(row["check_type"], {"pass":0,"fail":0})
        grouped[row["check_type"]][row["result"]] = row["n"]
    def one(kind: str) -> dict[str, Any]:
        item = grouped.get(kind, {"pass":0,"fail":0})
        total = item["pass"] + item["fail"]
        return {
            **item,
            "total": total,
            "rate_percent": round(item["pass"]/total*100,1) if total else None,
        }
    return {
        "tree": one("tree"),
        "route": one("route"),
        "bike": one("bike"),
        "pending_reports": pending,
    }
