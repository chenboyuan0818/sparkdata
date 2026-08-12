"""
Skill 资产库 —— 带版本管理的存储层。

为什么一定要有版本：
  已发布的 Skill 正在被业务方使用。直接原地编辑意味着线上行为被静默改变，
  出了问题既查不出是哪次改动引起的，也回不去。
  所以规则是：**已发布版本不可直接编辑，任何改动都产生新版本，旧版本永久保留。**

Demo 用 SQLite（零配置、单文件、随仓库分发）。
切生产只需把 _connect() 换成 PostgreSQL 连接，其余逻辑不变。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from app.schemas.skill_spec import SkillSpec, SkillStatus

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
PRESETS_DIR = DATA_DIR / "presets"

# 数据库位置可通过环境变量覆盖。
# 云平台（HF Spaces / Render 等）的应用目录常常只读或不持久，
# 这时把 DB 指到平台提供的可写目录，例如 SKILL_DB_PATH=/tmp/skills.db。
# 预置 Skill 每次启动都会重新灌入，所以即便 DB 是临时的，Demo 也能正常工作。
DB_PATH = Path(os.getenv("SKILL_DB_PATH", str(DATA_DIR / "skills.db")))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id    TEXT NOT NULL,
    version     TEXT NOT NULL,
    status      TEXT NOT NULL,
    name        TEXT NOT NULL,
    domain      TEXT NOT NULL,
    owner       TEXT,
    spec_json   TEXT NOT NULL,
    change_note TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE(skill_id, version)
);

CREATE TABLE IF NOT EXISTS executions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id     TEXT NOT NULL,
    version      TEXT NOT NULL,
    row_count    INTEGER,
    metric_count INTEGER,
    duration_ms  INTEGER,
    halluc_count INTEGER,
    feedback     TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skills_skill_id ON skills(skill_id);
CREATE INDEX IF NOT EXISTS idx_exec_skill_id  ON executions(skill_id);
"""


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(load_presets: bool = True) -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)
    if load_presets:
        _load_presets()


def _load_presets() -> None:
    """首次启动时把预置 Skill 灌入资产库。已存在的跳过，不覆盖用户改动。"""
    if not PRESETS_DIR.exists():
        return
    for path in sorted(PRESETS_DIR.glob("*.json")):
        try:
            skill = SkillSpec.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001  预置文件损坏不应阻断启动
            continue
        if get_skill(skill.skill_id, skill.version) is None:
            save_skill(skill, change_note="预置 Skill 初始化")


# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------

