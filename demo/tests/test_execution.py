"""
执行链路冒烟测试 —— 验证 D3-D4 的成果。

按 MVP 拆解里定的顺序，先打通执行链路再做生成链路：
只有执行引擎存在，"什么样的 Skill 才算合格"才有客观标准。

运行： ./.venv/bin/python -m tests.test_execution
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.executor.loader import DataValidationError, load_and_validate  # noqa: E402
from app.executor.metrics import MetricEngine, UnsafeFormulaError  # noqa: E402
from app.executor.orchestrator import SkillOrchestrator  # noqa: E402
from app.schemas.skill_spec import SkillSpec  # noqa: E402

PRESET = ROOT / "data" / "presets" / "douyin_live_review.json"
SAMPLE = ROOT / "data" / "samples" / "douyin_live_7days.csv"

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  ✅ {name}")
    else:
        FAILED.append(f"{name} — {detail}")
        print(f"  ❌ {name}  {detail}")


def load_skill() -> SkillSpec:
    return SkillSpec.model_validate(json.loads(PRESET.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------

def test_schema_parsing() -> SkillSpec:
    print("\n[1] SkillSpec 解析")
    skill = load_skill()
    check("七个字段齐全", all([
        skill.name, skill.description, skill.use_cases,
        skill.input_schema, skill.analysis_flow,
        skill.agent_prompt, skill.output_template,
    ]))
    check("metric / llm 步骤被正确区分",
          len(skill.metric_steps) == 2 and len(skill.llm_steps) == 2,
          f"metric={len(skill.metric_steps)} llm={len(skill.llm_steps)}")
    # 8 个字段中只有「直播日期」是选填
    check("必填字段识别正确", len(skill.required_fields) == 7,
          f"实际 {len(skill.required_fields)}：{skill.required_fields}")
    return skill


def test_data_loading(skill: SkillSpec):
    print("\n[2] 数据加载与校验")
    df, report = load_and_validate(SAMPLE.read_bytes(), skill)
    check("CSV 读取成功", len(df) == 7, f"实际 {len(df)} 行")
    check("字段自动映射完整", len(report.mapping) == 8, f"映射了 {len(report.mapping)} 个")
    check("校验通过", report.ok)
    return df


def test_missing_field_blocks(skill: SkillSpec):
    print("\n[3] 必填字段缺失时必须拦截执行")
    broken = b"\n".join([
        "场次ID,曝光人数".encode(),
        "LIVE-001,100000".encode(),
    ])
    try:
        load_and_validate(broken, skill)
        check("缺字段时抛出 DataValidationError", False, "居然没报错，属于严重问题")
    except DataValidationError as exc:
        missing = {m["name"] for m in exc.detail["missing_required"]}
        check("缺字段时抛出 DataValidationError", True)
        check("准确列出所有缺失字段",
              {"进入直播间人数", "平均停留时长", "商品点击人数", "成交订单数", "GMV"} <= missing,
              f"实际报出 {missing}")
        check("给出数据来源提示",
              all(m["source_hint"] != "未提供来源提示"
                  for m in exc.detail["missing_required"]))


def test_metric_correctness(skill: SkillSpec, df):
    print("\n[4] 指标计算正确性（人工核算比对）")
    engine = MetricEngine(df)
    results = engine.execute_formula(skill.metric_steps[0].formula, {})

    # 人工核算：sum(进入) / sum(曝光)
    expected_entry_rate = df["进入直播间人数"].sum() / df["曝光人数"].sum()
    expected_gmv = df["GMV"].sum()
    expected_stay = df["平均停留时长"].mean()

    check("进入率 = sum(进入)/sum(曝光)",
          abs(results["进入率"].value - expected_entry_rate) < 1e-9,
          f"{results['进入率'].value} vs {expected_entry_rate}")
    check("总GMV 聚合正确",
          abs(results["总GMV"].value - expected_gmv) < 1e-6)
    check("平均停留使用 mean 而非 sum",
          abs(results["平均停留秒数"].value - expected_stay) < 1e-9)
    check("溯源信息完整",
          results["进入率"].depends_on == ["进入直播间人数", "曝光人数"]
          and results["进入率"].row_count == 7,
          str(results["进入率"].to_dict()))
    return results


def test_threshold_step(skill: SkillSpec, df, upstream):
    print("\n[5] 阈值判定步骤（可引用上游指标）")
    engine = MetricEngine(df)
    results = engine.execute_formula(skill.metric_steps[1].formula, upstream)
    check("布尔判定产出正确类型",
          isinstance(results["流量环节异常"].value, bool),
          f"实际类型 {type(results['流量环节异常'].value)}")
    entry_rate = upstream["进入率"].value
    check("流量环节判定与实际数值一致",
          results["流量环节异常"].value == (entry_rate < 0.05),
          f"进入率 {entry_rate:.4f}")
    return results


def test_formula_safety():
    print("\n[6] 公式安全性（ast 白名单必须拦截危险表达式）")
    import pandas as pd
    engine = MetricEngine(pd.DataFrame({"a": [1, 2, 3]}))

    dangerous = [
        ("导入模块", "x = __import__('os').system('ls')"),
        ("属性访问", "x = a.__class__.__bases__"),
        ("内置函数", "x = eval('1+1')"),
        ("文件读取", "x = open('/etc/passwd')"),
    ]
    for label, formula in dangerous:
        try:
            engine.execute_formula(formula, {})
            check(f"拦截「{label}」", False, "危险表达式竟然执行成功了")
        except Exception as exc:
            check(f"拦截「{label}」",
                  isinstance(exc, UnsafeFormulaError) or "不被允许" in str(exc)
                  or "未知函数" in str(exc) or "未定义" in str(exc),
                  f"异常类型 {type(exc).__name__}: {exc}")


def test_div_by_zero():
    print("\n[7] 除零保护")
    import pandas as pd
    engine = MetricEngine(pd.DataFrame({"分子": [10], "分母": [0]}))
    results = engine.execute_formula("比率 = 分子 / 分母", {})
    check("除零返回 0 而非崩溃", results["比率"].value == 0.0)


def test_full_execution(skill: SkillSpec, df):
    print("\n[8] 端到端执行（mock 模式，验证全链路打通）")
    result = SkillOrchestrator(skill, df).run()

    check("全部 4 个步骤都被执行", len(result.traces) == 4)
    check("无失败步骤",
          all(t.status == "ok" for t in result.traces),
          str([(t.step_id, t.status, t.error) for t in result.traces]))
    check("报告已生成", len(result.report_markdown) > 200)
    check("占位符全部被替换",
          "⚠️ 未找到占位符" not in result.report_markdown,
          "存在未匹配的占位符")
    check("报告含数据说明与局限章节", "数据说明与局限" in result.report_markdown)
    # 断言「声明的产出都被真的算出来了」，而不是写死一个魔法数字 ——
    # 后者会在每次给 Skill 加指标时失效，且失效了也说明不了任何问题
    declared = {o for s in skill.metric_steps for o in s.outputs}
    missing = declared - set(result.metrics)
    check("metric 步骤声明的产出全部算出", not missing, f"缺失 {missing}")

    check("每个指标都带溯源信息",
          all(m.formula and m.row_count > 0 for m in result.metrics.values()))
    return result


def test_hallucination_detection(skill: SkillSpec, df):
    print("\n[9] 数字幻觉检测")
    from app.executor.orchestrator import detect_hallucinated_numbers

    engine = MetricEngine(df)
    metrics = engine.execute_formula(skill.metric_steps[0].formula, {})

    real_gmv = metrics["总GMV"].display()
    clean = f"本周总 GMV 为 {real_gmv} 元，整体表现平稳。"
    check("引用真实计算值时不误报",
          len(detect_hallucinated_numbers(clean, metrics)) == 0,
          str(detect_hallucinated_numbers(clean, metrics)))

    check("引用百分比形式时不误报（0.0668 → 6.68%）",
          len(detect_hallucinated_numbers("进入率为 6.68%", metrics)) == 0)

    check("引用四舍五入形式时不误报（186000.5 → 186001）",
          len(detect_hallucinated_numbers("总 GMV 约 186001 元", metrics)) == 0)

    # 每个捏造数字单独测。早期版本把大数字和小数字混在一句里测，
    # 结果只有小数字被抓到，大数字长期漏网却让测试显示通过。
    fabricated = {
        "大额捏造（无逗号）": "本周总 GMV 为 999888.77 元",
        "大额捏造（带千分位）": "本周总 GMV 为 999,888.77 元",
        "中等捏造": "共触达 45231 名用户",
        "比率捏造": "同比增长 47.3%",
    }
    for label, text in fabricated.items():
        flags = detect_hallucinated_numbers(text, metrics)
        check(f"捏造数字能检出：{label}", len(flags) >= 1, f"检出 {len(flags)} 个")


# --------------------------------------------------------------------------

def main() -> int:
    print("=" * 62)
    print("执行链路冒烟测试")
    print("=" * 62)

    skill = test_schema_parsing()
    df = test_data_loading(skill)
    test_missing_field_blocks(skill)
    upstream = test_metric_correctness(skill, df)
    test_threshold_step(skill, df, upstream)
    test_formula_safety()
    test_div_by_zero()
    result = test_full_execution(skill, df)
    test_hallucination_detection(skill, df)

    print("\n" + "=" * 62)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("\n失败明细：")
        for item in FAILED:
            print(f"  - {item}")
        return 1

    print("\n" + "-" * 62)
    print("报告预览（前 40 行）")
    print("-" * 62)
    for line in result.report_markdown.split("\n")[:40]:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
