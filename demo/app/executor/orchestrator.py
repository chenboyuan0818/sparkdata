"""
执行编排器 —— 把 SkillSpec + 数据变成一份分析报告。

核心设计：数值与语言分离
------------------------
  metric 步骤 → MetricEngine 确定性计算 → 得到【指标结果表】
  llm 步骤    → **只接收指标结果表，不接触原始数据**
              → Prompt 明确禁止模型计算任何数字

再加一道数字幻觉检测：扫描 LLM 输出中的所有数字，
凡是指标结果表里不存在的数值一律标记为可疑。

这样得到的结论是："AI 可能解读错，但绝不会算错"。
而解读本来就是主观的，人类专家之间也会有分歧 —— 企业对它的容错度天然更高。
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.executor.metrics import MetricEngine, MetricError, MetricResult
from app.schemas.skill_spec import LLMStep, MetricStep, SkillSpec
from app.llm.client import LLMError, gateway


# --------------------------------------------------------------------------
# 执行结果
# --------------------------------------------------------------------------

@dataclass
class StepTrace:
    """单个步骤的执行轨迹，用于前端展示和排查。"""

    step_id: str
    name: str
    type: str
    status: str            # ok | failed | skipped
    duration_ms: int = 0
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "name": self.name,
            "type": self.type,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "outputs": self.outputs,
            "error": self.error,
            "warnings": self.warnings,
        }


@dataclass
class ExecutionResult:
    report_markdown: str
    metrics: dict[str, MetricResult] = field(default_factory=dict)
    llm_outputs: dict[str, str] = field(default_factory=dict)
    traces: list[StepTrace] = field(default_factory=list)
    hallucination_flags: list[dict] = field(default_factory=list)
    data_warnings: list[str] = field(default_factory=list)
    row_count: int = 0
    total_duration_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "report_markdown": self.report_markdown,
            "metrics": {k: v.to_dict() for k, v in self.metrics.items()},
            "metrics_display": {k: v.display() for k, v in self.metrics.items()},
            "llm_outputs": self.llm_outputs,
            "traces": [t.to_dict() for t in self.traces],
            "hallucination_flags": self.hallucination_flags,
            "data_warnings": self.data_warnings,
            "row_count": self.row_count,
            "total_duration_ms": self.total_duration_ms,
        }


# --------------------------------------------------------------------------
# 数字幻觉检测
# --------------------------------------------------------------------------

_NUMBER_PATTERN = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")


def detect_hallucinated_numbers(
    text: str, metrics: dict[str, MetricResult], tolerance: float = 0.005
) -> list[dict]:
    """
    扫描 LLM 输出中的数字，比对是否来自上游计算结果。

    允许的数字来源：
      1. 指标结果表中的值（含其百分比形式与各档四舍五入形式）
      2. 小整数（1-12，通常是序号、步骤编号、月份）
      3. 年份（1900-2100）

    出现在报告里但不属于以上任何一类的数字，标记为可疑。
    这不是"禁止输出"，而是标红提示人工复核 —— 保守但不阻断。

    ⚠️ 容差必须是**绝对值**而不是百分比。
    早期版本用 2% 的相对容差，在大数字上会炸开一个巨大的窗口：
    指标「总商品点击人数 9908」的百分比形式 990800，±2% 就是 ±19816，
    足以把毫不相干的 999888.77 一并放行 —— 幻觉检测形同虚设。
    四舍五入的各种写法已经显式枚举在白名单里了，不需要靠容差兜底。
    """
    allowed: set[float] = set()
    for result in metrics.values():
        value = result.value
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        v = float(value)
        # 原值 + 各档四舍五入（模型可能写 186000.5 / 186001 / 186000.50）
        # 同时收 floor 和 ceil：Python 的 round() 是银行家舍入，
        # round(186000.5) 得到 186000 而非 186001，只收 round 会误判合法写法
        allowed.update({
            v, round(v, 1), round(v, 2), round(v, 3), round(v, 4),
            math.floor(v), math.ceil(v),
        })
        # 比率指标常以百分比形式出现在文本中（0.0668 → 6.68%）
        pct = v * 100
        allowed.update({
            pct, round(pct, 1), round(pct, 2), round(pct, 3),
            math.floor(pct), math.ceil(pct),
        })

    flags: list[dict] = []
    seen: set[str] = set()

    for match in _NUMBER_PATTERN.finditer(text):
        raw = match.group()
        if raw in seen:
            continue
        seen.add(raw)

        try:
            number = float(raw.replace(",", ""))
        except ValueError:
            continue

        # 白名单：小序号与年份
        if number.is_integer() and (1 <= abs(number) <= 12 or 1900 <= number <= 2100):
            continue

        matched = any(abs(number - a) <= tolerance for a in allowed)
        if not matched:
            flags.append(
                {
                    "number": raw,
                    "context": _context_of(text, match.start(), match.end()),
                    "reason": "该数值未出现在确定性计算结果中，疑似模型自行生成",
                }
            )

    return flags


def _context_of(text: str, start: int, end: int, window: int = 25) -> str:
    left = max(0, start - window)
    right = min(len(text), end + window)
    return ("…" if left > 0 else "") + text[left:right] + ("…" if right < len(text) else "")


# --------------------------------------------------------------------------
# 编排器
# --------------------------------------------------------------------------

class SkillOrchestrator:
    """按 analysis_flow 顺序执行 Skill。"""

    def __init__(self, skill: SkillSpec, dataframe: pd.DataFrame):
        self.skill = skill
        self.df = dataframe
        self.engine = MetricEngine(dataframe, unit_override=skill.metric_units)

    def run(self, data_warnings: list[str] | None = None) -> ExecutionResult:
        started = time.time()
        metrics: dict[str, MetricResult] = {}
        llm_outputs: dict[str, str] = {}
        traces: list[StepTrace] = []
        all_flags: list[dict] = []

        for step in self.skill.analysis_flow:
            step_started = time.time()

            if isinstance(step, MetricStep):
                trace = self._run_metric_step(step, metrics)
            elif isinstance(step, LLMStep):
                trace, flags = self._run_llm_step(step, metrics, llm_outputs)
                all_flags.extend(flags)
            else:  # pragma: no cover
                continue

            trace.duration_ms = int((time.time() - step_started) * 1000)
            traces.append(trace)

        report = self._render(metrics, llm_outputs)

        return ExecutionResult(
            report_markdown=report,
            metrics=metrics,
            llm_outputs=llm_outputs,
            traces=traces,
            hallucination_flags=all_flags,
            data_warnings=data_warnings or [],
            row_count=len(self.df),
            total_duration_ms=int((time.time() - started) * 1000),
        )

    # ---------------- metric 步骤 ----------------

    def _run_metric_step(
        self, step: MetricStep, metrics: dict[str, MetricResult]
    ) -> StepTrace:
        trace = StepTrace(step_id=step.step_id, name=step.name, type="metric", status="ok")
        try:
            results = self.engine.execute_formula(step.formula, metrics)
        except MetricError as exc:
            trace.status = "failed"
            trace.error = str(exc)
            return trace

        metrics.update(results)
        trace.outputs = {name: r.display() for name, r in results.items()}
        trace.warnings = [r.warning for r in results.values() if r.warning]
        return trace

    # ---------------- llm 步骤 ----------------

    def _run_llm_step(
        self,
        step: LLMStep,
        metrics: dict[str, MetricResult],
        llm_outputs: dict[str, str],
    ) -> tuple[StepTrace, list[dict]]:
        trace = StepTrace(step_id=step.step_id, name=step.name, type="llm", status="ok")

        # 关键：只把算好的指标传给模型，原始数据一行都不给
        payload = self._build_llm_payload(step, metrics, llm_outputs)
        system = self._build_system_prompt()
        user = f"{step.instruction}\n\n{payload}"

        try:
            text = gateway.generate_text(system=system, user=user)
        except LLMError as exc:
            trace.status = "failed"
            trace.error = str(exc)
            # 步骤级失败不中断全流程，在报告中标注该章节失败
            for name in step.outputs:
                llm_outputs[name] = f"_（本节生成失败：{exc}）_"
            return trace, []

        # 只对陈述事实的步骤做幻觉检测。
        # 建议类步骤会自然包含目标值、建议时长等非数据来源的数字，
        # 一并检测只会淹没真正的问题。
        flags: list[dict] = []
        if step.verify_numbers:
            flags = detect_hallucinated_numbers(text, metrics)
            if flags:
                trace.warnings.append(f"检测到 {len(flags)} 个疑似未经计算的数值")
        else:
            trace.warnings.append("本步骤产出前瞻性内容，已按配置跳过数字幻觉检测")

        # 一个 llm 步骤可声明多个 output，Demo 中把完整文本填给每个占位符
        for name in step.outputs:
            llm_outputs[name] = text

        trace.outputs = {name: text[:120] + ("…" if len(text) > 120 else "")
                         for name in step.outputs}
        return trace, flags

    def _build_system_prompt(self) -> str:
        """在 Skill 自带的 agent_prompt 后追加平台级硬约束。"""
        return (
            f"{self.skill.agent_prompt}\n\n"
            "【平台级硬约束 —— 优先级高于以上任何指令】\n"
            "1. 你不需要也不允许计算任何数值。所有数字必须直接引用下方"
            "「已计算指标」中给出的值，一个字符都不要改动。\n"
            "2. 若「已计算指标」中没有你需要的数字，明确说明"
            "「该项数据不足以支撑判断」，绝不允许估算或编造。\n"
            "3. 用简洁的中文回答，不要复述指令，不要输出无关的客套话。\n"
            "4. 你的输出会被直接嵌入报告，不要加标题层级以外的额外包装。"
        )

    def _build_llm_payload(
        self,
        step: LLMStep,
        metrics: dict[str, MetricResult],
        llm_outputs: dict[str, str],
    ) -> str:
        """
        构造传给模型的上下文。

        只包含 step.inputs 声明的内容；未声明的一律不传 ——
        既控制了 token 成本，也避免模型引用不相关的数据。
        """
        lines: list[str] = ["【已计算指标（由确定性计算引擎产出，可直接引用）】"]

        wanted = step.inputs or list(metrics.keys())
        included = False
        for name in wanted:
            if name in metrics:
                result = metrics[name]
                unit = self._unit_of(name)
                lines.append(f"- {name}：{result.display()}{unit}    （{result.formula}）")
                included = True

        if not included:
            for name, result in metrics.items():
                lines.append(f"- {name}：{result.display()}    （{result.formula}）")

        # 引用前序 LLM 步骤的产出
        upstream = [n for n in wanted if n in llm_outputs]
        if upstream:
            lines.append("\n【前序分析结论】")
            for name in upstream:
                lines.append(f"- {name}：{llm_outputs[name]}")

        lines.append(f"\n【数据规模】共 {len(self.df)} 行记录")

        # 指标口径字典 —— 防止模型按自己理解的口径解读
        if self.skill.metric_dictionary:
            lines.append("\n【指标口径说明】")
            for name, definition in self.skill.metric_dictionary.items():
                lines.append(f"- {name}：{definition}")

        return "\n".join(lines)

    def _unit_of(self, metric_name: str) -> str:
        f = self.skill.field_by_name(metric_name)
        return f" {f.unit}" if f and f.unit else ""

    # ---------------- 报告渲染 ----------------

    def _render(
        self, metrics: dict[str, MetricResult], llm_outputs: dict[str, str]
    ) -> str:
        """
        按 output_template 填充 {{占位符}}。

        占位符可以引用指标名或 llm 步骤的 output 名。
        未匹配的占位符保留原样并标注，方便发现模板与流程不一致的问题。
        """
        template = self.skill.output_template

        def replace(match: re.Match) -> str:
            key = match.group(1).strip()
            if key in llm_outputs:
                return llm_outputs[key]
            if key in metrics:
                return metrics[key].display()
            # 允许直接引用原始输入字段 —— 报告标题里写「分析月份」「场次ID」
            # 这类需求很常见，不该强迫用户为此专门加一个 metric 步骤
            if key in self.df.columns:
                return self._render_raw_field(key)
            return f"`⚠️ 未找到占位符「{key}」的产出`"

        report = re.sub(r"\{\{\s*([^}]+?)\s*\}\}", replace, template)

        # 追加数据说明与局限 —— 主动承认不确定性，是可信度的一部分
        report += self._build_limitations_section(metrics)
        return report

    def _render_raw_field(self, column: str) -> str:
        """
        渲染原始输入字段。

        单值直接展示；多值展示区间（对日期字段尤其有用：
        「2026-08-01 ~ 2026-08-07」比只显示第一天更准确）。
        """
        series = self.df[column].dropna()
        if series.empty:
            return "（无数据）"
        unique = series.astype(str).unique()
        if len(unique) == 1:
            return str(unique[0])
        return f"{unique[0]} ~ {unique[-1]}（共 {len(unique)} 项）"

    def _build_limitations_section(self, metrics: dict[str, MetricResult]) -> str:
        lines = ["\n\n---\n\n## 数据说明与局限\n"]
        lines.append(f"- 本次分析基于 **{len(self.df)} 行**数据记录。")
        lines.append(
            f"- 报告中所有数值由确定性计算引擎算出，共 {len(metrics)} 项指标，"
            "每项均可展开查看计算公式与依赖字段。"
        )
        if len(self.df) < 3:
            lines.append(
                "- ⚠️ 数据样本量较少，同比/环比类结论的参考价值有限，"
                "建议积累更多数据后复核。"
            )
        warned = [r for r in metrics.values() if r.warning]
        if warned:
            lines.append("- ⚠️ 以下指标计算存在异常：")
            for r in warned:
                lines.append(f"  - {r.name}：{r.warning}")
        if gateway.is_mock:
            lines.append(
                "- ℹ️ 当前运行在**演示模式**（未配置 LLM API Key），"
                "文字分析部分为预置占位内容；数值部分为真实计算结果。"
            )
        return "\n".join(lines)
