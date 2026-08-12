"""
FastAPI 服务入口。

路由按两条链路组织：
  生成链路  POST /api/skills/generate      自然语言 → SkillSpec（含四道校验闸）
  执行链路  POST /api/skills/{id}/run      上传 CSV → 校验 → 计算 → 报告

治理相关：
  GET  /api/skills                资产列表
  GET  /api/skills/{id}           详情
  PUT  /api/skills/{id}           编辑（已发布版本会自动产生新版本）
  POST /api/skills/{id}/publish   发布
  GET  /api/skills/{id}/versions  版本历史
  GET  /api/stats                 资产健康度

启动： ./.venv/bin/uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.executor.loader import DataValidationError, load_and_validate
from app.executor.orchestrator import SkillOrchestrator
from app.generator.generator import SkillGenerator
from app.generator.validator import validate
from app.llm.client import gateway
from app.schemas.skill_spec import SkillSpec
from app.storage import repository as repo

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "static"
SAMPLES_DIR = ROOT / "data" / "samples"


@asynccontextmanager
async def lifespan(app: FastAPI):
    repo.init_db(load_presets=True)
    yield


app = FastAPI(
    title="企业岗位经验 Skill 生成平台",
    description="把自然语言描述的岗位经验，转换为可执行、可评测、可版本管理的 Skill 资产",
    version="1.0.0",
    lifespan=lifespan,
)

# 允许从 file:// 直接打开 index.html 时也能调用接口。
# 常见场景：拿到仓库的人双击 HTML 文件而不是访问服务地址。
# 本 Demo 无鉴权、无敏感数据，放开跨域不增加实际暴露面；
# 若要用于生产，这里必须收紧为具体域名白名单。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# 请求模型
# --------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    request: str = Field(description="自然语言需求描述")
    max_attempts: int = Field(default=3, ge=1, le=5)
    self_critique: bool = Field(default=True)


class UpdateRequest(BaseModel):
    spec: dict = Field(description="完整的 SkillSpec")
    change_note: str | None = None


# --------------------------------------------------------------------------
# 系统状态
# --------------------------------------------------------------------------

@app.get("/api/status")
def get_status() -> dict:
    status = gateway.status()
    return {
        **status,
        "mock_mode": gateway.is_mock,
        "hint": (
            "演示模式：数值由确定性计算引擎真实算出，文字分析为预置内容。"
            "配置 .env 中的 LLM_API_KEY 后可启用完整能力。"
            if gateway.is_mock
            else None
        ),
    }


@app.get("/api/stats")
def get_stats() -> dict:
    return repo.usage_stats()


# --------------------------------------------------------------------------
# 生成链路
# --------------------------------------------------------------------------

@app.post("/api/skills/generate")
def generate_skill(body: GenerateRequest) -> JSONResponse:
    """自然语言 → SkillSpec。经过四道校验闸，失败会带错误信息自动重试。"""
    if not body.request.strip():
        raise HTTPException(status_code=400, detail="需求描述不能为空")

    result = SkillGenerator(
        max_attempts=body.max_attempts,
        run_self_critique=body.self_critique,
    ).generate(body.request)

    # 即使未通过全部校验也保存为草稿 —— 让用户在配置界面上人工修正，
    # 总比直接报错、什么都不给强
    if result.skill is not None:
        repo.save_skill(result.skill, change_note=f"由需求生成：{body.request[:50]}")

    return JSONResponse(result.to_dict())


# --------------------------------------------------------------------------
# 资产管理
# --------------------------------------------------------------------------

@app.get("/api/skills")
def list_skills(include_drafts: bool = True) -> list[dict]:
    return repo.list_skills(include_drafts=include_drafts)


@app.get("/api/skills/{skill_id}")
def get_skill(skill_id: str, version: str | None = None) -> dict:
    skill = repo.get_skill(skill_id, version)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill 不存在：{skill_id}")
    return {
        "skill": skill.model_dump(mode="json"),
        "validation": validate(skill).to_dict(),
        "versions": repo.list_versions(skill_id),
    }


@app.get("/api/skills/{skill_id}/versions")
def get_versions(skill_id: str) -> list[dict]:
    return repo.list_versions(skill_id)


@app.put("/api/skills/{skill_id}")
def update_skill(skill_id: str, body: UpdateRequest) -> dict:
    """
    编辑 Skill。已发布版本会自动产生新版本并回到草稿状态。

    保存前重跑四道校验闸 —— 人工编辑同样可能引入引用错误。
    """
    try:
        updated = SkillSpec.model_validate(body.spec)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"SkillSpec 格式错误：{exc}") from exc

    validation = validate(updated)
    try:
        saved = repo.update_skill(skill_id, updated, change_note=body.change_note)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {
        "skill": saved.model_dump(mode="json"),
        "validation": validation.to_dict(),
        "versions": repo.list_versions(skill_id),
    }


@app.post("/api/skills/{skill_id}/publish")
def publish_skill(skill_id: str, version: str | None = None) -> dict:
    """发布。未通过校验的 Skill 不允许发布 —— 这是资产库的质量门槛。"""
    skill = repo.get_skill(skill_id, version)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill 不存在：{skill_id}")

    validation = validate(skill)
    if not validation.passed:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "未通过校验的 Skill 不允许发布，请先修正以下问题",
                "errors": validation.all_errors,
            },
        )

    saved = repo.publish_skill(skill_id, version)
    return {"skill": saved.model_dump(mode="json"), "validation": validation.to_dict()}


# --------------------------------------------------------------------------
# 执行链路
# --------------------------------------------------------------------------

@app.post("/api/skills/{skill_id}/run")
async def run_skill(
    skill_id: str,
    file: UploadFile = File(...),
    version: str | None = Form(default=None),
    mapping: str | None = Form(default=None),
) -> JSONResponse:
    """上传 CSV 并执行 Skill。"""
    skill = repo.get_skill(skill_id, version)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill 不存在：{skill_id}")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传的文件为空")

    mapping_override: dict[str, str] | None = None
    if mapping:
        try:
            mapping_override = json.loads(mapping)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="字段映射不是合法的 JSON")

    # 必填字段缺失时明确拦截，不允许带病运行
    try:
        df, report = load_and_validate(content, skill, mapping_override)
    except DataValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"error": str(exc), "detail": exc.detail},
        )

    result = SkillOrchestrator(skill, df).run(data_warnings=report.warnings)

    repo.log_execution(
        skill_id=skill.skill_id,
        version=skill.version,
        row_count=result.row_count,
        metric_count=len(result.metrics),
        duration_ms=result.total_duration_ms,
        halluc_count=len(result.hallucination_flags),
    )

    return JSONResponse(
        {
            "skill": {"name": skill.name, "version": skill.version},
            "validation_report": report.to_dict(),
            "execution": result.to_dict(),
        }
    )


@app.get("/api/skills/{skill_id}/sample")
def download_sample(skill_id: str):
    """下载示例数据。没有配套示例时，按 input_schema 生成一份空模板。"""
    skill = repo.get_skill(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill 不存在：{skill_id}")

    mapping = {"skill_douyin_live_review": "douyin_live_7days.csv"}
    filename = mapping.get(skill_id)
    if filename and (SAMPLES_DIR / filename).exists():
        return FileResponse(
            SAMPLES_DIR / filename, filename=filename, media_type="text/csv"
        )

    # 生成空模板：表头 + 一行提示，让用户知道该填什么
    header = ",".join(f.name for f in skill.input_schema)
    hint = ",".join((f.source_hint or f.description or "")[:20] for f in skill.input_schema)
    csv = f"{header}\n{hint}\n"
    return JSONResponse(
        content={"filename": f"{skill_id}_template.csv", "content": csv},
    )


# --------------------------------------------------------------------------
# 静态资源
# --------------------------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
