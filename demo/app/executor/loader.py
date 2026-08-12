"""
数据加载与输入校验。

职责：
  1. 稳健地读入 CSV（编码探测、分隔符探测、数值清洗）
  2. 把 CSV 列名模糊匹配到 SkillSpec 的 input_schema 字段名
  3. 按 input_schema 做强校验 —— 必填缺失就拦截执行，不允许"带病运行"

第 3 步是刻意设计的：宁可明确报错告诉用户缺哪个字段、从哪导出，
也不要在数据不全的情况下产出一份看起来很像模像样但结论错误的报告。
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import pandas as pd

from app.schemas.skill_spec import FieldType, InputField, SkillSpec


class DataValidationError(Exception):
    """输入数据不满足 Skill 的数据契约。"""

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


# --------------------------------------------------------------------------
# 校验报告
# --------------------------------------------------------------------------

@dataclass
class ValidationReport:
    ok: bool
    missing_required: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mapping: dict[str, str] = field(default_factory=dict)  # schema 字段 -> CSV 列
    row_count: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "missing_required": self.missing_required,
            "warnings": self.warnings,
            "mapping": self.mapping,
            "row_count": self.row_count,
        }


# --------------------------------------------------------------------------
# CSV 读取
# --------------------------------------------------------------------------

def read_csv(content: bytes) -> pd.DataFrame:
    """
    稳健读取 CSV。

    真实用户上传的文件千奇百怪：Excel 导出的 GBK、带 BOM 的 UTF-8、
    分号分隔的欧洲格式。这里按常见组合依次尝试。
    """
    encodings = ["utf-8-sig", "utf-8", "gbk", "gb18030", "latin1"]
    separators = [",", "\t", ";"]

    last_error: Exception | None = None
    for encoding in encodings:
        for sep in separators:
            try:
                df = pd.read_csv(io.BytesIO(content), encoding=encoding, sep=sep)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
            # 只解析出一列通常说明分隔符猜错了
            if df.shape[1] > 1 or sep == separators[-1]:
                df.columns = [str(c).strip() for c in df.columns]
                return df

    raise DataValidationError(f"无法解析该 CSV 文件：{last_error}")


# --------------------------------------------------------------------------
# 字段映射
# --------------------------------------------------------------------------

_ALIAS_HINTS: dict[str, list[str]] = {
    "曝光": ["曝光量", "曝光人数", "展现量", "impressions", "show"],
    "进入": ["进入人数", "观看人数", "uv", "访客数", "visitors"],
    "点击": ["点击人数", "点击量", "clicks"],
    "订单": ["订单数", "成交订单数", "orders"],
    "gmv": ["gmv", "成交金额", "销售额", "revenue"],
    "停留": ["停留时长", "平均停留时长", "duration"],
}


def _normalize(text: str) -> str:
    """去掉空格、括号、单位后缀，统一小写，便于模糊匹配。"""
    text = str(text).lower().strip()
    text = re.sub(r"[（(].*?[)）]", "", text)
    text = re.sub(r"[\s_\-/]", "", text)
    return text


def auto_map_columns(
    df: pd.DataFrame, input_schema: list[InputField]
) -> dict[str, str]:
    """
    自动把 schema 字段名匹配到 CSV 列名。

    三级匹配：完全一致 → 归一化后一致 → 包含关系 / 别名表。
    结果会返回给前端，由用户确认或手动调整 —— 不做静默猜测。
    """
    mapping: dict[str, str] = {}
    columns = list(df.columns)
    used: set[str] = set()
    norm_cols = {col: _normalize(col) for col in columns}

    for f in input_schema:
        target = f.name
        target_norm = _normalize(target)

        # 1) 完全一致
        if target in columns and target not in used:
            mapping[target] = target
            used.add(target)
            continue

        # 2) 归一化后一致
        match = next(
            (c for c in columns if c not in used and norm_cols[c] == target_norm), None
        )
        if match:
            mapping[target] = match
            used.add(match)
            continue

        # 3) 包含关系
        match = next(
            (
                c
                for c in columns
                if c not in used
                and (target_norm in norm_cols[c] or norm_cols[c] in target_norm)
            ),
            None,
        )
        if match:
            mapping[target] = match
            used.add(match)
            continue

        # 4) 别名表
        for key, aliases in _ALIAS_HINTS.items():
            if key not in target_norm:
                continue
            match = next(
                (
                    c
                    for c in columns
                    if c not in used
                    and any(a in norm_cols[c] for a in aliases)
                ),
                None,
            )
            if match:
                mapping[target] = match
                used.add(match)
                break

    return mapping


# --------------------------------------------------------------------------
# 数值清洗
# --------------------------------------------------------------------------

_NUMERIC_JUNK = re.compile(r"[,¥$€\s%万元人次单]")


def _coerce_numeric(series: pd.Series) -> pd.Series:
    """把 '1,234'、'¥5,678.90'、'12.3%' 这类字符串转成数值。"""
    if pd.api.types.is_numeric_dtype(series):
        return series

    cleaned = series.astype(str).str.strip()
    has_percent = cleaned.str.contains("%", na=False)
    cleaned = cleaned.str.replace(_NUMERIC_JUNK, "", regex=True)
    result = pd.to_numeric(cleaned, errors="coerce")
    # 百分号列转成小数
    result = result.where(~has_percent, result / 100)
    return result


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

def load_and_validate(
    content: bytes,
    skill: SkillSpec,
    mapping_override: dict[str, str] | None = None,
) -> tuple[pd.DataFrame, ValidationReport]:
    """
    读取 CSV 并按 skill.input_schema 校验。

    返回：(已按 schema 字段名重命名的 DataFrame, 校验报告)
    必填字段缺失时抛 DataValidationError —— 拦截执行，不允许带病运行。
    """
    raw = read_csv(content)
    mapping = auto_map_columns(raw, skill.input_schema)
    if mapping_override:
        mapping.update({k: v for k, v in mapping_override.items() if v})

    report = ValidationReport(ok=True, mapping=mapping, row_count=len(raw))

    # ---- 必填字段检查 ----
    for f in skill.input_schema:
        if f.required and f.name not in mapping:
            report.missing_required.append(
                {
                    "name": f.name,
                    "description": f.description,
                    "source_hint": f.source_hint or "未提供来源提示",
                }
            )

    if report.missing_required:
        report.ok = False
        names = "、".join(m["name"] for m in report.missing_required)
        raise DataValidationError(
            f"缺少必填字段：{names}。请补充后重新上传。",
            detail=report.to_dict(),
        )

    # ---- 构建规范化 DataFrame ----
    data: dict[str, pd.Series] = {}
    for f in skill.input_schema:
        source_col = mapping.get(f.name)
        if source_col is None:
            report.warnings.append(f"选填字段「{f.name}」未提供，相关分析将被跳过")
            continue

        series = raw[source_col]
        if f.type in (FieldType.INTEGER, FieldType.FLOAT):
            converted = _coerce_numeric(series)
            bad = int(converted.isna().sum()) - int(series.isna().sum())
            if bad > 0:
                report.warnings.append(
                    f"字段「{f.name}」有 {bad} 行无法转换为数值，已按空值处理"
                )
            series = converted.fillna(0)
        data[f.name] = series

    df = pd.DataFrame(data)

    # ---- 异常值检测 ----
    for f in skill.input_schema:
        if f.name not in df.columns or f.type not in (FieldType.INTEGER, FieldType.FLOAT):
            continue
        col = df[f.name]
        negatives = int((col < 0).sum())
        if negatives:
            report.warnings.append(f"字段「{f.name}」存在 {negatives} 个负值，请核对数据")
        if f.unit == "%" and (col > 1.5).any():
            report.warnings.append(
                f"字段「{f.name}」存在大于 150% 的值，疑似数据错误"
            )

    if len(df) == 0:
        raise DataValidationError("上传的数据为空，无法执行分析。")

    report.row_count = len(df)
    return df, report
