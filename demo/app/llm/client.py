"""
LLM 网关 —— 屏蔽模型差异，支持切换与降级。

三种运行模式（按 .env 中的 LLM_PROVIDER 选择）：
  - anthropic       Claude，使用原生 structured output，结构化可靠性最好
  - openai_compat   OpenAI 兼容接口（DeepSeek / 智谱 / 通义 / Kimi 等）
  - mock            无 API Key 时的演示模式，返回预置结果

保留 mock 模式是刻意设计：面试官打开 Demo 链接时，
不应该因为额度耗尽或网络问题看到一个报错页面。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Type, TypeVar

from pydantic import BaseModel, ValidationError

# 从项目根目录的 .env 读取配置（若存在）。
# 没装 python-dotenv 或没有 .env 文件都不影响运行 —— 会自动降级到 mock 模式。
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except ImportError:  # pragma: no cover
    pass

T = TypeVar("T", bound=BaseModel)

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
DEFAULT_OPENAI_COMPAT_MODEL = "deepseek-v4-flash"


class LLMError(Exception):
    """LLM 调用失败。"""


def _is_placeholder(api_key: str) -> bool:
    """判断 Key 是否还是 .env.example 中的占位符。"""
    if not api_key:
        return True
    lowered = api_key.lower()
    return "在这里" in api_key or any(
        token in lowered for token in ("xxxxx", "your-key", "your_key", "填入")
    )


class LLMGateway:
    """
    统一的模型调用入口。

    上层（generator / orchestrator）只依赖这两个方法，
    换模型供应商时上层代码零改动。
    """

    def __init__(self) -> None:
        self.provider = os.getenv("LLM_PROVIDER", "").strip().lower()
        self.api_key = os.getenv("LLM_API_KEY", "").strip()
        self.model = os.getenv("LLM_MODEL", "").strip()
        self.base_url = os.getenv("LLM_BASE_URL", "").strip()

        # 未显式配置 provider 时自动探测；都没有就降级到 mock
        if not self.provider:
            if self.api_key.startswith("sk-ant-") or os.getenv("ANTHROPIC_API_KEY"):
                self.provider = "anthropic"
                self.api_key = self.api_key or os.getenv("ANTHROPIC_API_KEY", "")
            elif self.api_key and self.base_url:
                self.provider = "openai_compat"
            else:
                self.provider = "mock"

        # anthropic 模式下允许直接读官方标准环境变量
        if self.provider == "anthropic" and not self.api_key:
            self.api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

        # Key 仍是 .env.example 里的占位符时降级到 mock，
        # 避免带着假 Key 去调用然后收到一个难以理解的 401
        if _is_placeholder(self.api_key):
            self.provider = "mock"

        # 只在真正要调用模型时才填默认模型名；
        # mock 模式保持 model 为空，避免在自检输出里显示一个并不会被调用的模型
        if not self.model:
            if self.provider == "anthropic":
                self.model = DEFAULT_ANTHROPIC_MODEL
            elif self.provider == "openai_compat":
                self.model = DEFAULT_OPENAI_COMPAT_MODEL

        self._client = None

    # ---------------- 状态 ----------------

    @property
    def is_mock(self) -> bool:
        return self.provider == "mock"

    def status(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model or "(mock)",
            "ready": self.provider == "mock" or bool(self.api_key),
        }

    # ---------------- 客户端惰性初始化 ----------------

    def _get_client(self):
        if self._client is not None:
            return self._client

        if self.provider == "anthropic":
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise LLMError("未安装 anthropic SDK，请执行 pip install anthropic") from exc
            self._client = anthropic.Anthropic(api_key=self.api_key or None)

        elif self.provider == "openai_compat":
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover
                raise LLMError("未安装 openai SDK，请执行 pip install openai") from exc
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url or None)

        return self._client

    # ---------------- 结构化输出 ----------------

    def generate_structured(
        self,
        system: str,
        user: str,
        schema: Type[T],
        max_retries: int = 2,
    ) -> T:
        """
        产出符合 schema 的对象。

        Claude 走原生 structured output，由 API 层面保证 JSON 合法；
        OpenAI 兼容接口走 JSON mode + 本地 Pydantic 校验 + 带错误信息重试。
        """
        if self.provider == "mock":
            raise LLMError("mock 模式不支持结构化生成，请使用预置 Skill")

        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            prompt = user
            if last_error is not None:
                # 把上一次的校验错误反馈给模型，定向修正
                prompt = (
                    f"{user}\n\n"
                    f"【上一次生成存在以下问题，请修正后重新输出】\n{last_error}"
                )

            try:
                if self.provider == "anthropic":
                    return self._anthropic_structured(system, prompt, schema)
                return self._openai_structured(system, prompt, schema)
            except (ValidationError, json.JSONDecodeError) as exc:
                last_error = exc
                continue
            except Exception as exc:  # noqa: BLE001
                raise LLMError(f"模型调用失败：{exc}") from exc

        raise LLMError(f"结构化生成连续 {max_retries + 1} 次失败：{last_error}")

    def _anthropic_structured(self, system: str, user: str, schema: Type[T]) -> T:
        client = self._get_client()
        response = client.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
        if response.parsed_output is None:
            raise LLMError(f"模型未返回可解析结果，stop_reason={response.stop_reason}")
        return response.parsed_output

    def _openai_structured(self, system: str, user: str, schema: Type[T]) -> T:
        client = self._get_client()
        json_schema = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        response = client.chat.completions.create(
            model=self.model,
            max_tokens=8000,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": f"{system}\n\n【输出要求】严格输出符合以下 JSON Schema 的"
                    f"单个 JSON 对象，不要包含任何解释文字或 Markdown 代码块标记：\n{json_schema}",
                },
                {"role": "user", "content": user},
            ],
        )
        text = response.choices[0].message.content or ""
        return schema.model_validate_json(_strip_code_fence(text))

    # ---------------- 自由文本输出 ----------------

    def generate_text(self, system: str, user: str, max_tokens: int = 4000) -> str:
        """用于归因分析和建议生成。"""
        if self.provider == "mock":
            return _mock_text(user)

        try:
            if self.provider == "anthropic":
                client = self._get_client()
                response = client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
                if response.stop_reason == "refusal":
                    raise LLMError("模型拒绝了本次请求")
                return "".join(
                    b.text for b in response.content if b.type == "text"
                ).strip()

            client = self._get_client()
            response = client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            return (response.choices[0].message.content or "").strip()

        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"模型调用失败：{exc}") from exc


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------

def _strip_code_fence(text: str) -> str:
    """去掉模型有时会加上的 ```json 包裹。"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def _mock_text(user_prompt: str) -> str:
    """mock 模式下的占位输出，保证无 Key 时全流程可演示。"""
    return (
        "（演示模式输出）根据上游确定性计算得到的指标结果，"
        "本环节的分析结论如下：核心漏斗中转化环节表现低于基准区间，"
        "为当前的主要瓶颈；建议优先核查该环节的执行动作。\n\n"
        "> 提示：当前未配置 LLM API Key，本段文字为预置占位内容。"
        "报告中的所有**数值**仍由确定性计算引擎真实算出，不受此影响。"
    )


# 全局单例
gateway = LLMGateway()
