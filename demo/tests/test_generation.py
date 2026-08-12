"""
生成链路真实调用测试。

会真的调用 LLM API（haiku 单次约 $0.03）。
故意用销售场景，而 few-shot 给的是电商直播 —— 用来检验模型是在
针对新需求重新设计，还是在照抄示例。

运行： ./.venv/bin/python -m tests.test_generation
      ./.venv/bin/python -m tests.test_generation "帮我做个门店客流分析 Skill"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.executor.orchestrator import SkillOrchestrator  # noqa: E402
from app.generator.generator import SkillGenerator  # noqa: E402
from app.generator.validator import build_mock_dataframe  # noqa: E402
from app.llm.client import gateway  # noqa: E402

DEFAULT_REQUEST = "帮我创建一个销售团队月度业绩复盘 Skill"


def main() -> int:
    request = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REQUEST

    print("=" * 66)
    print("Skill 生成链路测试")
    print("=" * 66)
    print(f"\n模型   ：{gateway.status()['model']}")
    print(f"需求   ：{request}\n")

    if gateway.is_mock:
        print("⚠️ 当前为演示模式，无法测试生成链路。请先配置 API Key。")
        return 1

    print("生成中……（含四道校验闸，失败会带错误信息自动重试）\n")
    result = SkillGenerator(max_attempts=3).generate(request)

    # ---- 生成过程 ----
    print("-" * 66)
    print("生成过程")
    print("-" * 66)
    for a in result.attempts:
        mark = "✅" if a.ok else "❌"
        print(f"  第 {a.attempt} 次尝试  {mark}  耗时 {a.duration_ms / 1000:.1f}s")
        if a.error:
            print(f"      模型调用失败：{a.error}")
        if a.validation and not a.validation["passed"]:
            for gate in a.validation["gates"]:
                if gate["errors"]:
                    print(f"      闸{gate['gate']} {gate['name']} 未通过：")
                    for e in gate["errors"][:3]:
                        print(f"        · {e[:110]}")

    if result.skill is None:
        print(f"\n❌ 生成失败：{result.error}")
        return 1

    skill = result.skill

    # ---- 校验结果 ----
    print("\n" + "-" * 66)
    print("四道校验闸")
    print("-" * 66)
    for gate in result.validation.gates:
        mark = "✅" if gate.passed else "❌"
        print(f"  {mark} 闸{gate.gate} {gate.name}"
              f"  （{len(gate.errors)} 错误 / {len(gate.warnings)} 告警）")
        for w in gate.warnings[:2]:
            print(f"       WARN: {w[:100]}")

    # ---- 生成内容 ----
    print("\n" + "-" * 66)
    print("生成的 Skill")
    print("-" * 66)
    print(f"\n① 名称     ：{skill.name}")
    print(f"② 描述     ：{skill.description[:90]}…")
    print(f"③ 使用场景 ：")
    for uc in skill.use_cases:
        print(f"     · {uc}")

    print(f"\n④ 输入数据定义（{len(skill.input_schema)} 个字段）：")
    for f in skill.input_schema:
        req = "必填" if f.required else "选填"
        unit = f"（{f.unit}）" if f.unit else ""
        print(f"     · {f.name}{unit} [{f.type.value}/{req}]")
        print(f"         口径：{f.description[:60]}")
        print(f"         来源：{f.source_hint or '⚠️ 未提供'}")

    print(f"\n⑤ 分析流程（{len(skill.analysis_flow)} 步："
          f"{len(skill.metric_steps)} 个 metric + {len(skill.llm_steps)} 个 llm）：")
    for step in skill.analysis_flow:
        tag = "🔢 计算" if step.type == "metric" else "🧠 推理"
        print(f"\n     [{step.step_id}] {tag}  {step.name}")
        if step.type == "metric":
            for line in step.formula.split("\n"):
                if line.strip():
                    print(f"           {line.strip()}")
        else:
            print(f"           {step.instruction[:150].replace(chr(10), ' ')}…")

    print(f"\n⑥ Agent Prompt（{len(skill.agent_prompt)} 字）：")
    has_forbid = "禁止" in skill.agent_prompt
    print(f"     含【禁止事项】章节：{'✅ 是' if has_forbid else '❌ 否'}")

    print(f"\n⑦ 输出模板（{len(skill.output_template)} 字）：")
    for line in skill.output_template.split("\n")[:6]:
        print(f"     {line}")
    print("     …")

    if skill.metric_dictionary:
        print(f"\n   指标口径字典（{len(skill.metric_dictionary)} 条）：")
        for name, desc in list(skill.metric_dictionary.items())[:4]:
            print(f"     · {name}：{desc[:60]}")

    # ---- 自检 ----
    if result.critique:
        print("\n" + "-" * 66)
        print("模型自检（self-critique）")
        print("-" * 66)
        print(result.critique[:700])

    # ---- 是否照抄示例 ----
    print("\n" + "-" * 66)
    print("原创性检查（是否照抄了 few-shot 示例）")
    print("-" * 66)
    preset = json.loads(
        (ROOT / "data" / "presets" / "douyin_live_review.json").read_text(encoding="utf-8")
    )
    preset_fields = {f["name"] for f in preset["input_schema"]}
    new_fields = {f.name for f in skill.input_schema}
    overlap = preset_fields & new_fields
    print(f"  与示例重合的输入字段：{sorted(overlap) if overlap else '无'}")
    print(f"  {'✅ 是针对新需求重新设计的' if len(overlap) <= 1 else '⚠️ 与示例重合较多，需检查'}")

    # ---- 端到端：生成的 Skill 能否真的跑起来 ----
    print("\n" + "-" * 66)
    print("端到端验证：用 mock 数据真跑一遍生成的 Skill")
    print("-" * 66)
    try:
        df = build_mock_dataframe(skill, rows=3)
        exec_result = SkillOrchestrator(skill, df).run()
        failed = [t for t in exec_result.traces if t.status == "failed"]
        if failed:
            print(f"  ❌ {len(failed)} 个步骤执行失败：")
            for t in failed:
                print(f"      [{t.step_id}] {t.error[:120]}")
        else:
            print(f"  ✅ 全部 {len(exec_result.traces)} 个步骤执行成功")
            print(f"     产出 {len(exec_result.metrics)} 项指标，"
                  f"报告 {len(exec_result.report_markdown)} 字")
            if exec_result.hallucination_flags:
                print(f"     ⚠️ 检出 {len(exec_result.hallucination_flags)} 个疑似幻觉数字")
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ 执行异常：{type(exc).__name__}: {exc}")

    # ---- 保存 ----
    out = ROOT / "data" / "presets" / f"{skill.skill_id}.json"
    out.write_text(skill.model_dump_json(indent=2), encoding="utf-8")

    print("\n" + "=" * 66)
    print(f"{'✅ 生成成功' if result.ok else '⚠️ 生成完成但未通过全部校验'}"
          f"   总耗时 {result.total_duration_ms / 1000:.1f}s")
    print(f"已保存：{out.relative_to(ROOT)}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
