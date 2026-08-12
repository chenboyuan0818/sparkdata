"""
把三份 Markdown 文档合成一份可提交的 PDF。

路线：Markdown → 带打印样式的 HTML → Chrome 无头模式打印 PDF
选 Chrome 而不是 pandoc/wkhtmltopdf 的原因：
  - 中文字体开箱即用（PingFang SC），不需要额外配置字体路径
  - 表格、代码块、emoji 的渲染效果和浏览器里看到的一致
  - macOS 自带，无需安装额外依赖

运行： python3 tools/build_pdf.py
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
OUT_DIR = ROOT / "submission"

# ── 提交信息 ──
NAME = "袁陈博"
SCHOOL = "雷丁大学 亨利商学院"
MAJOR = "数字商业与数据分析"
TITLE = "企业岗位经验 Skill 生成平台设计"
SUBTITLE = "FDE Echo 实习生笔试"
DEMO_URL = "https://sparkdata.onrender.com"
REPO_URL = "https://github.com/chenboyuan0818/sparkdata"

SECTIONS = [
    ("一", "产品设计文档（PRD）", "01_产品设计文档_PRD.md"),
    ("二", "技术方案说明", "02_技术方案说明.md"),
    ("三", "面试问题回答", "03_面试问题回答.md"),
]

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]

CSS = """
@page { size: A4; margin: 18mm 16mm 16mm 16mm; }

* { box-sizing: border-box; }

body {
  font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  font-size: 10.5pt;
  line-height: 1.75;
  color: #1f2933;
  margin: 0;
}

