"""
Skill 生成的 Prompt 设计。

不加约束时，LLM 生成 Skill 最容易犯三个错，每一个都会让 Skill 无法执行或不可信：

  1. 把计算塞进 llm 步骤的 instruction 里，让模型自己算数
     → 数字变成"生成"的而不是"算"的，不可复现，企业不敢用
  2. 在分析流程里引用 input_schema 中不存在的字段
     → Skill 根本跑不起来
  3. 指标口径含糊（只说"转化率"，不说除以什么）
     → 一发布就被不同部门质疑

所以 system prompt 的重心不是"教它怎么写得漂亮"，
而是用硬性约束把这三条路堵死。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.generator.validator import load_metric_dictionary
from app.schemas.skill_spec import Domain

PRESETS_DIR = Path(__file__).resolve().parents[2] / "data" / "presets"

# 模型不该生成的字段（由系统赋值），做 few-shot 时先剔除
_SYSTEM_OWNED_FIELDS = {
    "skill_id", "version", "status", "owner", "created_at", "updated_at",
}


# --------------------------------------------------------------------------
# 业务域识别
# --------------------------------------------------------------------------

_DOMAIN_KEYWORDS: dict[Domain, tuple[str, ...]] = {
    Domain.ECOMMERCE: (
        "直播", "抖音", "快手", "电商", "带货", "GMV", "商品", "店铺",
        "淘宝", "天猫", "拼多多", "转化", "选品", "退货",
    ),
    Domain.SALES: (
        "销售", "商机", "线索", "客户", "签约", "赢单", "漏斗", "CRM",
        "业绩", "回款", "销售额", "大客户",
    ),
    Domain.GROWTH: (
        "增长", "留存", "拉新", "获客", "activation", "激活", "渠道",
        "投放", "LTV", "CAC", "用户增长", "裂变",
    ),
    Domain.RETAIL: (
        "门店", "零售", "坪效", "客流", "导购", "连带", "到店", "商场",
    ),
}


def detect_domain(user_request: str) -> Domain:
    """从用户的自然语言需求里识别业务域，用于注入对应的指标字典。"""
    scores = {
        domain: sum(1 for kw in keywords if kw in user_request)
        for domain, keywords in _DOMAIN_KEYWORDS.items()
    }
    best = max(scores, key=lambda d: scores[d])
    return best if scores[best] > 0 else Domain.ECOMMERCE


# --------------------------------------------------------------------------
# 上下文组装
# --------------------------------------------------------------------------

def render_metric_dictionary(domain: Domain) -> str:
    """把指标字典渲染成 Prompt 片段。模型只能引用，不能发明。"""
    dictionary = load_metric_dictionary()
    metrics: dict[str, Any] = dictionary.get(domain.value, {})
    if not metrics:
        return "（该业务域暂无预置指标字典，你需要在 metric_dictionary 中显式定义所有用到的指标口径）"

    lines: list[str] = []
    for name, meta in metrics.items():
        parts = [f"- {name}：{meta.get('formula', '')}"]
        if meta.get("unit"):
            parts.append(f"单位 {meta['unit']}")
        if meta.get("healthy_range"):
            low, high = meta["healthy_range"]
            parts.append(f"健康区间 {low}~{high}")
        if meta.get("description"):
            parts.append(meta["description"])
        lines.append("，".join(parts))
    return "\n".join(lines)


def load_few_shot_example() -> str:
    """
    取一个高质量的预置 Skill 作为示例。

    完整示例比任何文字描述都更能让模型理解目标结构，
    是提升生成质量性价比最高的一招。
    """
    path = PRESETS_DIR / "douyin_live_review.json"
    if not path.exists():
        return ""
    raw = json.loads(path.read_text(encoding="utf-8"))
    for key in _SYSTEM_OWNED_FIELDS:
        raw.pop(key, None)
    return json.dumps(raw, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# System Prompt
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """你是企业经营分析领域的 Skill 架构师。你的任务是把业务专家的岗位经验，转换为一份结构化、可执行的 SkillSpec。

生成的 Skill 会被一个真实的执行引擎运行：metric 步骤由确定性计算引擎（pandas + 受限表达式求值器）执行，llm 步骤才交给大模型。因此你写的每一条公式都必须真的能跑通。

# 一、硬性约束（违反任意一条，生成结果会被系统拦截）

1. **数值必须由 metric 步骤计算，不允许交给 LLM。**
   任何涉及加减乘除、求和、求平均、比率的计算，type 必须是 "metric"，并写出明确的 formula。
   绝对不允许把"请计算转化率"这类指令写进 llm 步骤的 instruction 里。

2. **分析流程中引用的每个字段，必须已在 input_schema 中定义。**
   不允许凭空出现字段。比如流程里要算退货率，input_schema 里就必须有"退货订单数"。

3. **input_schema 的每个字段都必须给出 description（口径说明）和 source_hint（数据从哪导出）。**
   使用者拿到 Skill 后要知道该准备什么数据、去哪里导出，否则 Skill 无法落地。

4. **agent_prompt 必须包含【角色】【分析准则】【禁止事项】三个章节。**
   其中【禁止事项】至少包含两条：
   - 不得自行计算任何数值，所有数字必须引用上游 metric 步骤的产出
   - 数据不足时必须明确指出缺失项，不得估算或编造

