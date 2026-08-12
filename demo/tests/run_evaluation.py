"""
Golden Set 评测 —— 跑一遍标准用例，输出五维评分卡。

会真实调用 LLM（4 组用例约 $0.05 @ haiku）。

运行： ./.venv/bin/python -m tests.run_evaluation
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.evaluation.scorer import PASS_DATA_ACCURACY, PASS_TOTAL, evaluate_skill  # noqa: E402
from app.llm.client import gateway  # noqa: E402
from app.schemas.skill_spec import SkillSpec  # noqa: E402

SKILL_FILE = ROOT / "data" / "presets" / "douyin_live_review.json"
GOLDEN_DIR = ROOT / "tests" / "golden_set" / "douyin_live_review"


def bar(score: float, width: int = 20) -> str:
    filled = round(score / 100 * width)
    return "█" * filled + "░" * (width - filled)


def main() -> int:
    skill = SkillSpec.model_validate(json.loads(SKILL_FILE.read_text(encoding="utf-8")))

    print("=" * 68)
    print(f"Golden Set 评测：{skill.name}  v{skill.version}")
    print("=" * 68)
    print(f"模型：{gateway.status()['model']}")
    if gateway.is_mock:
        print("⚠️ 演示模式：文字类维度得分不具参考价值，但数据准确性维度依然有效")
    print()

    report = evaluate_skill(skill, GOLDEN_DIR)

    for case in report["cases"]:
        mark = "✅" if case["passed"] else "❌"
        print(f"{mark} {case['case']}    总分 {case['total']}")

        if case["blocked_as_expected"] is not None:
            status = "按预期拦截了执行" if case["blocked_as_expected"] else "未按预期拦截"
            print(f"     {status}")
            if case["error"]:
                print(f"     {case['error']}")
            print()
            continue

        if case["error"]:
            print(f"     执行失败：{case['error']}\n")
            continue

        for d in case["dimensions"]:
            print(f"     {d['name']:6} {bar(d['score'])} {d['score']:5.1f}  "
                  f"×{d['weight']:.2f} = {d['weighted']:5.2f}")
            for line in d["detail"]:
                print(f"            {line}")
        print()

    print("-" * 68)
    print(f"用例数        ：{report['case_count']}")
    print(f"通过          ：{report['passed_count']} / {report['case_count']}")
    print(f"平均分        ：{report['average_score']}")
    print(f"发布门槛      ：总分 ≥ {PASS_TOTAL} 且 数据准确性 ≥ {PASS_DATA_ACCURACY}")
    print(f"是否允许发布  ：{'✅ 是' if report['publishable'] else '❌ 否'}")

    out = ROOT / "data" / "evaluation_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完整报告已保存：{out.relative_to(ROOT)}")

    return 0 if report["publishable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
