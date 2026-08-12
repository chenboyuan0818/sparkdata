"""
Skill 生成器 —— 把「一句话」变成「可执行的 SkillSpec」。

完整链路：

    意图解析 → 上下文组装 → LLM 结构化生成 → 四道校验闸
                                   ↑              │
                                   └── 带错误信息重试 ──┘
                                                  │
                                            人工确认 → 发布

关键设计：校验失败时不是简单重试，而是把**具体的错误信息回喂给模型**做定向修正。
盲目重试只是碰运气；带着"步骤 S2 引用了未定义字段「退货订单数」"这样的反馈重试，
模型改对的概率高得多。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from app.generator.prompt import (
    SELF_CRITIQUE_SYSTEM,
    build_generation_prompt,
    detect_domain,
)
from app.generator.validator import ValidationResult, validate
from app.llm.client import LLMError, gateway
from app.schemas.skill_spec import Domain, GeneratedSkill, SkillSpec, SkillStatus


@dataclass
class GenerationAttempt:
    """单次生成尝试的记录，用于前端展示生成过程。"""

    attempt: int
    ok: bool
    duration_ms: int
    error: str | None = None
    validation: dict | None = None

    def to_dict(self) -> dict:
        return {
            "attempt": self.attempt,
            "ok": self.ok,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "validation": self.validation,
        }


@dataclass
class GenerationResult:
    ok: bool
    skill: SkillSpec | None = None
    validation: ValidationResult | None = None
    attempts: list[GenerationAttempt] = field(default_factory=list)
    critique: str | None = None
    error: str | None = None
    total_duration_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "skill": self.skill.model_dump(mode="json") if self.skill else None,
            "validation": self.validation.to_dict() if self.validation else None,
            "attempts": [a.to_dict() for a in self.attempts],
            "critique": self.critique,
            "error": self.error,
            "total_duration_ms": self.total_duration_ms,
        }


class SkillGenerator:
    """自然语言 → SkillSpec。"""

    def __init__(self, max_attempts: int = 3, run_self_critique: bool = True):
        self.max_attempts = max_attempts
        self.run_self_critique = run_self_critique

    # ---------------- 主入口 ----------------

    def generate(
        self, user_request: str, domain: Domain | None = None
    ) -> GenerationResult:
        started = time.time()
        result = GenerationResult(ok=False)

        if gateway.is_mock:
            result.error = (
                "当前为演示模式（未配置 LLM API Key），无法生成新 Skill。"
                "你可以直接使用资产库中的预置 Skill 体验执行流程。"
            )
            return result

        domain = domain or detect_domain(user_request)
        system, base_user = build_generation_prompt(user_request, domain)

        feedback: str | None = None
        skill: SkillSpec | None = None
        validation: ValidationResult | None = None

        for attempt in range(1, self.max_attempts + 1):
            attempt_started = time.time()

            user = base_user
            if feedback:
                # 把上一轮校验闸的具体错误回喂给模型，做定向修正
                user = (
                    f"{base_user}\n\n"
                    f"⚠️ 上一次生成的内容未通过系统校验，问题如下。"
                    f"请**仅修正这些问题**，其余部分保持不变：\n\n{feedback}"
                )

            try:
                generated = gateway.generate_structured(
                    system=system,
                    user=user,
                    schema=GeneratedSkill,
                    max_retries=1,   # JSON 本身不合法时的重试，与业务校验重试分开
                )
            except LLMError as exc:
                result.attempts.append(
                    GenerationAttempt(
                        attempt=attempt,
                        ok=False,
                        duration_ms=int((time.time() - attempt_started) * 1000),
                        error=str(exc),
                    )
                )
                feedback = None      # 模型调用失败与业务校验失败无关，不带反馈重试
                continue

            skill = self._to_skill_spec(generated, domain)
            validation = validate(skill)

            result.attempts.append(
                GenerationAttempt(
                    attempt=attempt,
                    ok=validation.passed,
                    duration_ms=int((time.time() - attempt_started) * 1000),
                    validation=validation.to_dict(),
                )
            )

            if validation.passed:
                break

            feedback = validation.feedback_for_model()

        result.skill = skill
        result.validation = validation
        result.total_duration_ms = int((time.time() - started) * 1000)

        if skill is None:
            result.error = "模型调用连续失败，未能产出任何结果。请检查网络与 API 配置。"
            return result

        if validation is not None and not validation.passed:
            # 降级策略：仍把结果交给用户，但明确标注未通过校验，
            # 由人工在配置界面上修正 —— 总比直接报错、什么都不给强
            result.ok = False
            result.error = (
                f"生成结果经过 {len(result.attempts)} 次尝试仍未通过全部校验。"
                f"已保留为草稿，请在配置界面上人工修正标红项。"
            )
            return result

        result.ok = True

        if self.run_self_critique:
            result.critique = self._self_critique(skill)

        return result

    # ---------------- 内部方法 ----------------

    def _to_skill_spec(self, generated: GeneratedSkill, domain: Domain) -> SkillSpec:
        """补齐系统所有的字段，模型不该也不需要生成这些。"""
        now = datetime.now().isoformat(timespec="seconds")
        return SkillSpec(
            **generated.model_dump(),
            skill_id=f"skill_{uuid.uuid4().hex[:12]}",
            version="1.0.0",
            status=SkillStatus.DRAFT,   # 生成物一律是草稿，必须人工确认后才能发布
            created_at=now,
            updated_at=now,
        )

    def _self_critique(self, skill: SkillSpec) -> str | None:
        """
        让模型审查自己的产出。

        模型评价比模型生成更可靠，这是低成本的质量提升手段。
        产出只作为提示展示给用户，不参与自动拦截 —— 避免过度拦截。
        """
        try:
            summary = skill.model_dump_json(indent=2, exclude={
                "skill_id", "version", "status", "created_at", "updated_at", "owner",
            })
            return gateway.generate_text(
                system=SELF_CRITIQUE_SYSTEM,
                user=f"请审查以下 SkillSpec：\n\n{summary}",
                max_tokens=1200,
            )
        except LLMError:
            return None   # 自检失败不影响主流程


# 便捷函数
def generate_skill(user_request: str, **kwargs) -> GenerationResult:
    return SkillGenerator(**kwargs).generate(user_request)