5. **优先使用【可用指标字典】中已定义的指标。**
   如果业务确实需要字典外的新指标，必须在 metric_dictionary 字段中显式写出它的口径定义。

6. **output_template 中的每个 {{占位符}}，必须对应某个 metric 步骤或 llm 步骤的 output 名称。**

# 二、formula 的书写规则（执行引擎的硬性要求）

- 每行一条赋值语句，格式为 `指标名 = 表达式`，多行之间用换行分隔
- 同一个 formula 块内，后面的行可以引用前面刚算出的指标
- 支持的函数**只有**这些，用了别的会被拦截：
  sum、mean、avg、max、min、median、std、count、first、last、abs、round、days_between
- 日期字段不能直接相减。求两个日期的天数差要用 `days_between(结束日期, 开始日期)`，
  例如 `平均销售周期 = mean(days_between(商机关闭日期, 商机创建日期))`
- 支持四则运算和比较运算（可用于产出布尔型的判定结果，如 `流量环节异常 = 进入率 < 0.05`）
- 指标名可以用中文，但**不能包含空格**，也不能以数字开头
- 裸列名参与算术运算时会自动按 sum() 聚合。
  所以 `进入率 = 进入人数 / 曝光人数` 在多行数据下等价于 `sum(进入人数)/sum(曝光人数)`，
  这正是漏斗比率的正确算法。若需要求平均，请显式写 `mean(字段名)`。
- 阈值请写成独立的赋值行（如 `进入率基准下限 = 0.05`），不要硬编码在比较式里，
  这样使用者能在配置界面上直接调整阈值

# 三、分析流程的组织原则

按业务专家的真实思考路径组织，通常遵循五段式：

  测量（算出核心指标）→ 对比（与基准/历史比）→ 定位（找到问题环节）
  → 归因（解释为什么）→ 行动（给出怎么办）

前三段用 metric 步骤（确定性），后两段用 llm 步骤（需要理解和判断）。
典型的步骤数是 3~5 个，不要为了显得复杂而堆砌步骤。

llm 步骤的 instruction 要具体说明"输出什么、用什么格式、不要输出什么"。
特别是行动建议类步骤，必须要求"具体到谁做什么"，明令禁止"提升转化率"这类无法执行的空话。

**每个 llm 步骤都要设置 verify_numbers 字段**，它决定该步骤的输出是否接受数字幻觉检测：
- `true`  —— 陈述事实的步骤（现状描述、诊断、归因）。它们说的每个数字都应来自确定性计算
- `false` —— 产出前瞻性内容的步骤（改进建议、目标设定、行动计划）。
  这类步骤会自然写出「把点击率提升至 15%」「素材控制在 30 秒内」这样的数字，
  它们是目标值和建议参数而非数据事实，做幻觉检测只会产生大量误报

# 四、可用指标字典（{domain}）

{metric_dictionary}

# 五、完整示例（严格参照这个结构和详细程度）

{few_shot}

现在，根据用户的需求生成一份新的 SkillSpec。注意：这是一个**新的**业务场景，不要照抄示例的字段和流程，要针对用户的实际需求重新设计。"""


def build_generation_prompt(user_request: str, domain: Domain | None = None) -> tuple[str, str]:
    """
    组装生成用的 (system, user) 两段 Prompt。

    返回的 system 部分在同一业务域内是稳定的，
    未来接入 prompt caching 可显著降低重复生成的成本。
    """
    domain = domain or detect_domain(user_request)

    system = SYSTEM_PROMPT.format(
        domain=domain.value,
        metric_dictionary=render_metric_dictionary(domain),
        few_shot=load_few_shot_example(),
    )

    user = f"""【用户需求】
{user_request}

【识别到的业务域】
{domain.value}

请生成对应的 SkillSpec。再次提醒几个最容易出错的点：
- 分析流程里用到的每个字段，都要在 input_schema 里定义好
- 所有数值计算写进 metric 步骤的 formula，不要写进 llm 步骤的 instruction
- 每个输入字段都要有 description 和 source_hint
- agent_prompt 要有【禁止事项】章节
- output_template 的占位符要对得上步骤的 outputs"""

    return system, user


# --------------------------------------------------------------------------
# 自检 Prompt（生成后让模型审查自己的产出）
# --------------------------------------------------------------------------

SELF_CRITIQUE_SYSTEM = """你是 Skill 质量审查员。你会收到一份刚生成的 SkillSpec，需要以挑剔的眼光找出其中的问题。

重点检查以下几类问题（按严重程度排序）：

1. **可执行性**：analysis_flow 能否用 input_schema 中的字段真正跑完？有没有引用未定义的字段？
2. **计算归属**：有没有把数值计算混进 llm 步骤的 instruction 里？
3. **口径明确性**：指标定义是否无歧义？"转化率"有没有说清楚分母是什么？
4. **数据可得性**：input_schema 要求的数据，业务方真的能从后台导出吗？source_hint 靠谱吗？
5. **建议可执行性**：llm 步骤是否会产出"提升转化率"这类空话？

只报告真正的问题，不要为了凑数而挑剔措辞。如果没有实质问题，就说"未发现实质问题"。
输出要简洁，每条问题一句话，指明是哪个字段/步骤。"""
