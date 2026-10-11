"""
EcoTrace Community Hub v2.

Features:
- Combined public feed
- Dedicated tree suggestions
- Environmental events/campaigns with join/leave support
- Environmental discussions with replies/comments
- Optional map coordinates and image URL
- SQLite persistence
- Reporting/moderation status
- No accounts or private messaging in this prototype
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field


router = APIRouter(prefix="/api/community", tags=["community"])

DATABASE_PATH = Path(
    os.getenv("ECOTRACE_COMMUNITY_DB", "ecotrace_community.db")
).expanduser()

_DB_LOCK = threading.RLock()

PostCategory = Literal[
    "general",
    "tree_suggestion",
    "park_cleanup",
    "environmental_campaign",
    "discussion",
    "heat_report",
]

PostVisibility = Literal["public", "private_draft"]
EventStatus = Literal[
    "open",
    "full",
    "completed",
    "cancelled",
    "not_applicable",
]


class CommunityPostCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=40)
    category: PostCategory
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=3, max_length=2000)
    visibility: PostVisibility = "public"

    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)

    event_date: str | None = Field(default=None, max_length=40)
    location_text: str | None = Field(default=None, max_length=220)
    image_url: str | None = Field(default=None, max_length=500)

    max_participants: int | None = Field(default=None, ge=1, le=10000)
    tags: list[str] = Field(default_factory=list, max_length=12)
    tree_reason: str | None = Field(default=None, max_length=500)


class CommunityCommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=40)
    message: str = Field(min_length=1, max_length=1000)
    parent_comment_id: int | None = Field(default=None, ge=1)


class CommunityPostUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=3, max_length=2000)
    location_text: str | None = Field(default=None, max_length=220)
    image_url: str | None = Field(default=None, max_length=500)
    tree_reason: str | None = Field(default=None, max_length=500)
    event_date: str | None = Field(default=None, max_length=40)


class CommunityCommentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=1000)


class JoinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=40)


class CommunityPost(BaseModel):
    id: int
    display_name: str
    category: PostCategory
    title: str
    description: str
    visibility: PostVisibility

    latitude: float | None = None
    longitude: float | None = None
    event_date: str | None = None
    location_text: str | None = None
    image_url: str | None = None

    max_participants: int | None = None
    participant_count: int = 0
    participant_names: list[str] = Field(default_factory=list)
    event_status: EventStatus
    tags: list[str] = Field(default_factory=list)
    tree_reason: str | None = None

    moderation_status: str
    created_at: str
    edit_token: str | None = None
    comment_count: int = 0
    edit_token: str | None = None


class CommunityComment(BaseModel):
    id: int
    post_id: int
    parent_comment_id: int | None = None
    display_name: str
    message: str
    created_at: str


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(
        DATABASE_PATH,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def _table_columns(
    connection: sqlite3.Connection,
    table_name: str,
) -> set[str]:
    rows = connection.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def _ensure_column(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    declaration: str,
) -> None:
    if column_name in _table_columns(connection, table_name):
        return
    connection.execute(
        f"ALTER TABLE {table_name} "
        f"ADD COLUMN {column_name} {declaration}"
    )


def initialize_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    with _DB_LOCK, _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS community_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                visibility TEXT NOT NULL,
                latitude REAL,
                longitude REAL,
                event_date TEXT,
                location_text TEXT,
                image_url TEXT,
                moderation_status TEXT NOT NULL DEFAULT 'visible',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS community_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (post_id)
                    REFERENCES community_posts(id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS community_event_participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(post_id, display_name),
                FOREIGN KEY (post_id)
                    REFERENCES community_posts(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_community_posts_created
            ON community_posts(created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_community_posts_category
            ON community_posts(category);

            CREATE INDEX IF NOT EXISTS idx_community_comments_post
            ON community_comments(post_id, created_at);

            CREATE INDEX IF NOT EXISTS idx_community_participants_post
            ON community_event_participants(post_id);
            """
        )

        _ensure_column(
            connection,
            "community_posts",
            "max_participants",
            "INTEGER",
        )
        _ensure_column(
            connection,
            "community_posts",
            "event_status",
            "TEXT NOT NULL DEFAULT 'not_applicable'",
        )
        _ensure_column(
            connection,
            "community_posts",
            "tags_json",
            "TEXT NOT NULL DEFAULT '[]'",
        )
        _ensure_column(
            connection,
            "community_posts",
            "tree_reason",
            "TEXT",
        )
        _ensure_column(
            connection,
            "community_comments",
            "parent_comment_id",
            "INTEGER",
        )
        _ensure_column(
            connection,
            "community_posts",
            "owner_token_hash",
            "TEXT",
        )
        _ensure_column(
            connection,
            "community_comments",
            "owner_token_hash",
            "TEXT",
        )