def save_skill(skill: SkillSpec, change_note: str | None = None) -> SkillSpec:
    """保存一个版本。同 (skill_id, version) 已存在时覆盖该版本的内容。"""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO skills
                (skill_id, version, status, name, domain, owner,
                 spec_json, change_note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(skill_id, version) DO UPDATE SET
                status      = excluded.status,
                name        = excluded.name,
                spec_json   = excluded.spec_json,
                change_note = excluded.change_note
            """,
            (
                skill.skill_id,
                skill.version,
                skill.status.value,
                skill.name,
                skill.domain.value,
                skill.owner,
                skill.model_dump_json(),
                change_note,
                skill.created_at,
            ),
        )
    return skill


def bump_version(version: str) -> str:
    """1.0.0 → 1.1.0。次版本号递增，主版本号留给不兼容变更（如输入字段删减）。"""
    try:
        major, minor, patch = (int(p) for p in version.split("."))
    except ValueError:
        return "1.1.0"
    return f"{major}.{minor + 1}.0"


def update_skill(
    skill_id: str, updated: SkillSpec, change_note: str | None = None
) -> SkillSpec:
    """
    更新 Skill。

    已发布版本不允许原地改 —— 生成新版本，旧版本保留可回滚。
    草稿版本可以直接改，因为它还没有人在用。
    """
    current = get_skill(skill_id)
    if current is None:
        raise ValueError(f"Skill 不存在：{skill_id}")

    if current.status == SkillStatus.PUBLISHED:
        updated.version = bump_version(current.version)
        updated.status = SkillStatus.DRAFT   # 新版本回到草稿，需重新确认后发布
    else:
        updated.version = current.version

    updated.skill_id = skill_id
    updated.created_at = current.created_at
    updated.updated_at = datetime.now().isoformat(timespec="seconds")
    return save_skill(updated, change_note=change_note)


def publish_skill(skill_id: str, version: str | None = None) -> SkillSpec:
    skill = get_skill(skill_id, version)
    if skill is None:
        raise ValueError(f"Skill 不存在：{skill_id}")
    skill.status = SkillStatus.PUBLISHED
    skill.updated_at = datetime.now().isoformat(timespec="seconds")
    return save_skill(skill, change_note="发布")


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------

def _row_to_skill(row: sqlite3.Row) -> SkillSpec:
    return SkillSpec.model_validate_json(row["spec_json"])


def get_skill(skill_id: str, version: str | None = None) -> SkillSpec | None:
    """不指定版本时返回最新版本。"""
    with _connect() as conn:
        if version:
            row = conn.execute(
                "SELECT * FROM skills WHERE skill_id = ? AND version = ?",
                (skill_id, version),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM skills WHERE skill_id = ? ORDER BY id DESC LIMIT 1",
                (skill_id,),
            ).fetchone()
    return _row_to_skill(row) if row else None


def list_skills(include_drafts: bool = True) -> list[dict]:
    """
    资产列表 —— 每个 skill_id 只返回最新版本，并附带版本数。

    这是资产库首页要展示的视图：使用者关心的是"有哪些能力可用"，
    而不是"历史上有过哪些版本"。
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT s.*, (
                SELECT COUNT(*) FROM skills v WHERE v.skill_id = s.skill_id
            ) AS version_count
            FROM skills s
            INNER JOIN (
                SELECT skill_id, MAX(id) AS max_id FROM skills GROUP BY skill_id
            ) latest ON s.id = latest.max_id
            ORDER BY s.id DESC
            """
        ).fetchall()

    items: list[dict] = []
    for row in rows:
        if not include_drafts and row["status"] != SkillStatus.PUBLISHED.value:
            continue
        skill = _row_to_skill(row)
        items.append(
            {
                "skill_id": skill.skill_id,
                "name": skill.name,
                "description": skill.description,
                "domain": skill.domain.value,
                "status": skill.status.value,
                "version": skill.version,
                "version_count": row["version_count"],
                "owner": skill.owner,
                "field_count": len(skill.input_schema),
                "step_count": len(skill.analysis_flow),
                "metric_step_count": len(skill.metric_steps),
                "llm_step_count": len(skill.llm_steps),
                "use_cases": skill.use_cases,
                "updated_at": skill.updated_at,
            }
        )
    return items


def list_versions(skill_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT version, status, change_note, created_at
            FROM skills WHERE skill_id = ? ORDER BY id DESC
            """,
            (skill_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# 执行记录
# --------------------------------------------------------------------------

def log_execution(
    skill_id: str,
    version: str,
    row_count: int,
    metric_count: int,
    duration_ms: int,
    halluc_count: int,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO executions
                (skill_id, version, row_count, metric_count,
                 duration_ms, halluc_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                skill_id,
                version,
                row_count,
                metric_count,
                duration_ms,
                halluc_count,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def usage_stats() -> dict:
    """资产健康度看板的数据源。"""
    with _connect() as conn:
        totals = conn.execute(
            """
            SELECT COUNT(DISTINCT skill_id) AS skill_count,
                   COUNT(*) AS version_count
            FROM skills
            """
        ).fetchone()
        published = conn.execute(
            "SELECT COUNT(DISTINCT skill_id) AS c FROM skills WHERE status = ?",
            (SkillStatus.PUBLISHED.value,),
        ).fetchone()
        execs = conn.execute(
            """
            SELECT COUNT(*) AS run_count,
                   COALESCE(SUM(halluc_count), 0) AS halluc_total,
                   COALESCE(AVG(duration_ms), 0) AS avg_ms
            FROM executions
            """
        ).fetchone()
        top = conn.execute(
            """
            SELECT skill_id, COUNT(*) AS runs FROM executions
            GROUP BY skill_id ORDER BY runs DESC LIMIT 5
            """
        ).fetchall()

    return {
        "skill_count": totals["skill_count"],
        "version_count": totals["version_count"],
        "published_count": published["c"],
        "run_count": execs["run_count"],
        "hallucination_total": execs["halluc_total"],
        "avg_duration_ms": round(execs["avg_ms"]),
        "top_skills": [dict(r) for r in top],
    }
