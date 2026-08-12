"""
交互式写入 API Key 到 .env。

为什么单独写个脚本：直接用 shell 的 read -s 粘贴时屏幕无任何反馈，
很容易被误以为卡死而中断。这里改成可见输入 + 逐项校验 + 明确反馈。

运行： ./.venv/bin/python tools/set_key.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"

KEY_FIELD = "LLM_API_KEY"


def mask(key: str) -> str:
    if len(key) <= 18:
        return key[:6] + "…"
    return f"{key[:14]}{'•' * 12}{key[-4:]}"


def main() -> int:
    print("=" * 58)
    print("配置 Anthropic API Key")
    print("=" * 58)

    if not ENV_FILE.exists():
        if not ENV_EXAMPLE.exists():
            print("❌ 找不到 .env.example，请确认在 demo 目录下运行")
            return 1
        ENV_FILE.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        print("已从 .env.example 创建 .env")

    print(f"\n目标文件：{ENV_FILE}")
    print("\n请粘贴你的 API Key（Command + V），然后按回车。")
    print("提示：这次粘贴的内容**会**显示在屏幕上，方便你确认粘对了。\n")

    try:
        key = input("Key > ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n\n已取消，未做任何修改。")
        return 1

    # ---- 逐项校验，把问题说清楚而不是只报一句"格式错误" ----
    if not key:
        print("\n❌ 没有输入任何内容。请重新运行本脚本。")
        return 1

    key = key.strip().strip('"').strip("'")  # 容错：去掉可能误粘的引号

    problems: list[str] = []
    if not key.startswith("sk-ant-"):
        problems.append(f"应以 'sk-ant-' 开头，实际开头是 {key[:10]!r}")
    if " " in key:
        problems.append("包含空格，可能是粘贴时多带了内容")
    if len(key) < 50:
        problems.append(f"长度只有 {len(key)} 字符，正常的 Key 有 100 字符左右，可能没粘完整")
    if "在这里" in key or "填入" in key:
        problems.append("这还是占位符文本，不是真实 Key")

    if problems:
        print("\n❌ 这个 Key 看起来有问题：")
        for p in problems:
            print(f"   · {p}")
        print("\n请到 https://platform.claude.com/settings/keys 重新复制后再运行本脚本。")
        return 1

    # ---- 写入 ----
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    written = False
    for i, line in enumerate(lines):
        if line.startswith(f"{KEY_FIELD}="):
            lines[i] = f"{KEY_FIELD}={key}"
            written = True
            break
    if not written:
        lines.append(f"{KEY_FIELD}={key}")

    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\n✅ 已写入 .env")
    print(f"   {KEY_FIELD}={mask(key)}")
    print(f"   Key 长度：{len(key)} 字符")
    print("\n下一步，跑连通性自检：")
    print("   ./.venv/bin/python -m tests.check_llm")
    print("\n⚠️ 刚才 Key 显示在了屏幕上，建议执行 clear 命令清空终端记录。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