def _new_edit_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(24)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return token, digest


def _token_matches(token: str, stored_hash: str | None) -> bool:
    if not token or not stored_hash:
        return False
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return secrets.compare_digest(digest, stored_hash)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_tags(value: str | None) -> list[str]:
    import json

    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [
        str(item)
        for item in parsed
        if isinstance(item, (str, int, float))
    ]


def _dump_tags(values: list[str]) -> str:
    import json

    clean = []
    seen: set[str] = set()

    for value in values:
        item = " ".join(str(value).strip().split())
        if not item:
            continue
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        clean.append(item[:40])

    return json.dumps(clean[:12], ensure_ascii=False)


def _is_event_category(category: str) -> bool:
    return category in {
        "park_cleanup",
        "environmental_campaign",
    }


def _parse_participant_names(value: str | None) -> list[str]:
    if not value:
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in str(value).split("||"):
        cleaned = item.strip()
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            output.append(cleaned)
    return output


def _post_from_row(row: sqlite3.Row) -> CommunityPost:
    return CommunityPost(
        id=int(row["id"]),
        display_name=str(row["display_name"]),
        category=str(row["category"]),
        title=str(row["title"]),
        description=str(row["description"]),
        visibility=str(row["visibility"]),
        latitude=row["latitude"],
        longitude=row["longitude"],
        event_date=row["event_date"],
        location_text=row["location_text"],
        image_url=row["image_url"],
        max_participants=row["max_participants"],
        participant_count=int(row["participant_count"] or 0),
        participant_names=_parse_participant_names(
            row["participant_names"]
            if "participant_names" in row.keys()
            else None
        ),
        event_status=str(
            row["event_status"]
            or "not_applicable"
        ),
        tags=_parse_tags(row["tags_json"]),
        tree_reason=row["tree_reason"],
        moderation_status=str(row["moderation_status"]),
        created_at=str(row["created_at"]),
        comment_count=int(row["comment_count"] or 0),
        edit_token=None,
    )


def _post_select_sql() -> str:
    return """
        SELECT
            p.*,
            COUNT(DISTINCT c.id) AS comment_count,
            COUNT(DISTINCT ep.id) AS participant_count,
            GROUP_CONCAT(ep.display_name, '||') AS participant_names
        FROM community_posts AS p
        LEFT JOIN community_comments AS c
            ON c.post_id = p.id
        LEFT JOIN community_event_participants AS ep
            ON ep.post_id = p.id
    """


initialize_database()


@router.get("/posts", response_model=list[CommunityPost])
async def list_posts(
    category: PostCategory | None = Query(default=None),
    section: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
) -> list[CommunityPost]:
    where = [
        "p.visibility = 'public'",
        "p.moderation_status = 'visible'",
    ]
    parameters: list[Any] = []

    if category:
        where.append("p.category = ?")
        parameters.append(category)

    if section == "trees":
        where.append("p.category = 'tree_suggestion'")
    elif section == "events":
        where.append(
            "p.category IN ('park_cleanup', 'environmental_campaign')"
        )
    elif section == "discussions":
        where.append("p.category = 'discussion'")

    query = (
        _post_select_sql()
        + f"""
        WHERE {' AND '.join(where)}
        GROUP BY p.id
        ORDER BY p.created_at DESC
        LIMIT ?
        """
    )
    parameters.append(limit)

    with _DB_LOCK, _connect() as connection:
        rows = connection.execute(
            query,
            parameters,
        ).fetchall()

    return [_post_from_row(row) for row in rows]