/* ---------- 封面 ---------- */
.cover {
  height: 247mm;                 /* A4 减去上下边距，保证封面独占一页 */
  display: flex;
  flex-direction: column;
  justify-content: center;
  page-break-after: always;
}
.cover .eyebrow { font-size: 11pt; color: #6b7280; letter-spacing: .12em; margin-bottom: 10mm; }
.cover h1 { font-size: 26pt; font-weight: 700; line-height: 1.35; margin: 0 0 6mm; color: #111827; }
.cover .lead {
  font-size: 11pt; color: #4b5563; line-height: 1.9;
  border-left: 3px solid #4f46e5; padding-left: 5mm; margin: 0 0 12mm;
}
.cover .meta { font-size: 10.5pt; color: #374151; line-height: 2.1; }
.cover .meta b { display: inline-block; width: 22mm; color: #6b7280; font-weight: 400; }
.cover .links { margin-top: 10mm; font-size: 10pt; line-height: 2; }
.cover .links a { color: #4f46e5; text-decoration: none; word-break: break-all; }
.cover .note { margin-top: 8mm; font-size: 9pt; color: #9ca3af; }

/* ---------- 目录 ---------- */
.toc { page-break-after: always; }
.toc h2 { font-size: 16pt; border: none; margin: 0 0 8mm; }
.toc ol { list-style: none; padding: 0; counter-reset: sec; }
.toc li {
  counter-increment: sec; font-size: 11pt; padding: 3mm 0;
  border-bottom: 1px dotted #d1d5db;
}
.toc li::before { content: counter(sec, cjk-decimal) "、"; color: #6b7280; }
.toc .desc { font-size: 9pt; color: #9ca3af; margin-top: 1mm; padding-left: 8mm; }

/* ---------- 章节分隔 ---------- */
.section { page-break-before: always; }
.section-label {
  font-size: 9pt; color: #6b7280; letter-spacing: .1em;
  border-bottom: 2px solid #4f46e5; padding-bottom: 2mm; margin-bottom: 6mm;
}

/* ---------- 正文 ---------- */
h1 { font-size: 19pt; margin: 0 0 5mm; color: #111827; page-break-after: avoid; }
h2 {
  font-size: 14pt; margin: 9mm 0 4mm; padding-bottom: 2mm;
  border-bottom: 1px solid #e5e7eb; color: #1f2933; page-break-after: avoid;
}
h3 { font-size: 11.5pt; margin: 6mm 0 3mm; color: #374151; page-break-after: avoid; }
h4 { font-size: 10.5pt; margin: 5mm 0 2mm; color: #4b5563; page-break-after: avoid; }
p { margin: 2.5mm 0; }
ul, ol { margin: 2.5mm 0 2.5mm 6mm; padding: 0; }
li { margin: 1.2mm 0; }
strong { color: #111827; font-weight: 600; }

table {
  width: 100%; border-collapse: collapse; margin: 4mm 0;
  font-size: 9pt; page-break-inside: avoid;
}
th, td { border: 1px solid #d1d5db; padding: 2mm 2.5mm; text-align: left; vertical-align: top; }
th { background: #f3f4f6; font-weight: 600; }
tr:nth-child(even) td { background: #fafafa; }

pre {
  background: #f6f8fa; border: 1px solid #e5e7eb; border-radius: 3px;
  padding: 3mm; font-size: 8pt; line-height: 1.55; overflow-x: auto;
  white-space: pre-wrap; word-break: break-word; page-break-inside: avoid;
  font-family: "SF Mono", Menlo, Consolas, monospace;
}
code {
  background: #f3f4f6; padding: .3mm 1.2mm; border-radius: 2px;
  font-size: 8.8pt; font-family: "SF Mono", Menlo, Consolas, monospace;
}
pre code { background: none; padding: 0; font-size: inherit; }

blockquote {
  border-left: 3px solid #c7d2fe; background: #f8f9ff;
  padding: 2.5mm 4mm; margin: 3mm 0; color: #4b5563;
}
blockquote p { margin: 1.5mm 0; }

hr { border: none; border-top: 1px solid #e5e7eb; margin: 6mm 0; }
a { color: #4f46e5; text-decoration: none; }
"""


def find_chrome() -> str:
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    sys.exit("❌ 未找到 Chrome / Edge，无法生成 PDF")


def render_markdown(text: str) -> str:
    import markdown

    # 顶层 h1 在章节封面里已经给过了，正文里降一级避免重复
    return markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
    )


def build_html() -> str:
    parts: list[str] = []

    # ── 封面 ──
    parts.append(f"""
<div class="cover">
  <div class="eyebrow">{html.escape(SUBTITLE)}</div>
  <h1>{html.escape(TITLE)}</h1>
  <div class="lead">
    把优秀员工的岗位经验，通过自然语言描述，<br>
    自动转换为可执行、可评测、可版本管理的 AI 员工能力资产。
  </div>
  <div class="meta">
    <div><b>姓名</b>{html.escape(NAME)}</div>
    <div><b>学校</b>{html.escape(SCHOOL)}</div>
    <div><b>专业</b>{html.escape(MAJOR)}</div>
  </div>
  <div class="links">
    <div><b>在线 Demo</b>　<a href="{DEMO_URL}">{DEMO_URL}</a></div>
    <div><b>代码仓库</b>　<a href="{REPO_URL}">{REPO_URL}</a></div>
  </div>
  <div class="note">
    在线 Demo 无需任何配置即可直接体验完整流程。<br>
    免费实例在无访问时会休眠，首次打开约需一分钟唤醒。
  </div>
</div>
""")

    # ── 目录 ──
    toc_items = "".join(
        f'<li>{html.escape(title)}</li>' for _, title, _ in SECTIONS
    )
    parts.append(f"""
<div class="toc">
  <h2>目录</h2>
  <ol>{toc_items}</ol>
  <div class="desc" style="margin-top:8mm">
    可运行 Demo 见封面链接，代码与自动化测试均在仓库中。
  </div>
</div>
""")

    # ── 三份正文 ──
    for num, title, filename in SECTIONS:
        path = DOCS / filename
        if not path.exists():
            sys.exit(f"❌ 找不到文档：{path}")
        body = render_markdown(path.read_text(encoding="utf-8"))
        parts.append(f"""
<div class="section">
  <div class="section-label">第 {num} 部分 · {html.escape(title)}</div>
  {body}
</div>
""")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>{html.escape(TITLE)}</title>
<style>{CSS}</style></head>
<body>{"".join(parts)}</body>
</html>"""


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)

    html_path = OUT_DIR / "_submission.html"
    html_path.write_text(build_html(), encoding="utf-8")
    print(f"✅ HTML 已生成：{html_path.relative_to(ROOT)}  ({html_path.stat().st_size // 1024} KB)")

    pdf_name = f"FDE Echo 实习生笔试—{NAME}—雷丁大学.pdf"
    pdf_path = OUT_DIR / pdf_name

    chrome = find_chrome()
    print(f"   使用 {Path(chrome).name} 打印 PDF…")

    subprocess.run(
        [
            chrome,
            "--headless",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path}",
            html_path.as_uri(),
        ],
        check=True,
        capture_output=True,
        timeout=180,
    )

    if not pdf_path.exists():
        sys.exit("❌ PDF 生成失败")

    size_kb = pdf_path.stat().st_size // 1024
    print(f"✅ PDF 已生成：submission/{pdf_name}  ({size_kb} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
