"""
指标计算引擎 —— 确定性计算，报告里所有数字的唯一来源。

安全性说明
----------
formula 字段是 LLM 生成的字符串，直接 eval() 等于任意代码执行漏洞。
本引擎用 Python ast 模块把表达式解析成语法树，然后用**节点类型白名单**
逐个校验，只放行四则运算、比较、以及白名单内的聚合函数。
遇到属性访问、下标、导入、lambda 等一律拒绝。

聚合语义
--------
CSV 可能有多行（如一周 7 场直播）。规则：
  - 裸列名出现在算术运算中  → 自动按 sum() 聚合
    例：进入率 = 进入直播间人数 / 曝光人数
        实际计算 sum(进入人数) / sum(曝光人数)，这正是漏斗比率的正确算法
  - 列名出现在函数调用中    → 传入完整 Series
    例：平均停留 = mean(停留时长)

每个指标的计算结果都带溯源信息（公式、依赖字段、数据行数），
供报告前端展开查看。
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------

class MetricError(Exception):
    """指标计算失败。"""


class UnsafeFormulaError(MetricError):
    """公式中包含不被允许的语法结构。"""


# --------------------------------------------------------------------------
# 计算结果（带溯源）
# --------------------------------------------------------------------------

def infer_unit(metric_name: str, override: dict[str, str] | None = None) -> str | None:
    """
    推断指标的展示单位。

    优先级：Skill 显式声明的 metric_units > 按名字推断 > 无单位。

    ⚠️ 按名字推断天生不可靠，只能当兜底。
    最典型的反例是「连带率」—— 它的值是 1.4 件/单，不是 140%，
    但名字以「率」结尾。中文指标命名不足以确定单位，
    所以凡是可能猜错的，都应该在 Skill 的 metric_units 里显式写死。

    推断逻辑集中在这一个函数里，是为了保证同一指标在报告表格、
    溯源面板、传给模型的上下文里展示口径完全一致。
    """
    if override and metric_name in override:
        return override[metric_name] or None

    # 金额类放在比率类之前判断：「客单价」「件单价」都含「价」但不是百分比
    if any(k in metric_name for k in (
        "GMV", "gmv", "金额", "单价", "价值", "收入", "产能", "缺口", "销售额",
    )):
        return "元"
    if "坪效" in metric_name:
        return "元/㎡"
    if any(k in metric_name for k in ("时长", "秒数")):
        return "秒"
    if metric_name.endswith(("率", "占比", "比例")) or "率基准" in metric_name:
        return "%"
    if any(k in metric_name for k in ("人数", "订单数", "场次数", "次数", "客流")):
        return "个"
    return None


@dataclass
class MetricResult:
    """单个指标的计算结果 + 溯源信息。"""

    name: str
    value: Any
    formula: str
    depends_on: list[str] = field(default_factory=list)
    row_count: int = 0
    warning: str | None = None
    unit: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "value": self.value,
            "display": self.display(),
            "formula": self.formula,
            "depends_on": self.depends_on,
            "row_count": self.row_count,
            "warning": self.warning,
            "unit": self.unit,
        }

    def display(self) -> str:
        """
        人类可读的数值表示。

        注意：这里只改变**展示形式**，self.value 始终保留原始计算值。
        幻觉检测同时接受原始值和百分比形式，所以模型引用 "6.68%" 不会被误判。
        """
        v = self.value

        if isinstance(v, bool):
            return "是" if v else "否"

        if not isinstance(v, (int, float)):
            return str(v)

        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return "N/A"

        # 百分比是唯一需要改变数量级的单位，单独处理
        if self.unit == "%":
            return f"{v * 100:.2f}%"

        number = self._format_number(float(v))

        # 「个」是计数的默认单位，模板里通常自带「人/单/家」，不再追加后缀
        if self.unit in (None, "个"):
            return number
        return f"{number} {self.unit}"

    @staticmethod
    def _format_number(v: float) -> str:
        """整数不带小数点；小数保留两位；极小值保留更多位以免显示成 0.00。"""
        if v.is_integer():
            return f"{int(v):,}"
        if abs(v) < 0.01:
            return f"{v:,.4f}".rstrip("0").rstrip(".")
        return f"{v:,.2f}"


# --------------------------------------------------------------------------
# 受限求值器
# --------------------------------------------------------------------------

_ALLOWED_NODES: tuple[type, ...] = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.BoolOp,
    ast.IfExp,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    # 运算符
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.And, ast.Or, ast.Not,
)

_BIN_OPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: _safe_div(a, b),
    ast.FloorDiv: lambda a, b: a // b if b else 0,
    ast.Mod: lambda a, b: a % b if b else 0,
    ast.Pow: lambda a, b: a ** b,
}

_CMP_OPS = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}


def _safe_div(a: Any, b: Any) -> Any:
    """除零保护 —— 数据缺失时不让整个流程崩掉。"""
    try:
        if b == 0 or pd.isna(b):
            return 0.0
    except (TypeError, ValueError):
        pass
    try:
        return a / b
    except ZeroDivisionError:
        return 0.0


def _reduce(value: Any) -> Any:
    """把 Series 降维成标量（默认 sum），标量原样返回。"""
    if isinstance(value, pd.Series):
        if len(value) == 1:
            return value.iloc[0]
        if pd.api.types.is_numeric_dtype(value):
            return value.sum()
        return value.iloc[0]
    return value


def _days_between(end: Any, start: Any) -> pd.Series:
    """
    两个日期字段的天数差，返回逐行的天数 Series。

    做成显式函数而不是让 `结束日期 - 开始日期` 隐式生效，
    理由是后者需要引擎去猜列的类型：猜对了没人注意，猜错了报出来的是
    `unsupported operand type(s) for -: 'str' and 'str'`，
    生成 Skill 的模型根本看不懂该怎么改。显式函数名本身就是文档。
    """
    end_dt = pd.to_datetime(pd.Series(end), errors="coerce")
    start_dt = pd.to_datetime(pd.Series(start), errors="coerce")
    return (end_dt - start_dt).dt.days


# 允许在公式中调用的函数。参数会拿到完整 Series，不做预聚合。
_ALLOWED_FUNCS = {
    "days_between": _days_between,
    "sum": lambda s: float(pd.Series(s).sum()),
    "mean": lambda s: float(pd.Series(s).mean()),
    "avg": lambda s: float(pd.Series(s).mean()),
    "max": lambda s: float(pd.Series(s).max()),
    "min": lambda s: float(pd.Series(s).min()),
    "median": lambda s: float(pd.Series(s).median()),
    "std": lambda s: float(pd.Series(s).std()),
    "count": lambda s: int(pd.Series(s).count()),
    "first": lambda s: pd.Series(s).iloc[0],
    "last": lambda s: pd.Series(s).iloc[-1],
    "abs": lambda x: abs(_reduce(x)),
    "round": lambda x, n=2: round(float(_reduce(x)), int(_reduce(n))),
}


class SafeEvaluator:
    """基于 ast 白名单的受限表达式求值器。"""

    def __init__(self, variables: dict[str, Any]):
        self.variables = variables
        self.referenced: list[str] = []

    # ---- 安全校验 ----

    def _assert_safe(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_NODES):
                raise UnsafeFormulaError(
                    f"公式中包含不被允许的语法结构：{type(node).__name__}。"
                    f"只允许四则运算、比较和 {sorted(_ALLOWED_FUNCS)} 中的函数。"
                )
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name):
                    raise UnsafeFormulaError("只允许直接调用白名单函数，不允许属性调用。")
                if node.func.id not in _ALLOWED_FUNCS:
                    raise UnsafeFormulaError(
                        f"未知函数 '{node.func.id}'，"
                        f"可用函数：{sorted(_ALLOWED_FUNCS)}"
                    )
                if node.keywords:
                    raise UnsafeFormulaError("函数调用不支持关键字参数。")

    # ---- 求值 ----

    def evaluate(self, expression: str) -> Any:
        try:
            tree = ast.parse(expression.strip(), mode="eval")
        except SyntaxError as exc:
            raise MetricError(f"公式语法错误：{expression!r} —— {exc.msg}") from exc

        self._assert_safe(tree)
        return _reduce(self._eval(tree.body))

    def _eval(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value

        if isinstance(node, ast.Name):
            if node.id not in self.variables:
                raise MetricError(
                    f"公式引用了未定义的字段或指标：'{node.id}'。"
                    f"可用变量：{sorted(self.variables)[:20]}"
                )
            if node.id not in self.referenced:
                self.referenced.append(node.id)
            return self.variables[node.id]

        if isinstance(node, ast.BinOp):
            # 算术运算前把 Series 降维 —— 漏斗比率要的是 sum/sum
            left = _reduce(self._eval(node.left))
            right = _reduce(self._eval(node.right))
            op = _BIN_OPS.get(type(node.op))
            if op is None:
                raise UnsafeFormulaError(f"不支持的运算符：{type(node.op).__name__}")
            return op(left, right)

        if isinstance(node, ast.UnaryOp):
            operand = _reduce(self._eval(node.operand))
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.UAdd):
                return operand
            if isinstance(node.op, ast.Not):
                return not operand
            raise UnsafeFormulaError("不支持的一元运算符。")

        if isinstance(node, ast.Compare):
            left = _reduce(self._eval(node.left))
            for op, comparator in zip(node.ops, node.comparators):
                right = _reduce(self._eval(comparator))
                fn = _CMP_OPS.get(type(op))
                if fn is None:
                    raise UnsafeFormulaError("不支持的比较运算符。")
                if not fn(left, right):
                    return False
                left = right
            return True

        if isinstance(node, ast.BoolOp):
            values = [bool(_reduce(self._eval(v))) for v in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)

        if isinstance(node, ast.IfExp):
            cond = bool(_reduce(self._eval(node.test)))
            return self._eval(node.body) if cond else self._eval(node.orelse)

        if isinstance(node, ast.Call):
            # 函数拿到未降维的原始值（通常是 Series）
            args = [self._eval(a) for a in node.args]
            return _ALLOWED_FUNCS[node.func.id](*args)  # type: ignore[attr-defined]

        raise UnsafeFormulaError(f"不支持的表达式节点：{type(node).__name__}")


# --------------------------------------------------------------------------
# 指标引擎
# --------------------------------------------------------------------------

class MetricEngine:
    """执行 MetricStep 的 formula，产出带溯源的 MetricResult。"""

    def __init__(self, dataframe: pd.DataFrame, unit_override: dict[str, str] | None = None):
        self.df = dataframe
        self.row_count = len(dataframe)
        # Skill 显式声明的单位，优先于按名字推断
        self.unit_override = unit_override or {}

    def _build_variables(self, computed: dict[str, MetricResult]) -> dict[str, Any]:
        """可用变量 = CSV 各列 + 上游已算出的指标。"""
        variables: dict[str, Any] = {col: self.df[col] for col in self.df.columns}
        for name, result in computed.items():
            variables[name] = result.value
        return variables

    def execute_formula(
        self, formula: str, computed: dict[str, MetricResult]
    ) -> dict[str, MetricResult]:
        """
        执行一个 formula 块，可包含多行赋值语句。

        每行格式：`指标名 = 表达式`
        后面的行可以引用前面行刚算出的指标。
        """
        results: dict[str, MetricResult] = {}
        # 已算出的指标在块内累积可见
        local_computed = dict(computed)

        for raw_line in formula.replace(";", "\n").split("\n"):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise MetricError(
                    f"公式行缺少 '='，无法确定指标名：{line!r}。"
                    f"正确格式：'指标名 = 表达式'"
                )

            metric_name, expression = line.split("=", 1)
            metric_name = metric_name.strip()
            expression = expression.strip()

            evaluator = SafeEvaluator(self._build_variables(local_computed))
            warning = None
            try:
                value = evaluator.evaluate(expression)
            except MetricError:
                raise
            except Exception as exc:  # pragma: no cover - 兜底
                raise MetricError(f"计算 '{metric_name}' 失败：{exc}") from exc

            # 数值合理性检查
            if isinstance(value, float):
                if math.isnan(value):
                    warning = "计算结果为 NaN，可能是源数据存在空值"
                    value = 0.0
                elif math.isinf(value):
                    warning = "计算结果为无穷大，可能存在除零"
                    value = 0.0

            result = MetricResult(
                name=metric_name,
                value=value,
                formula=f"{metric_name} = {expression}",
                depends_on=evaluator.referenced,
                row_count=self.row_count,
                warning=warning,
                unit=infer_unit(metric_name, self.unit_override),
            )
            results[metric_name] = result
            local_computed[metric_name] = result

        if not results:
            raise MetricError(f"公式块中没有可执行的赋值语句：{formula!r}")

        return results