@router.post("/posts", response_model=CommunityPost)
async def create_post(
    payload: CommunityPostCreate,
) -> CommunityPost:
    if (
        payload.category == "tree_suggestion"
        and (
            payload.latitude is None
            or payload.longitude is None
        )
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "Tree suggestions require a map coordinate. "
                "Click the map first."
            ),
        )

    if (
        _is_event_category(payload.category)
        and not payload.event_date
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "Environmental events and campaigns require "
                "a date and time."
            ),
        )

    created_at = _utc_now()
    edit_token, owner_token_hash = _new_edit_token()
    moderation_status = (
        "visible"
        if payload.visibility == "public"
        else "private"
    )
    event_status: EventStatus = (
        "open"
        if _is_event_category(payload.category)
        else "not_applicable"
    )

    with _DB_LOCK, _connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO community_posts (
                display_name,
                category,
                title,
                description,
                visibility,
                latitude,
                longitude,
                event_date,
                location_text,
                image_url,
                moderation_status,
                created_at,
                max_participants,
                event_status,
                tags_json,
                tree_reason,
                owner_token_hash
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.display_name.strip(),
                payload.category,
                payload.title.strip(),
                payload.description.strip(),
                payload.visibility,
                payload.latitude,
                payload.longitude,
                payload.event_date,
                payload.location_text.strip()
                if payload.location_text
                else None,
                payload.image_url.strip()
                if payload.image_url
                else None,
                moderation_status,
                created_at,
                payload.max_participants,
                event_status,
                _dump_tags(payload.tags),
                payload.tree_reason.strip()
                if payload.tree_reason
                else None,
                owner_token_hash,
            ),
        )
        post_id = int(cursor.lastrowid)

        row = connection.execute(
            _post_select_sql()
            + """
            WHERE p.id = ?
            GROUP BY p.id
            """,
            (post_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(
            status_code=500,
            detail="Post could not be read after creation.",
        )

    created = _post_from_row(row)
    created.edit_token = edit_token
    return created


@router.get(
    "/posts/{post_id}/comments",
    response_model=list[CommunityComment],
)
async def list_comments(
    post_id: int,
) -> list[CommunityComment]:
    with _DB_LOCK, _connect() as connection:
        post = connection.execute(
            """
            SELECT id
            FROM community_posts
            WHERE id = ?
              AND visibility = 'public'
              AND moderation_status = 'visible'
            """,
            (post_id,),
        ).fetchone()

        if post is None:
            raise HTTPException(
                status_code=404,
                detail="Public post not found.",
            )

        rows = connection.execute(
            """
            SELECT *
            FROM community_comments
            WHERE post_id = ?
            ORDER BY created_at ASC
            """,
            (post_id,),
        ).fetchall()

    return [
        CommunityComment(
            id=int(row["id"]),
            post_id=int(row["post_id"]),
            parent_comment_id=row["parent_comment_id"],
            display_name=str(row["display_name"]),
            message=str(row["message"]),
            created_at=str(row["created_at"]),
        )
        for row in rows
    ]


@router.post(
    "/posts/{post_id}/comments",
    response_model=CommunityComment,
)
async def create_comment(
    post_id: int,
    payload: CommunityCommentCreate,
) -> CommunityComment:
    created_at = _utc_now()
    edit_token, owner_token_hash = _new_edit_token()

    with _DB_LOCK, _connect() as connection:
        post = connection.execute(
            """
            SELECT id
            FROM community_posts
            WHERE id = ?
              AND visibility = 'public'
              AND moderation_status = 'visible'
            """,
            (post_id,),
        ).fetchone()

        if post is None:
            raise HTTPException(
                status_code=404,
                detail="Public post not found.",
            )

        if payload.parent_comment_id is not None:
            parent = connection.execute(
                """
                SELECT id
                FROM community_comments
                WHERE id = ? AND post_id = ?
                """,
                (
                    payload.parent_comment_id,
                    post_id,
                ),
            ).fetchone()

            if parent is None:
                raise HTTPException(
                    status_code=422,
                    detail="Reply target was not found.",
                )

        cursor = connection.execute(
            """
            INSERT INTO community_comments (
                post_id,
                parent_comment_id,
                display_name,
                message,
                created_at,
                owner_token_hash
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                post_id,
                payload.parent_comment_id,
                payload.display_name.strip(),
                payload.message.strip(),
                created_at,
                owner_token_hash,
            ),
        )
        comment_id = int(cursor.lastrowid)

    return CommunityComment(
        id=comment_id,
        post_id=post_id,
        parent_comment_id=payload.parent_comment_id,
        display_name=payload.display_name.strip(),
        message=payload.message.strip(),
        created_at=created_at,
        edit_token=edit_token,
    )


@router.patch(
    "/posts/{post_id}",
    response_model=CommunityPost,
)
async def update_post(
    post_id: int,
    payload: CommunityPostUpdate,
    edit_token: str = Query(..., min_length=10, max_length=200),
) -> CommunityPost:
    with _DB_LOCK, _connect() as connection:
        row = connection.execute(
            "SELECT owner_token_hash FROM community_posts WHERE id = ?",
            (post_id,),
        ).fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="Post not found.")
        if not _token_matches(edit_token, row["owner_token_hash"]):
            raise HTTPException(
                status_code=403,
                detail="This browser does not own that post.",
            )

        connection.execute(
            """
            UPDATE community_posts
            SET title = ?,
                description = ?,
                location_text = ?,
                image_url = ?,
                tree_reason = ?,
                event_date = ?
            WHERE id = ?
            """,
            (
                payload.title.strip(),
                payload.description.strip(),
                payload.location_text.strip()
                if payload.location_text
                else None,
                payload.image_url.strip()
                if payload.image_url
                else None,
                payload.tree_reason.strip()
                if payload.tree_reason
                else None,
                payload.event_date,
                post_id,
            ),
        )

        updated = connection.execute(
            _post_select_sql()
            + """
            WHERE p.id = ?
            GROUP BY p.id
            """,
            (post_id,),
        ).fetchone()

    if updated is None:
        raise HTTPException(status_code=404, detail="Post not found.")
    return _post_from_row(updated)


@router.delete("/posts/{post_id}")
async def delete_post(
    post_id: int,
    edit_token: str = Query(..., min_length=10, max_length=200),
) -> dict[str, Any]:
    with _DB_LOCK, _connect() as connection:
        row = connection.execute(
            "SELECT owner_token_hash FROM community_posts WHERE id = ?",
            (post_id,),
        ).fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="Post not found.")
        if not _token_matches(edit_token, row["owner_token_hash"]):
            raise HTTPException(
                status_code=403,
                detail="This browser does not own that post.",
            )

        connection.execute(
            "DELETE FROM community_posts WHERE id = ?",
            (post_id,),
        )

    return {"post_id": post_id, "deleted": True}


@router.patch(
    "/comments/{comment_id}",
    response_model=CommunityComment,
)
async def update_comment(
    comment_id: int,
    payload: CommunityCommentUpdate,
    edit_token: str = Query(..., min_length=10, max_length=200),
) -> CommunityComment:
    with _DB_LOCK, _connect() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM community_comments
            WHERE id = ?
            """,
            (comment_id,),
        ).fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="Reply not found.")
        if not _token_matches(edit_token, row["owner_token_hash"]):
            raise HTTPException(
                status_code=403,
                detail="This browser does not own that reply.",
            )

        connection.execute(
            """
            UPDATE community_comments
            SET message = ?
            WHERE id = ?
            """,
            (payload.message.strip(), comment_id),
        )

    return CommunityComment(
        id=int(row["id"]),
        post_id=int(row["post_id"]),
        parent_comment_id=row["parent_comment_id"],
        display_name=str(row["display_name"]),
        message=payload.message.strip(),
        created_at=str(row["created_at"]),
        edit_token=None,
    )


@router.delete("/comments/{comment_id}")
async def delete_comment(
    comment_id: int,
    edit_token: str = Query(..., min_length=10, max_length=200),
) -> dict[str, Any]:
    with _DB_LOCK, _connect() as connection:
        row = connection.execute(
            """
            SELECT owner_token_hash
            FROM community_comments
            WHERE id = ?
            """,
            (comment_id,),
        ).fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="Reply not found.")
        if not _token_matches(edit_token, row["owner_token_hash"]):
            raise HTTPException(
                status_code=403,
                detail="This browser does not own that reply.",
            )

        connection.execute(
            "DELETE FROM community_comments WHERE id = ?",
            (comment_id,),
        )

    return {"comment_id": comment_id, "deleted": True}


