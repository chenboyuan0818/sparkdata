"""
LLM 连通性自检 —— 配好 .env 之后先跑这个。

用最小的调用量验证三件事：
  1. Key 是否有效、余额是否充足
  2. 自由文本输出能不能用（归因分析链路依赖它）
  3. 结构化输出能不能用（Skill 生成链路依赖它）

整次自检消耗不到 1000 token，成本可以忽略。

运行： ./.venv/bin/python -m tests.check_llm
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import BaseModel, Field  # noqa: E402

from app.llm.client import LLMError, gateway  # noqa: E402


# 每百万 token 单价（输入, 输出），用于估算本次自检花了多少钱
PRICING = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


class _Probe(BaseModel):
    """一个刻意设计得很小的结构，用来验证 structured output 是否可用。"""

    city: str = Field(description="城市名")
    population_wan: int = Field(description="常住人口，单位万人")
    is_coastal: bool = Field(description="是否为沿海城市")


def main() -> int:
    print("=" * 58)
    print("LLM 连通性自检")
    print("=" * 58)

    status = gateway.status()
    print(f"\n供应商 : {status['provider']}")
    print(f"模型   : {status['model']}")

    if gateway.is_mock:
        print("\n⚠️  当前处于 mock 模式，未检测到有效的 API Key。")
        print("\n请按以下步骤配置：")
        print("  1. 充值      https://platform.claude.com/settings/billing")
        print("  2. 创建 Key  https://platform.claude.com/settings/keys")
        print("  3. cp .env.example .env")
        print("  4. 编辑 .env，把 LLM_API_KEY 换成你的真实 Key")
        print("\n提示：mock 模式下 Demo 仍可完整演示，只是文字分析为预置内容。")
        return 1

    ok = True

    # ---- 1. 自由文本输出 ----
    print("\n[1] 自由文本输出（归因分析链路依赖）")
    try:
        text = gateway.generate_text(
            system="你是一个测试助手。只回答被问到的内容，不要有多余的话。",
            user="用不超过 15 个字回答：直播间进入率低通常说明什么问题？",
            max_tokens=100,
        )
        print(f"  ✅ 调用成功")
        print(f"     模型回复：{text.strip()}")
    except LLMError as exc:
        ok = False
        print(f"  ❌ 调用失败：{exc}")
        _explain(exc)

    # ---- 2. 结构化输出 ----
    print("\n[2] 结构化输出（Skill 生成链路依赖）")
    try:
        probe = gateway.generate_structured(
            system="你是一个数据助手，按要求输出结构化数据。",
            user="给出深圳市的基本信息。",
            schema=_Probe,
        )
        print(f"  ✅ 调用成功，且返回值通过了 Pydantic 校验")
        print(f"     解析结果：{probe.city} / {probe.population_wan} 万人 / "
              f"沿海={probe.is_coastal}")
    except LLMError as exc:
        ok = False
        print(f"  ❌ 调用失败：{exc}")
        _explain(exc)

    # ---- 小结 ----
    print("\n" + "=" * 58)
    if ok:
        price = PRICING.get(gateway.model)
        cost_hint = ""
        if price:
            # 两次调用合计约 300 输入 + 150 输出
            cost = 300 * price[0] / 1_000_000 + 150 * price[1] / 1_000_000
            cost_hint = f"（本次自检约消耗 ${cost:.5f}）"
        print(f"✅ 全部通过，可以开始跑完整流程了 {cost_hint}")
        return 0

    print("❌ 存在失败项，请按上面的提示排查后重试")
    return 1


def _explain(exc: Exception) -> None:
    """把常见错误翻译成可操作的建议。"""
    message = str(exc).lower()
    if "401" in message or "authentication" in message or "invalid x-api-key" in message:
        print("     → Key 无效。检查 .env 里是否粘贴完整（含 sk-ant- 前缀）、有无多余空格")
    elif "credit" in message or "billing" in message or "quota" in message:
        print("     → 余额不足。前往 https://platform.claude.com/settings/billing 充值")
    elif "404" in message or "not_found" in message:
        print("     → 模型名有误。检查 .env 中的 LLM_MODEL，"
              "可用值：claude-opus-5 / claude-sonnet-5 / claude-haiku-4-5")
    elif "rate_limit" in message or "429" in message:
        print("     → 触发限流。等待几十秒后重试")
    elif "connection" in message or "timeout" in message:
        print("     → 网络问题。检查网络连通性或代理设置")


if __name__ == "__main__":
    raise SystemExit(main())
