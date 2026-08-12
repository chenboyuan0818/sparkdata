"""
Skill 质量评分卡 —— 把「这个 Skill 好不好用」变成可量化、可追踪的分数。

五个维度与权重（对应 PRD 中的评分卡）：

  数据准确性  30%  报告里的数字与人工核算是否完全一致    ← 全自动，一票否决
  归因合理性  25%  定位的瓶颈环节是否与专家判断一致      ← 关键词命中 + 可选 LLM 评判
  建议可执行性 25%  建议是否具体到"谁做什么"，而非空话    ← 关键词命中 + 反例检查
  结构完整性  10%  是否严格遵循输出模板，无缺项          ← 全自动
  表达清晰度  10%  是否说人话，无冗余                    ← 启发式 + 可选 LLM 评判

发布门槛：加权总分 ≥ 80，且数据准确性 ≥ 95。
数据准确性一票否决 —— 数字错了，其他四维得分再高也没有意义，
因为企业根本不会用一份数字有错的报告。

默认不调用 LLM 评判（use_llm_judge=False），这样评测可以零成本反复跑；
需要更细的语义评分时再打开。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from app.executor.loader import DataValidationError, load_and_validate
from app.executor.orchestrator import ExecutionResult, SkillOrchestrator
from app.schemas.skill_spec import SkillSpec

WEIGHTS = {
    "data_accuracy": 0.30,
    "attribution": 0.25,
    "actionability": 0.25,
    "completeness": 0.10,
    "clarity": 0.10,
}

PASS_TOTAL = 80.0
PASS_DATA_ACCURACY = 95.0


# --------------------------------------------------------------------------
# 结果结构
# --------------------------------------------------------------------------

@dataclass
class DimensionScore:
    name: str
    score: float                      # 0~100
    weight: float
    detail: list[str] = field(default_factory=list)

    @property
    def weighted(self) -> float:
        return self.score * self.weight

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "score": round(self.score, 1),
            "weight": self.weight,
            "weighted": round(self.weighted, 2),
            "detail": self.detail,
        }


@dataclass
class CaseScore:
    case: str
    passed: bool
    total: float
    dimensions: list[DimensionScore] = field(default_factory=list)
    blocked_as_expected: bool | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "case": self.case,
            "passed": self.passed,
            "total": round(self.total, 1),
            "dimensions": [d.to_dict() for d in self.dimensions],
            "blocked_as_expected": self.blocked_as_expected,
            "error": self.error,
        }


# --------------------------------------------------------------------------
# 单维度打分
# --------------------------------------------------------------------------

def score_data_accuracy(
    result: ExecutionResult, expected: dict, tolerance: float = 1e-6
) -> DimensionScore:
    """
    数值精确比对 —— 完全自动，没有任何主观成分。

    这一维之所以能做到全自动且严格，正是因为架构上把数值计算
    从 LLM 手里拿走了：确定性计算的结果本来就应该可复现、可核对。
    """
    dim = DimensionScore("数据准确性", 0.0, WEIGHTS["data_accuracy"])

    expected_metrics: dict[str, float] = expected.get("expected_metrics", {}) or {}
    expected_flags: dict[str, bool] = expected.get("expected_flags", {}) or {}
    checks = len(expected_metrics) + len(expected_flags)
    if checks == 0:
        dim.score = 100.0
        return dim

    hits = 0

    for name, want in expected_metrics.items():
        got = result.metrics.get(name)
        if got is None:
            dim.detail.append(f"❌ 缺少指标「{name}」")
            continue
        if abs(float(got.value) - float(want)) <= tolerance:
            hits += 1
        else:
            dim.detail.append(
                f"❌ {name}：期望 {want}，实际 {got.value}"
            )

    for name, want in expected_flags.items():
        got = result.metrics.get(name)
        if got is None:
            dim.detail.append(f"❌ 缺少判定项「{name}」")
            continue
        if bool(got.value) == bool(want):
            hits += 1
        else:
            dim.detail.append(
                f"❌ {name}：期望 {'异常' if want else '正常'}，"
                f"实际 {'异常' if got.value else '正常'}"
            )

    dim.score = hits / checks * 100
    if not dim.detail:
        dim.detail.append(f"✅ {checks} 项数值与判定全部与人工核算一致")
    return dim


def score_attribution(result: ExecutionResult, expected: dict) -> DimensionScore:
    """归因合理性：诊断结论是否命中专家标注的瓶颈环节与关键词。"""
    dim = DimensionScore("归因合理性", 0.0, WEIGHTS["attribution"])
    spec = expected.get("expected_diagnosis") or {}
    text = " ".join(result.llm_outputs.values())

    if not spec:
        dim.score = 100.0
        return dim

    parts: list[float] = []

    bottleneck = spec.get("bottleneck")
    if bottleneck:
        if bottleneck == "无":
            # 负样本：不应报出任何环节异常
            claimed = any(k in text for k in ("瓶颈在", "异常环节", "低于基准", "低于健康"))
            parts.append(0.0 if claimed else 100.0)
            dim.detail.append(
                "✅ 未误报异常" if not claimed else "❌ 数据全部达标却报出了瓶颈环节"
            )
        else:
            hit = bottleneck in text
            parts.append(100.0 if hit else 0.0)
            dim.detail.append(
                f"{'✅' if hit else '❌'} 瓶颈环节判定：期望「{bottleneck}」"
            )

    keywords = spec.get("keywords_any") or []
    if keywords:
        matched = [k for k in keywords if k in text]
        ratio = min(len(matched) / 2, 1.0) * 100  # 命中 2 个即满分
        parts.append(ratio)
        dim.detail.append(f"关键词命中 {len(matched)}/{len(keywords)}：{matched}")

    dim.score = sum(parts) / len(parts) if parts else 100.0
    return dim


def score_actionability(result: ExecutionResult, expected: dict) -> DimensionScore:
    """
    建议可执行性。

    除了关键词命中，还做两项刚性检查：
      - must_not_contain：出现"提升转化率"这类空话直接扣分
      - 四要素：建议里是否出现责任角色和时间周期
    """
    dim = DimensionScore("建议可执行性", 0.0, WEIGHTS["actionability"])
    spec = expected.get("expected_actions") or {}
    text = " ".join(result.llm_outputs.values())

    parts: list[float] = []

    keywords = spec.get("keywords_any") or []
    if keywords:
        matched = [k for k in keywords if k in text]
        parts.append(min(len(matched) / 2, 1.0) * 100)
        dim.detail.append(f"建议方向命中 {len(matched)}/{len(keywords)}：{matched}")

    # 四要素：责任角色 + 见效周期
    has_owner = any(k in text for k in ("负责", "责任", "主播", "运营", "投手", "选品", "团队"))
    has_period = any(k in text for k in ("周期", "天", "周", "场", "小时", "月"))
    parts.append(100.0 if (has_owner and has_period) else (50.0 if has_owner or has_period else 0.0))
    dim.detail.append(
        f"{'✅' if has_owner else '❌'} 含责任角色　{'✅' if has_period else '❌'} 含见效周期"
    )

    # 反例：出现即重罚
    banned = expected.get("must_not_contain") or []
    violated = [b for b in banned if b in text]
    if violated:
        parts.append(0.0)
        dim.detail.append(f"❌ 出现无可执行性的空话：{violated}")

    dim.score = sum(parts) / len(parts) if parts else 100.0
    return dim


def score_completeness(
    result: ExecutionResult, skill: SkillSpec, expected: dict
) -> DimensionScore:
    """结构完整性：模板占位符是否全部被填充、步骤是否全部执行成功。"""
    dim = DimensionScore("结构完整性", 0.0, WEIGHTS["completeness"])
    parts: list[float] = []

    unfilled = result.report_markdown.count("未找到占位符")
    parts.append(100.0 if unfilled == 0 else 0.0)
    dim.detail.append(
        "✅ 占位符全部填充" if unfilled == 0 else f"❌ {unfilled} 个占位符未填充"
    )

    failed = [t for t in result.traces if t.status == "failed"]
    parts.append(100.0 if not failed else 0.0)
    dim.detail.append(
        "✅ 所有步骤执行成功" if not failed else f"❌ {len(failed)} 个步骤失败"
    )

    # 模板里的一级标题应当都出现在报告中
    headings = re.findall(r"^##\s+(.+)$", skill.output_template, re.M)
    if headings:
        present = [h for h in headings if h in result.report_markdown]
        parts.append(len(present) / len(headings) * 100)
        dim.detail.append(f"章节完整性 {len(present)}/{len(headings)}")

    dim.score = sum(parts) / len(parts)
    return dim


def score_clarity(result: ExecutionResult) -> DimensionScore:
    """
    表达清晰度（启发式）。

    检查三件事：有没有客套废话、篇幅是否失控、有没有幻觉数字。
    这一维天然主观，启发式只能兜底；正式评审仍需专家打分。
    """
    dim = DimensionScore("表达清晰度", 100.0, WEIGHTS["clarity"])
    text = " ".join(result.llm_outputs.values())

    filler = ["希望以上", "希望对您", "仅供参考", "综上所述，我们可以看出", "总而言之"]
    found = [f for f in filler if f in text]
    if found:
        dim.score -= 30
        dim.detail.append(f"❌ 出现客套废话：{found}")

    if len(text) > 4000:
        dim.score -= 20
        dim.detail.append(f"❌ 篇幅过长（{len(text)} 字），信息密度偏低")

    if result.hallucination_flags:
        dim.score -= 30
        dim.detail.append(f"❌ 存在 {len(result.hallucination_flags)} 个疑似幻觉数字")

    dim.score = max(dim.score, 0.0)
    if not dim.detail:
        dim.detail.append("✅ 无客套废话、篇幅合理、无幻觉数字")
    return dim


# --------------------------------------------------------------------------
# 单个用例评测
# --------------------------------------------------------------------------

def evaluate_case(skill: SkillSpec, case_dir: Path) -> CaseScore:
    import yaml

    expected = yaml.safe_load((case_dir / "expected.yaml").read_text(encoding="utf-8"))
    content = (case_dir / "input.csv").read_bytes()
    case_name = f"{case_dir.name}｜{expected.get('name', '')}"

    # ---- 拦截型用例：期望的正确行为是「拒绝执行」 ----
    if expected.get("expect_blocked"):
        try:
            load_and_validate(content, skill)
        except DataValidationError as exc:
            missing = {m["name"] for m in exc.detail.get("missing_required", [])}
            want = set(expected.get("expected_missing_fields", []))
            hint_ok = expected.get("expected_hint_contains", "") in str(exc.detail)
            ok = want <= missing and hint_ok
            return CaseScore(
                case=case_name,
                passed=ok,
                total=100.0 if ok else 0.0,
                blocked_as_expected=True,
                error=None if ok else f"拦截了，但信息不完整：缺失={missing}",
            )
        return CaseScore(
            case=case_name,
            passed=False,
            total=0.0,
            blocked_as_expected=False,
            error="应当拦截却照常执行了 —— 这会产出结论错误的报告",
        )

    # ---- 常规用例 ----
    try:
        df, report = load_and_validate(content, skill)
        result = SkillOrchestrator(skill, df).run(data_warnings=report.warnings)
    except Exception as exc:  # noqa: BLE001
        return CaseScore(case=case_name, passed=False, total=0.0, error=str(exc))

    dims = [
        score_data_accuracy(result, expected),
        score_attribution(result, expected),
        score_actionability(result, expected),
        score_completeness(result, skill, expected),
        score_clarity(result),
    ]
    total = sum(d.weighted for d in dims)
    accuracy = next(d for d in dims if d.name == "数据准确性").score

    return CaseScore(
        case=case_name,
        passed=(total >= PASS_TOTAL and accuracy >= PASS_DATA_ACCURACY),
        total=total,
        dimensions=dims,
    )


def evaluate_skill(skill: SkillSpec, golden_dir: Path) -> dict[str, Any]:
    """跑完一个 Skill 的全部 Golden Case。"""
    cases = sorted(d for d in golden_dir.iterdir() if d.is_dir())
    scores = [evaluate_case(skill, d) for d in cases]
    avg = sum(s.total for s in scores) / len(scores) if scores else 0.0
    return {
        "skill_id": skill.skill_id,
        "skill_name": skill.name,
        "version": skill.version,
        "case_count": len(scores),
        "passed_count": sum(1 for s in scores if s.passed),
        "average_score": round(avg, 1),
        "publishable": all(s.passed for s in scores),
        "cases": [s.to_dict() for s in scores],
    }