@router.post("/posts/{post_id}/complete")
async def complete_community_event(
    post_id: int,
    edit_token: str = Query(..., min_length=10, max_length=200),
) -> dict[str, Any]:
    with _DB_LOCK, _connect() as connection:
        row = connection.execute(
            """
            SELECT id, category, owner_token_hash, event_status
            FROM community_posts
            WHERE id = ?
            """,
            (post_id,),
        ).fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="Environmental event not found.")

        if not _is_event_category(str(row["category"])):
            raise HTTPException(
                status_code=422,
                detail="Only environmental events and campaigns can be marked done.",
            )

        if not _token_matches(edit_token, row["owner_token_hash"]):
            raise HTTPException(
                status_code=403,
                detail="This browser does not own that environmental event.",
            )

        connection.execute(
            """
            UPDATE community_posts
            SET event_status = 'completed'
            WHERE id = ?
            """,
            (post_id,),
        )

    return {
        "post_id": post_id,
        "event_status": "completed",
        "archived": True,
    }


@router.post(
    "/posts/{post_id}/join",
)
async def join_event(
    post_id: int,
    payload: JoinRequest,
) -> dict[str, Any]:
    with _DB_LOCK, _connect() as connection:
        post = connection.execute(
            """
            SELECT
                id,
                category,
                max_participants,
                event_status
            FROM community_posts
            WHERE id = ?
              AND visibility = 'public'
              AND moderation_status = 'visible'
            """,
            (post_id,),
        ).fetchone()

        if post is None:
            raise HTTPException(
                status_code=404,
                detail="Public event not found.",
            )

        if not _is_event_category(str(post["category"])):
            raise HTTPException(
                status_code=422,
                detail="This post is not an event or campaign.",
            )

        if post["event_status"] in {
            "completed",
            "cancelled",
        }:
            raise HTTPException(
                status_code=409,
                detail="This event is not open for joining.",
            )

        existing = connection.execute(
            """
            SELECT id
            FROM community_event_participants
            WHERE post_id = ?
              AND lower(display_name) = lower(?)
            """,
            (
                post_id,
                payload.display_name.strip(),
            ),
        ).fetchone()

        if existing is None:
            current_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM community_event_participants
                    WHERE post_id = ?
                    """,
                    (post_id,),
                ).fetchone()["count"]
            )
            maximum = post["max_participants"]

            if (
                maximum is not None
                and current_count >= int(maximum)
            ):
                connection.execute(
                    """
                    UPDATE community_posts
                    SET event_status = 'full'
                    WHERE id = ?
                    """,
                    (post_id,),
                )
                raise HTTPException(
                    status_code=409,
                    detail="This event is full.",
                )

            connection.execute(
                """
                INSERT INTO community_event_participants (
                    post_id,
                    display_name,
                    created_at
                )
                VALUES (?, ?, ?)
                """,
                (
                    post_id,
                    payload.display_name.strip(),
                    _utc_now(),
                ),
            )

        count = int(
            connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM community_event_participants
                WHERE post_id = ?
                """,
                (post_id,),
            ).fetchone()["count"]
        )

        maximum = post["max_participants"]
        status = "open"

        if maximum is not None and count >= int(maximum):
            status = "full"

        connection.execute(
            """
            UPDATE community_posts
            SET event_status = ?
            WHERE id = ?
            """,
            (
                status,
                post_id,
            ),
        )

    return {
        "post_id": post_id,
        "participant_count": count,
        "event_status": status,
        "joined": True,
    }


