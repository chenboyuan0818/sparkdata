"""
四道校验闸 —— "如何避免 AI 生成错误 Skill" 的落地实现。

防错不靠"让模型更聪明"，靠机制。四道闸依次是：

  闸① 结构校验      字段齐全？类型正确？        （Pydantic）
  闸② 逻辑闭环      引用的字段都定义过吗？       ← 最能抓出真问题的一道
  闸③ 领域规则      指标在白名单内？公式可解析？
  闸④ 冒烟测试      用 mock 数据能真的跑通吗？

设计原则：
  - 报错必须精确到"哪一步、哪个字段、错在哪"，不能只说"生成失败"
  - 区分 error（拦截）和 warning（放行但提示），避免过度拦截
  - 闸②③④ 的失败信息会回喂给模型做定向修正，所以措辞要让模型看得懂
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from app.executor.metrics import MetricEngine, MetricError
from app.schemas.skill_spec import (
    FieldType,
    LLMStep,
    MetricStep,
    SkillSpec,
)

KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "knowledge"

# 公式中允许调用的函数名，不应被当作"未定义变量"报错
_FUNC_NAMES = {
    "sum", "mean", "avg", "max", "min", "median",
    "std", "count", "first", "last", "abs", "round", "days_between",
}


# --------------------------------------------------------------------------
# 校验结果
# --------------------------------------------------------------------------

@dataclass
class GateResult:
    gate: str
    name: str
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "gate": self.gate,
            "name": self.name,
            "passed": self.passed,
            "errors": self.errors,
            "warnings": self.warnings,
        }


@dataclass
class ValidationResult:
    passed: bool
    gates: list[GateResult] = field(default_factory=list)

    @property
    def all_errors(self) -> list[str]:
        return [e for g in self.gates for e in g.errors]

    @property
    def all_warnings(self) -> list[str]:
        return [w for g in self.gates for w in g.warnings]

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "gates": [g.to_dict() for g in self.gates],
            "error_count": len(self.all_errors),
            "warning_count": len(self.all_warnings),
        }

    def feedback_for_model(self) -> str:
        """把失败信息组织成能回喂给模型做定向修正的文本。"""
        lines: list[str] = []
        for gate in self.gates:
            if gate.errors:
                lines.append(f"【{gate.name}】未通过：")
                lines.extend(f"  - {e}" for e in gate.errors)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------

def load_metric_dictionary() -> dict[str, dict[str, Any]]:
    """加载指标口径白名单。缺少 pyyaml 时降级为空字典（闸③ 相应降级为仅告警）。"""
    path = KNOWLEDGE_DIR / "metric_dictionary.yaml"
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError:  # pragma: no cover
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def extract_formula_targets(formula: str) -> list[str]:
    """提取公式中被赋值的指标名（等号左边）。"""
    targets: list[str] = []
    for raw in formula.replace(";", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        targets.append(line.split("=", 1)[0].strip())
    return targets


def iter_formula_lines(formula: str) -> list[tuple[str, list[str]]]:
    """
    按行解析公式块，返回 [(被赋值的指标名, 该行引用的变量名列表), ...]。

    必须逐行返回而不是把整块的引用揉成一堆：公式块内后面的行可以引用
    前面刚算出的指标（MetricEngine 支持块内累积），
    整块比对会把这种合法引用误判成"未定义字段"。
    """
    parsed: list[tuple[str, list[str]]] = []
    for raw in formula.replace(";", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        target, expression = line.split("=", 1)
        target = target.strip()
        refs: list[str] = []
        try:
            tree = ast.parse(expression.strip(), mode="eval")
        except SyntaxError:
            parsed.append((target, refs))
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id not in _FUNC_NAMES:
                if node.id not in refs:
                    refs.append(node.id)
        parsed.append((target, refs))
    return parsed


def extract_formula_references(formula: str) -> list[str]:
    """公式块中引用的全部外部变量（已排除块内自产自销的中间量）。"""
    produced: set[str] = set()
    external: list[str] = []
    for target, refs in iter_formula_lines(formula):
        for ref in refs:
            if ref not in produced and ref not in external:
                external.append(ref)
        produced.add(target)
    return external


def extract_template_placeholders(template: str) -> list[str]:
    """提取 output_template 中的 {{占位符}}。"""
    return [m.strip() for m in re.findall(r"\{\{\s*([^}]+?)\s*\}\}", template)]


def is_intermediate_metric(name: str) -> bool:
    """
    判断是否为无需进入口径字典的中间变量。

    用精确的前后缀匹配而不是宽泛的子串包含：
    早期版本用 `"数" in name` 来跳过计数类变量，结果把
    「直播间健康指数」「相关系数」这类真正的派生指标也一起跳过了，
    导致闸③ 形同虚设。
    """
    # 阈值类：进入率基准下限 / 转化率基准 / 进入率偏离基准
    if any(p in name for p in ("基准", "阈值", "偏离")):
        return True
    # 布尔判定类：流量环节异常 / 线索质量异常 / 目标达成预警
    # 用后缀而不是固定搭配 —— 模型对同一类判定量的命名方式并不统一
    if name.endswith(("异常", "预警", "达标", "健康")):
        return True
    # 原始字段的聚合：总GMV / 总曝光人数
    if name.startswith("总"):
        return True
    # 计数类：分析场次数 / 成交订单数
    if name.endswith(("人数", "订单数", "场次数", "件数", "次数", "单数", "天数")):
        return True
    return False


def _preview(names: set[str], limit: int = 12) -> str:
    """可用变量太多时截断展示，避免错误信息淹没重点（也节省回喂给模型的 token）。"""
    ordered = sorted(names)
    if len(ordered) <= limit:
        return "、".join(ordered)
    return "、".join(ordered[:limit]) + f" 等 {len(ordered)} 项"


def build_mock_dataframe(skill: SkillSpec, rows: int = 3) -> pd.DataFrame:
    """按 input_schema 造一份结构合法的假数据，供闸④ 冒烟测试使用。"""
    data: dict[str, list[Any]] = {}
    for f in skill.input_schema:
        if f.type == FieldType.INTEGER:
            data[f.name] = [1000 * (i + 1) for i in range(rows)]
        elif f.type == FieldType.FLOAT:
            data[f.name] = [100.0 * (i + 1) for i in range(rows)]
        elif f.type == FieldType.DATE:
            data[f.name] = [f"2026-08-0{i + 1}" for i in range(rows)]
        elif f.type == FieldType.ENUM and f.enum_values:
            data[f.name] = [f.enum_values[i % len(f.enum_values)] for i in range(rows)]
        else:
            data[f.name] = [f"{f.name}_{i + 1}" for i in range(rows)]
    return pd.DataFrame(data)


# --------------------------------------------------------------------------
# 闸① 结构校验
# --------------------------------------------------------------------------

def gate1_structure(skill: SkillSpec) -> GateResult:
    """字段齐全性与基本合理性。Pydantic 已保证类型，这里补业务层面的完整性。"""
    result = GateResult(gate="①", name="结构校验", passed=True)

    if not skill.name.strip():
        result.errors.append("Skill 名称为空")
    if len(skill.description.strip()) < 20:
        result.errors.append(
            f"Skill 描述过短（{len(skill.description)} 字），"
            "应说清楚分析什么数据、解决什么问题、产出什么结论"
        )
    if not skill.use_cases:
        result.errors.append("使用场景为空，使用者无法判断该 Skill 是否适用于自己的情况")
    if not skill.input_schema:
        result.errors.append("输入数据定义为空，Skill 无法执行")
    if not skill.analysis_flow:
        result.errors.append("分析流程为空，Skill 没有任何可执行内容")
    if not skill.output_template.strip():
        result.errors.append("输出模板为空")

    # 输入字段的完整性 —— description 缺失会让使用者不知道该给什么数据
    for f in skill.input_schema:
        if not f.description.strip():
            result.errors.append(f"输入字段「{f.name}」缺少口径说明（description）")
        if not f.source_hint:
            result.warnings.append(
                f"输入字段「{f.name}」缺少数据来源提示（source_hint），"
                "使用者可能不知道从哪里导出这个数据"
            )

    # 步骤 ID 唯一性
    step_ids = [s.step_id for s in skill.analysis_flow]
    duplicates = {sid for sid in step_ids if step_ids.count(sid) > 1}
    if duplicates:
        result.errors.append(f"分析流程存在重复的 step_id：{sorted(duplicates)}")

    # 至少要有一个确定性计算步骤 —— 否则数值就交给 LLM 算了，这是本平台的红线
    if not skill.metric_steps:
        result.errors.append(
            "分析流程中没有任何 metric 类型的步骤。"
            "所有数值计算必须由 metric 步骤完成，不允许交给 LLM 生成数字"
        )

    result.passed = not result.errors
    return result


# --------------------------------------------------------------------------
# 闸② 逻辑闭环校验
# --------------------------------------------------------------------------

def gate2_referential_integrity(skill: SkillSpec) -> GateResult:
    """
    检查引用完整性 —— 实践中抓出问题最多的一道闸。

    LLM 最爱犯的错就是在分析流程里凭空引用一个 input_schema 里没有的字段，
    比如流程要算"退货率"但输入定义里根本没有"退货订单数"。
    """
    result = GateResult(gate="②", name="逻辑闭环校验", passed=True)

    available: set[str] = {f.name for f in skill.input_schema}
    produced_by: dict[str, str] = {}

    for step in skill.analysis_flow:
        label = f"步骤 {step.step_id}「{step.name}」"

        if isinstance(step, MetricStep):
            # 逐行推进：块内后面的行可以引用前面刚算出的指标，
            # 所以每处理完一行就把它的产出加入可用集合
            for target, refs in iter_formula_lines(step.formula):
                for ref in refs:
                    if ref not in available:
                        result.errors.append(
                            f"{label} 的公式引用了未定义的字段「{ref}」。"
                            f"请在 input_schema 中定义它，或改用已有字段。"
                            f"当前可用：{_preview(available)}"
                        )
                available.add(target)

            targets = extract_formula_targets(step.formula)
            if not targets:
                result.errors.append(
                    f"{label} 的公式中没有任何 '指标名 = 表达式' 形式的赋值语句"
                )
            # outputs 声明应与公式实际产出一致
            undeclared = [t for t in targets if t not in step.outputs]
            if undeclared:
                result.warnings.append(
                    f"{label} 的公式产出了 {undeclared}，但未在 outputs 中声明"
                )
            missing = [o for o in step.outputs if o not in targets]
            if missing:
                result.errors.append(
                    f"{label} 声明会产出 {missing}，但公式里没有对应的赋值语句"
                )

            for name in targets + step.outputs:
                available.add(name)
                produced_by.setdefault(name, step.step_id)

        elif isinstance(step, LLMStep):
            for ref in step.inputs:
                if ref not in available:
                    result.errors.append(
                        f"{label} 声明依赖「{ref}」，但它既不是输入字段，"
                        f"也不是任何上游步骤的产出"
                    )
            if not step.outputs:
                result.errors.append(f"{label} 未声明任何 outputs，产出无法被模板引用")
            for name in step.outputs:
                available.add(name)
                produced_by.setdefault(name, step.step_id)

    # 输出模板的占位符必须都有来源
    for placeholder in extract_template_placeholders(skill.output_template):
        if placeholder not in available:
            result.errors.append(
                f"输出模板引用了占位符「{{{{{placeholder}}}}}」，"
                f"但没有任何步骤产出这个名字"
            )

    # 孤立产出：算了但没人用（告警，不拦截 —— 可能是留给人看的中间指标）
    used: set[str] = set(extract_template_placeholders(skill.output_template))
    for step in skill.analysis_flow:
        if isinstance(step, LLMStep):
            used.update(step.inputs)
        else:
            used.update(extract_formula_references(step.formula))
    orphans = [n for n in produced_by if n not in used]
    if orphans:
        result.warnings.append(
            f"以下产出未被任何步骤或模板使用：{orphans}（不影响执行，但可能是冗余计算）"
        )

    result.passed = not result.errors
    return result


# --------------------------------------------------------------------------
# 闸③ 领域规则校验
# --------------------------------------------------------------------------

def gate3_domain_rules(skill: SkillSpec) -> GateResult:
    """指标白名单、公式可解析性、Agent Prompt 必备内容。"""
    result = GateResult(gate="③", name="领域规则校验", passed=True)

    # ---- 公式必须能被安全求值器解析 ----
    empty_df = pd.DataFrame({f.name: [1.0] for f in skill.input_schema})
    engine = MetricEngine(empty_df)
    for step in skill.metric_steps:
        for raw in step.formula.replace(";", "\n").split("\n"):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                result.errors.append(
                    f"步骤 {step.step_id} 的公式行缺少 '='：{line!r}。"
                    "正确格式为 '指标名 = 表达式'"
                )
                continue
            expression = line.split("=", 1)[1].strip()
            try:
                ast.parse(expression, mode="eval")
            except SyntaxError as exc:
                result.errors.append(
                    f"步骤 {step.step_id} 的表达式语法错误：{expression!r} —— {exc.msg}"
                )

    # ---- 指标白名单 ----
    dictionary = load_metric_dictionary()
    domain_metrics = set(dictionary.get(skill.domain.value, {}).keys())
    if domain_metrics:
        declared = set(skill.metric_dictionary.keys())
        for step in skill.metric_steps:
            for target in extract_formula_targets(step.formula):
                if is_intermediate_metric(target):
                    continue
                if target not in domain_metrics and target not in declared:
                    result.warnings.append(
                        f"指标「{target}」不在「{skill.domain.value}」的标准口径字典中，"
                        f"也未在 metric_dictionary 中显式定义口径。"
                        f"建议补充定义，否则不同部门可能按各自理解解读"
                    )

    # ---- Agent Prompt 必备内容 ----
    prompt = skill.agent_prompt
    if len(prompt.strip()) < 50:
        result.errors.append("Agent Prompt 过短，无法有效约束模型行为")
    if "禁止" not in prompt:
        result.errors.append(
            "Agent Prompt 缺少【禁止事项】章节。必须至少包含两条："
            "① 不得自行计算任何数值；② 数据不足时不得臆测"
        )
    else:
        if not any(k in prompt for k in ("计算", "数值", "数字")):
            result.warnings.append(
                "Agent Prompt 的禁止事项未明确提到'不得自行计算数值'，"
                "存在模型自己算数的风险"
            )

    result.passed = not result.errors
    return result


# --------------------------------------------------------------------------
# 闸④ 冒烟测试
# --------------------------------------------------------------------------

def gate4_smoke_test(skill: SkillSpec) -> GateResult:
    """
    用 mock 数据真跑一遍 metric 步骤。

    只跑确定性计算部分，不调用 LLM —— 校验阶段不该产生 API 成本，
    而且 metric 步骤跑通了，llm 步骤的输入可用性已由闸② 保证。
    """
    result = GateResult(gate="④", name="冒烟测试", passed=True)

    try:
        df = build_mock_dataframe(skill)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"无法根据 input_schema 构造测试数据：{exc}")
        result.passed = False
        return result

    engine = MetricEngine(df, unit_override=skill.metric_units)
    computed: dict = {}

    for step in skill.metric_steps:
        try:
            computed.update(engine.execute_formula(step.formula, computed))
        except MetricError as exc:
            result.errors.append(
                f"步骤 {step.step_id}「{step.name}」执行失败：{exc}"
            )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(
                f"步骤 {step.step_id}「{step.name}」抛出意外异常：{type(exc).__name__}: {exc}"
            )

    # 模板可渲染性。三类合法来源必须与渲染器保持一致，
    # 否则会出现"闸②放行、闸④拦截"的口径打架
    llm_outputs = {o for s in skill.llm_steps for o in s.outputs}
    raw_fields = set(df.columns)
    for placeholder in extract_template_placeholders(skill.output_template):
        if (
            placeholder not in computed
            and placeholder not in llm_outputs
            and placeholder not in raw_fields
        ):
            result.errors.append(
                f"输出模板的占位符「{placeholder}」在冒烟测试中无法被填充。"
                f"它必须是某个 metric 步骤的产出、某个 llm 步骤的 output，"
                f"或 input_schema 中的字段名"
            )

    result.passed = not result.errors
    return result


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

def validate(skill: SkillSpec) -> ValidationResult:
    """依次通过四道闸。任一闸有 error 即判定不通过。"""
    gates = [
        gate1_structure(skill),
        gate2_referential_integrity(skill),
        gate3_domain_rules(skill),
        gate4_smoke_test(skill),
    ]
    return ValidationResult(passed=all(g.passed for g in gates), gates=gates)
