"""
SkillSpec —— 整个平台的中心数据结构。

所有模块都围绕它读写：
  - generator  负责产出它
  - validator  负责校验它
  - executor   负责执行它
  - storage    负责存储它的各个版本

设计要点：analysis_flow 中的步骤分为两类
  - MetricStep: 确定性计算，由 pandas/ast 求值引擎执行，LLM 不参与
  - LLMStep:    模型推理，只做归因与建议，明确禁止计算数值

这条分界线是整套方案可信度的地基。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# 基础枚举
# --------------------------------------------------------------------------

class FieldType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    DATE = "date"
    ENUM = "enum"


class SkillStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


class Domain(str, Enum):
    ECOMMERCE = "电商运营"
    SALES = "销售经营"
    GROWTH = "用户增长"
    RETAIL = "零售门店"


# --------------------------------------------------------------------------
# ④ 输入数据定义
# --------------------------------------------------------------------------

class InputField(BaseModel):
    """
    单个输入字段的契约。

    source_hint 是特意加的：很多 AI 产品死在"用户不知道该给什么数据"，
    而不是死在分析能力不够。明确告诉用户从哪个后台页面导出，
    是 Skill 能否真正落地的关键。
    """

    name: str = Field(description="字段名，如 '曝光人数'")
    type: FieldType = Field(description="字段类型")
    required: bool = Field(default=True, description="是否必填")
    unit: Optional[str] = Field(default=None, description="单位，如 '人' '元' '秒'")
    description: str = Field(description="口径说明，如 '直播间被展示的去重人数'")
    source_hint: Optional[str] = Field(
        default=None,
        description="数据来源提示，如 '抖音罗盘-直播分析-流量看板'",
    )
    enum_values: Optional[list[str]] = Field(
        default=None, description="type 为 enum 时的可选值"
    )


# --------------------------------------------------------------------------
# ⑤ 分析流程 —— 两类步骤
# --------------------------------------------------------------------------

class MetricStep(BaseModel):
    """
    确定性计算步骤。

    由 MetricEngine 用受限 ast 求值器执行，结果 100% 可复现。
    LLM 绝不参与此类步骤的数值产出。
    """

    type: Literal["metric"] = "metric"
    step_id: str = Field(description="步骤唯一标识，如 'S1'")
    name: str = Field(description="步骤名称，如 '计算核心转化漏斗指标'")
    formula: str = Field(
        description="计算表达式，如 '进入率 = 进入直播间人数 / 曝光人数'。"
        "多个指标用换行分隔。"
    )
    inputs: list[str] = Field(
        default_factory=list, description="依赖的输入字段名或上游步骤产出"
    )
    outputs: list[str] = Field(description="本步骤产出的指标名")


class LLMStep(BaseModel):
    """
    模型推理步骤。

    只接收上游 MetricStep 算好的指标结果，不接触原始数据。
    instruction 中必须明确禁止模型自行计算数值。
    """

    type: Literal["llm"] = "llm"
    step_id: str
    name: str = Field(description="步骤名称，如 '定位下滑环节并归因'")
    instruction: str = Field(description="给模型的具体指令")
    inputs: list[str] = Field(
        default_factory=list, description="依赖的上游指标名或前序步骤产出"
    )
    outputs: list[str] = Field(description="本步骤产出的文本块名，供模板引用")
    verify_numbers: bool = Field(
        default=True,
        description=(
            "本步骤的输出是否需要做数字幻觉检测。"
            "陈述事实的步骤（现状描述、归因分析）必须为 true —— 它们说的每个数字都应来自确定性计算；"
            "产出前瞻性内容的步骤（改进建议、目标设定）应为 false —— "
            "「把点击率提升至 15%」里的 15 是目标值而非数据事实，"
            "对这类步骤做幻觉检测只会产生误报"
        ),
    )


AnalysisStep = Annotated[Union[MetricStep, LLMStep], Field(discriminator="type")]


# --------------------------------------------------------------------------
# SkillSpec 主体
# --------------------------------------------------------------------------

class SkillSpec(BaseModel):
    """企业岗位经验 Skill 的完整定义。"""

    # ===== 题目要求的七个字段 =====
    name: str = Field(description="① Skill 名称")
    description: str = Field(description="② Skill 描述")
    use_cases: list[str] = Field(description="③ 使用场景，2-4 条")
    input_schema: list[InputField] = Field(description="④ 输入数据定义")
    analysis_flow: list[AnalysisStep] = Field(description="⑤ 分析流程")
    agent_prompt: str = Field(
        description="⑥ Agent Prompt，必须包含【角色】【分析准则】【禁止事项】三部分"
    )
    output_template: str = Field(
        description="⑦ 输出结果模板，Markdown 格式，用 {{占位符}} 引用步骤产出"
    )

    # ===== 治理所需的补充字段 =====
    # 这两组字段题目没要求，但没有它们 Skill 无法被"管理"：
    #   - metric_dictionary 解决企业内部最容易吵架的指标口径问题
    #   - version/status 解决"没有版本就没有回滚，没有回滚就不敢改"的问题
    skill_id: str = Field(default="", description="资产唯一标识")
    version: str = Field(default="1.0.0")
    status: SkillStatus = Field(default=SkillStatus.DRAFT)
    domain: Domain = Field(default=Domain.ECOMMERCE, description="业务域")
    owner: Optional[str] = Field(default=None, description="资产归属人")
    metric_dictionary: dict[str, str] = Field(
        default_factory=dict,
        description="本 Skill 用到的指标口径显式定义，key 为指标名，value 为口径说明",
    )
    metric_units: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "指标展示单位的显式声明，key 为指标名，value 为 '%' / '元' / '秒' / '件' 等。"
            "系统会按指标名猜测单位，但中文命名并不可靠 —— "
            "「连带率」是 1.4 件/单而不是 140%，靠名字猜必然出错。"
            "单位属于口径的一部分，凡是猜测可能出错的指标都应在此显式声明"
        ),
    )
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    # ---------------- 便捷访问方法 ----------------

    @property
    def required_fields(self) -> list[str]:
        """所有必填输入字段名。"""
        return [f.name for f in self.input_schema if f.required]

    @property
    def all_field_names(self) -> list[str]:
        return [f.name for f in self.input_schema]

    def field_by_name(self, name: str) -> Optional[InputField]:
        return next((f for f in self.input_schema if f.name == name), None)

    @property
    def metric_steps(self) -> list[MetricStep]:
        return [s for s in self.analysis_flow if isinstance(s, MetricStep)]

    @property
    def llm_steps(self) -> list[LLMStep]:
        return [s for s in self.analysis_flow if isinstance(s, LLMStep)]


# --------------------------------------------------------------------------
# 生成链路专用：给 LLM 的输出 Schema
# --------------------------------------------------------------------------

class GeneratedSkill(BaseModel):
    """
    LLM 结构化输出的目标 Schema。

    刻意只包含七个业务字段 + metric_dictionary：
    skill_id / version / created_at 这类由系统生成，不该交给模型。
    """

    name: str
    description: str
    use_cases: list[str]
    input_schema: list[InputField]
    analysis_flow: list[AnalysisStep]
    agent_prompt: str
    output_template: str
    domain: Domain
    metric_dictionary: dict[str, str] = Field(default_factory=dict)