@router.delete(
    "/posts/{post_id}/join",
)
async def leave_event(
    post_id: int,
    display_name: str = Query(
        ...,
        min_length=1,
        max_length=40,
    ),
) -> dict[str, Any]:
    with _DB_LOCK, _connect() as connection:
        connection.execute(
            """
            DELETE FROM community_event_participants
            WHERE post_id = ?
              AND lower(display_name) = lower(?)
            """,
            (
                post_id,
                display_name.strip(),
            ),
        )

        count = int(
            connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM community_event_participants
                WHERE post_id = ?
                """,
                (post_id,),
            ).fetchone()["count"]
        )

        connection.execute(
            """
            UPDATE community_posts
            SET event_status = 'open'
            WHERE id = ?
              AND event_status = 'full'
            """,
            (post_id,),
        )

    return {
        "post_id": post_id,
        "participant_count": count,
        "joined": False,
    }


@router.post("/posts/{post_id}/report")
async def report_post(
    post_id: int,
) -> dict[str, Any]:
    with _DB_LOCK, _connect() as connection:
        row = connection.execute(
            """
            SELECT id
            FROM community_posts
            WHERE id = ?
            """,
            (post_id,),
        ).fetchone()

        if row is None:
            raise HTTPException(
                status_code=404,
                detail="Community post not found.",
            )

        connection.execute(
            """
            UPDATE community_posts
            SET moderation_status = 'reported'
            WHERE id = ?
            """,
            (post_id,),
        )

    return {
        "post_id": post_id,
        "status": "reported",
        "message": (
            "The post was hidden pending moderation review."
        ),
    }
