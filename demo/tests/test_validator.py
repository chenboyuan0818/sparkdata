"""
四道校验闸测试 —— 故意把 Skill 弄坏，验证每道闸能抓住它该抓的那类错误。

这是"如何避免 AI 生成错误 Skill"的可执行证据：
不是靠说"我们做了校验"，而是每类错误都有一个能复现的反例。

运行： ./.venv/bin/python -m tests.test_validator
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.generator.validator import validate  # noqa: E402
from app.schemas.skill_spec import SkillSpec  # noqa: E402

PRESET = ROOT / "data" / "presets" / "douyin_live_review.json"

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  ✅ {name}")
    else:
        FAILED.append(f"{name} — {detail}")
        print(f"  ❌ {name}  {detail}")


def load_raw() -> dict:
    return json.loads(PRESET.read_text(encoding="utf-8"))


def gate_of(result, gate_symbol: str):
    return next(g for g in result.gates if g.gate == gate_symbol)


# --------------------------------------------------------------------------

def test_healthy_skill_passes():
    print("\n[基准] 完好的 Skill 应当四闸全绿")
    skill = SkillSpec.model_validate(load_raw())
    result = validate(skill)
    check("整体通过", result.passed, str(result.all_errors[:2]))
    check("零错误", len(result.all_errors) == 0)
    check("零告警", len(result.all_warnings) == 0, str(result.all_warnings))


def test_gate1_catches_structural_defects():
    print("\n[闸①] 结构缺陷")
    raw = load_raw()
    raw["use_cases"] = []
    raw["description"] = "复盘"                      # 过短
    raw["input_schema"][0]["description"] = ""       # 缺口径说明
    # 把两个 metric 步骤全删掉 —— 数值就没人算了，这是平台红线
    raw["analysis_flow"] = [s for s in raw["analysis_flow"] if s["type"] == "llm"]

    result = validate(SkillSpec.model_validate(raw))
    g1 = gate_of(result, "①")
    errors = " ".join(g1.errors)

    check("闸① 判定未通过", not g1.passed)
    check("抓出「使用场景为空」", "使用场景为空" in errors)
    check("抓出「描述过短」", "描述过短" in errors)
    check("抓出「字段缺少口径说明」", "缺少口径说明" in errors)
    check("抓出「没有 metric 步骤」", "没有任何 metric" in errors,
          "这是红线：数值必须由确定性计算产出")


def test_gate2_catches_undefined_field():
    print("\n[闸②] 分析流程引用了 input_schema 里没有的字段")
    print("      （这是 LLM 生成 Skill 时最常犯的错）")
    raw = load_raw()
    # 流程里要算退货率，但输入定义里根本没有「退货订单数」这个字段
    raw["analysis_flow"][0]["formula"] += "\n退货率 = 退货订单数 / 成交订单数"
    raw["analysis_flow"][0]["outputs"].append("退货率")

    result = validate(SkillSpec.model_validate(raw))
    g2 = gate_of(result, "②")
    errors = " ".join(g2.errors)

    check("闸② 判定未通过", not g2.passed)
    check("精确指出是哪个字段", "退货订单数" in errors, errors[:200])
    check("指明是哪个步骤出的问题", "S1" in errors)
    check("给出可用字段供修正", "当前可用" in errors)


def test_gate2_catches_dangling_placeholder():
    print("\n[闸②] 输出模板引用了没有任何步骤产出的占位符")
    raw = load_raw()
    raw["output_template"] += "\n\n## 四、竞品对比\n\n{{竞品对比结论}}"

    result = validate(SkillSpec.model_validate(raw))
    g2 = gate_of(result, "②")
    check("闸② 判定未通过", not g2.passed)
    check("指出是哪个占位符", "竞品对比结论" in " ".join(g2.errors))


def test_gate2_allows_intra_block_reference():
    print("\n[闸②] 不应误判：公式块内后面的行引用前面刚算出的指标")
    raw = load_raw()
    raw["analysis_flow"][0]["formula"] += (
        "\n中间量 = 总GMV / 2\n最终量 = 中间量 * 3"
    )
    raw["analysis_flow"][0]["outputs"].extend(["中间量", "最终量"])
    raw["output_template"] += "\n\n{{最终量}}"

    result = validate(SkillSpec.model_validate(raw))
    g2 = gate_of(result, "②")
    check("块内引用不被误判为未定义", g2.passed, str(g2.errors))


def test_gate3_catches_missing_prohibitions():
    print("\n[闸③] Agent Prompt 缺少【禁止事项】")
    raw = load_raw()
    raw["agent_prompt"] = (
        "你是一位资深的抖音直播运营专家，请根据数据分析直播间的表现，"
        "找出问题并给出改进建议。要专业、务实、有针对性。"
    )

    result = validate(SkillSpec.model_validate(raw))
    g3 = gate_of(result, "③")
    errors = " ".join(g3.errors)

    check("闸③ 判定未通过", not g3.passed)
    check("要求补充禁止事项", "禁止事项" in errors)
    check("说明必须包含哪两条", "不得自行计算" in errors)


def test_gate3_catches_bad_formula_syntax():
    print("\n[闸③] 公式语法错误")
    raw = load_raw()
    raw["analysis_flow"][0]["formula"] += "\n错误指标 = 曝光人数 / / 2"

    result = validate(SkillSpec.model_validate(raw))
    g3 = gate_of(result, "③")
    check("闸③ 判定未通过", not g3.passed)
    check("指出语法错误", "语法错误" in " ".join(g3.errors))


def test_gate3_warns_on_unknown_metric():
    print("\n[闸③] 使用了不在字典白名单里、也未定义口径的指标")
    raw = load_raw()
    raw["analysis_flow"][0]["formula"] += "\n直播间健康指数 = 总GMV / 100"
    raw["analysis_flow"][0]["outputs"].append("直播间健康指数")
    raw["output_template"] += "\n\n{{直播间健康指数}}"

    result = validate(SkillSpec.model_validate(raw))
    g3 = gate_of(result, "③")
    warnings = " ".join(g3.warnings)

    check("以告警而非错误的形式提示", g3.passed and len(g3.warnings) > 0,
          "自创指标应放行但提示，不该直接拦截")
    check("指出是哪个指标", "直播间健康指数" in warnings)
    check("说明风险是口径不统一", "口径" in warnings)


def test_gate4_catches_runtime_failure():
    print("\n[闸④] 静态检查通过、但真跑起来会崩的公式")
    raw = load_raw()
    # round() 是白名单函数、语法也合法，但传三个参数运行时会 TypeError
    raw["analysis_flow"][0]["formula"] += "\n崩溃指标 = round(曝光人数, 2, 3)"
    raw["analysis_flow"][0]["outputs"].append("崩溃指标")
    raw["output_template"] += "\n\n{{崩溃指标}}"

    result = validate(SkillSpec.model_validate(raw))
    g2, g3, g4 = gate_of(result, "②"), gate_of(result, "③"), gate_of(result, "④")

    check("闸②③ 静态检查放行（因为语法和引用都没问题）", g2.passed and g3.passed)
    check("闸④ 冒烟测试拦下", not g4.passed,
          "这正是保留冒烟测试作为最后一道闸的意义")
    check("指出是哪个步骤崩的", "S1" in " ".join(g4.errors))


def test_feedback_is_usable_by_model():
    print("\n[回喂] 校验失败信息应可直接回喂给模型做定向修正")
    raw = load_raw()
    raw["analysis_flow"][0]["formula"] += "\n退货率 = 退货订单数 / 成交订单数"
    raw["analysis_flow"][0]["outputs"].append("退货率")

    result = validate(SkillSpec.model_validate(raw))
    feedback = result.feedback_for_model()

    check("生成了回喂文本", len(feedback) > 0)
    check("包含闸的名称", "逻辑闭环校验" in feedback)
    check("包含具体错误", "退货订单数" in feedback)
    check("长度可控（不会撑爆 token）", len(feedback) < 3000, f"{len(feedback)} 字符")


# --------------------------------------------------------------------------

def main() -> int:
    print("=" * 62)
    print("四道校验闸测试")
    print("=" * 62)

    test_healthy_skill_passes()
    test_gate1_catches_structural_defects()
    test_gate2_catches_undefined_field()
    test_gate2_catches_dangling_placeholder()
    test_gate2_allows_intra_block_reference()
    test_gate3_catches_missing_prohibitions()
    test_gate3_catches_bad_formula_syntax()
    test_gate3_warns_on_unknown_metric()
    test_gate4_catches_runtime_failure()
    test_feedback_is_usable_by_model()

    print("\n" + "=" * 62)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("\n失败明细：")
        for item in FAILED:
            print(f"  - {item}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
