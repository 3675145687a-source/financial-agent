"""
calculator.py
==============
所有财务计算逻辑的唯一归属地。
网页层 (app.py) 不做任何计算，只调用本模块的公开函数。

数据流:
  raw_financial_data (dict) --> calculate_derived() --> derived_data (dict)
  derived_data             --> assess_risks()       --> risk_result (dict)
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

# ============================================================
# 常量
# ============================================================

EPSILON = 1e-9  # 防除零，仅用于派生指标计算

# 风险等级排序权重（数值越大越严重）
LEVEL_WEIGHT = {"严重": 4, "高": 3, "中": 2, "低": 1}

# ============================================================
# 数据结构
# ============================================================


@dataclass
class FinancialData:
    """单期财务数据（可缺失）。"""

    revenue: Optional[float] = None      # 营业收入
    net_profit: Optional[float] = None   # 净利润
    debt_ratio: Optional[float] = None    # 资产负债率
    period: Optional[str] = None          # 期次标识（单期为 None）


@dataclass
class DerivedMetrics:
    """派生指标（白名单内计算结果）。"""

    net_margin: Optional[float] = None                       # 净利率 = net_profit / revenue
    delta_revenue: Optional[list[float]] = None             # 相邻期营收差值列表
    qoq_revenue: Optional[list[float]] = None               # 营收环比列表
    consecutive_decline_revenue: Optional[int] = None        # 营收连续下降期数
    delta_debt_ratio: Optional[list[float]] = None           # 相邻期负债率差值列表
    consecutive_rise_debt_ratio: Optional[int] = None        # 负债率连续上升期数
    consecutive_decline_profit: Optional[int] = None         # 净利润连续下降期数
    delta_revenue_pct: Optional[list[float]] = None          # 营收变动幅度列表


@dataclass
class RiskPoint:
    """单条风险点。"""

    risk_id: str
    risk_category: str          # 盈利能力风险 | 偿债风险 | 营收健康度风险 | 多期趋势风险 | 综合交叉风险
    risk_level: str             # 严重 | 高 | 中 | 低
    rule_matched: str
    description: str
    data_evidence: dict         # {"field": ..., "value": ..., "threshold": ...}
    confidence: str             # high | medium | low
    recommendation: str
    scope: str                  # latest | full_series
    period: Optional[str] = None


# ============================================================
# 公开函数
# ============================================================

def calculate_derived(financial_data_list: list[FinancialData]) -> DerivedMetrics:
    """
    根据原始财务数据计算派生指标（白名单内）。
    单期数据仅计算 net_margin；多期数据计算全部趋势指标。

    参数:
        financial_data_list: FinancialData 列表（单期时长度为 1）

    返回:
        DerivedMetrics 对象
    """
    metrics = DerivedMetrics()
    n = len(financial_data_list)

    # --- net_margin（仅最新一期，且 revenue > 0）---
    latest = financial_data_list[-1]
    if latest.revenue is not None and latest.net_profit is not None and latest.revenue > 0:
        metrics.net_margin = latest.net_profit / latest.revenue

    if n < 2:
        return metrics

    # --- 多期趋势指标 ---
    revenues = [d.revenue for d in financial_data_list if d.revenue is not None]
    profits = [d.net_profit for d in financial_data_list if d.net_profit is not None]
    debts = [d.debt_ratio for d in financial_data_list if d.debt_ratio is not None]

    # delta_revenue / qoq_revenue / delta_revenue_pct
    delta_rev: list[float] = []
    qoq_rev: list[float] = []
    delta_rev_pct: list[float] = []
    for i in range(1, n):
        prev = financial_data_list[i - 1]
        curr = financial_data_list[i]
        if prev.revenue is not None and curr.revenue is not None:
            dr = curr.revenue - prev.revenue
            delta_rev.append(dr)
            delta_rev_pct.append(abs(dr) / max(prev.revenue, EPSILON))
            if prev.revenue > 0:
                qoq_rev.append(dr / prev.revenue)

    metrics.delta_revenue = delta_rev if delta_rev else None
    metrics.qoq_revenue = qoq_rev if qoq_rev else None
    metrics.delta_revenue_pct = delta_rev_pct if delta_rev_pct else None

    # consecutive_decline_revenue
    if len(revenues) == n:
        decl = 0
        for i in range(1, n):
            if revenues[i] < revenues[i - 1]:
                decl += 1
            else:
                break  # 从最新一期往前看连续下降
        # 从末尾往前数连续下降
        decl = 0
        for i in range(n - 1, 0, -1):
            if revenues[i] < revenues[i - 1]:
                decl += 1
            else:
                break
        metrics.consecutive_decline_revenue = decl if decl > 0 else None

    # consecutive_decline_profit
    if len(profits) == n:
        decl_p = 0
        for i in range(n - 1, 0, -1):
            if profits[i] < profits[i - 1]:
                decl_p += 1
            else:
                break
        metrics.consecutive_decline_profit = decl_p if decl_p > 0 else None

    # delta_debt_ratio / consecutive_rise_debt_ratio
    delta_debt: list[float] = []
    if len(debts) == n:
        for i in range(1, n):
            delta_debt.append(debts[i] - debts[i - 1])
        metrics.delta_debt_ratio = delta_debt if delta_debt else None

        rise = 0
        for i in range(n - 1, 0, -1):
            if debts[i] > debts[i - 1]:
                rise += 1
            else:
                break
        metrics.consecutive_rise_debt_ratio = rise if rise > 0 else None

    return metrics


def assess_risks(
    financial_data_list: list[FinancialData],
    metrics: DerivedMetrics,
) -> list[RiskPoint]:
    """
    根据原始数据和派生指标判定风险点。
    规则体系复用 financial_risk_system_prompt.md 第五节，阈值完全一致。

    返回:
        RiskPoint 列表（已经过去重和排序）
    """
    risks: list[RiskPoint] = []
    risk_counter = 0

    def next_id() -> str:
        nonlocal risk_counter
        risk_counter += 1
        return f"RISK-{risk_counter:03d}"

    n = len(financial_data_list)
    latest = financial_data_list[-1]
    is_multi = n >= 2
    latest_period = latest.period

    # ----------------------------------------------------------
    # 5.1 盈利能力风险（作用域：最新一期；revenue == 0 时跳过）
    # ----------------------------------------------------------
    profit_risk_triggered = False  # 用于去重策略 A 标记
    if latest.revenue is not None and latest.revenue != 0:
        # net_profit < 0 -> 高
        if latest.net_profit is not None and latest.net_profit < 0:
            profit_risk_triggered = True
            risks.append(RiskPoint(
                risk_id=next_id(),
                risk_category="盈利能力风险",
                risk_level="高",
                rule_matched="5.1 net_profit < 0",
                description=f"当期净利润为 {latest.net_profit}，为负值",
                data_evidence={"field": "net_profit", "value": latest.net_profit, "threshold": 0},
                confidence="high",
                recommendation="建议补充成本结构数据以分析亏损原因",
                scope="latest",
                period=latest_period,
            ))
        # net_profit == 0 -> 中
        elif latest.net_profit is not None and latest.net_profit == 0:
            risks.append(RiskPoint(
                risk_id=next_id(),
                risk_category="盈利能力风险",
                risk_level="中",
                rule_matched="5.1 net_profit == 0",
                description="当期净利润为 0",
                data_evidence={"field": "net_profit", "value": 0, "threshold": 0},
                confidence="high",
                recommendation="建议复核净利润数据准确性",
                scope="latest",
                period=latest_period,
            ))
        # net_margin 相关
        if metrics.net_margin is not None:
            if 0 < metrics.net_margin < 0.02:
                risks.append(RiskPoint(
                    risk_id=next_id(),
                    risk_category="盈利能力风险",
                    risk_level="中",
                    rule_matched="5.1 0 < net_margin < 0.02",
                    description=f"净利率为 {metrics.net_margin:.4f}，低于 0.02",
                    data_evidence={"field": "net_margin", "value": round(metrics.net_margin, 6), "threshold": 0.02},
                    confidence="high",
                    recommendation="建议补充毛利率数据以进一步评估盈利能力",
                    scope="latest",
                    period=latest_period,
                ))
            elif 0.02 <= metrics.net_margin < 0.05:
                risks.append(RiskPoint(
                    risk_id=next_id(),
                    risk_category="盈利能力风险",
                    risk_level="低",
                    rule_matched="5.1 0.02 <= net_margin < 0.05",
                    description=f"净利率为 {metrics.net_margin:.4f}，处于 0.02 至 0.05 之间",
                    data_evidence={"field": "net_margin", "value": round(metrics.net_margin, 6), "threshold": "0.02-0.05"},
                    confidence="high",
                    recommendation="建议复核 5.1 盈利能力规则",
                    scope="latest",
                    period=latest_period,
                ))

    # ----------------------------------------------------------
    # 5.2 偿债风险（作用域：最新一期）
    # ----------------------------------------------------------
    if latest.debt_ratio is not None:
        if latest.debt_ratio > 0.7:
            risks.append(RiskPoint(
                risk_id=next_id(),
                risk_category="偿债风险",
                risk_level="高",
                rule_matched="5.2 debt_ratio > 0.7",
                description=f"资产负债率为 {latest.debt_ratio}，超过 0.7",
                data_evidence={"field": "debt_ratio", "value": latest.debt_ratio, "threshold": 0.7},
                confidence="high",
                recommendation="建议补充有息负债占比字段以细化偿债能力评估",
                scope="latest",
                period=latest_period,
            ))
        elif 0.6 < latest.debt_ratio <= 0.7:
            risks.append(RiskPoint(
                risk_id=next_id(),
                risk_category="偿债风险",
                risk_level="中",
                rule_matched="5.2 0.6 < debt_ratio <= 0.7",
                description=f"资产负债率为 {latest.debt_ratio}，处于 0.6 至 0.7 之间",
                data_evidence={"field": "debt_ratio", "value": latest.debt_ratio, "threshold": "0.6-0.7"},
                confidence="high",
                recommendation="建议关注负债结构变化",
                scope="latest",
                period=latest_period,
            ))
        elif 0.4 <= latest.debt_ratio <= 0.6:
            risks.append(RiskPoint(
                risk_id=next_id(),
                risk_category="偿债风险",
                risk_level="低",
                rule_matched="5.2 0.4 <= debt_ratio <= 0.6",
                description=f"资产负债率为 {latest.debt_ratio}，处于 0.4 至 0.6 之间",
                data_evidence={"field": "debt_ratio", "value": latest.debt_ratio, "threshold": "0.4-0.6"},
                confidence="high",
                recommendation="建议复核 5.2 偿债风险规则",
                scope="latest",
                period=latest_period,
            ))

    # ----------------------------------------------------------
    # 5.3 营收健康度风险
    # ----------------------------------------------------------
    # 单期：revenue == 0
    if latest.revenue is not None and latest.revenue == 0:
        risks.append(RiskPoint(
            risk_id=next_id(),
            risk_category="营收健康度风险",
            risk_level="高",
            rule_matched="5.3 revenue == 0",
            description="当期营业收入为 0",
            data_evidence={"field": "revenue", "value": 0, "threshold": 0},
            confidence="high",
            recommendation="建议核实营收数据来源",
            scope="latest",
            period=latest_period,
        ))

    # 多期趋势
    if is_multi:
        # 连续下降
        if metrics.consecutive_decline_revenue is not None:
            if metrics.consecutive_decline_revenue >= 3:
                risks.append(RiskPoint(
                    risk_id=next_id(),
                    risk_category="营收健康度风险",
                    risk_level="高",
                    rule_matched=f"5.3 consecutive_decline_revenue >= 3 ({metrics.consecutive_decline_revenue})",
                    description=f"营业收入连续 {metrics.consecutive_decline_revenue} 期下降",
                    data_evidence={"field": "consecutive_decline_revenue", "value": metrics.consecutive_decline_revenue, "threshold": 3},
                    confidence="high",
                    recommendation="建议补充营收分产品/分地区明细字段以定位下降来源",
                    scope="full_series",
                    period=latest_period,
                ))
            elif metrics.consecutive_decline_revenue == 2:
                risks.append(RiskPoint(
                    risk_id=next_id(),
                    risk_category="营收健康度风险",
                    risk_level="中",
                    rule_matched="5.3 consecutive_decline_revenue == 2",
                    description="营业收入连续 2 期下降",
                    data_evidence={"field": "consecutive_decline_revenue", "value": 2, "threshold": 2},
                    confidence="medium",
                    recommendation="建议补充更多期次数据确认趋势持续性",
                    scope="full_series",
                    period=latest_period,
                ))

        # 营收波动
        if metrics.delta_revenue_pct:
            max_pct = max(metrics.delta_revenue_pct)
            # 检查序列非单调（同时存在上升和下降）
            delta_revs = metrics.delta_revenue or []
            has_up = any(d > 0 for d in delta_revs)
            has_down = any(d < 0 for d in delta_revs)
            non_monotonic = has_up and has_down
            if max_pct > 0.2 and non_monotonic:
                risks.append(RiskPoint(
                    risk_id=next_id(),
                    risk_category="营收健康度风险",
                    risk_level="中",
                    rule_matched=f"5.3 delta_revenue_pct > 0.2 且序列非单调 (max={max_pct:.4f})",
                    description=f"营收波动幅度最大为 {max_pct:.4f}，超过 0.2 且序列非单调",
                    data_evidence={"field": "delta_revenue_pct", "value": round(max_pct, 6), "threshold": 0.2},
                    confidence="medium",
                    recommendation="建议复核营收波动原因",
                    scope="full_series",
                    period=latest_period,
                ))

    # ----------------------------------------------------------
    # 5.4 多期趋势风险（作用域：完整序列）
    # ----------------------------------------------------------
    if is_multi:
        # net_profit 由正转负
        if n >= 2 and latest.net_profit is not None:
            prev = financial_data_list[-2]
            if prev.net_profit is not None and prev.net_profit > 0 and latest.net_profit < 0:
                risks.append(RiskPoint(
                    risk_id=next_id(),
                    risk_category="多期趋势风险",
                    risk_level="高",
                    rule_matched="5.4 net_profit 由正转负",
                    description=f"净利润由 {prev.net_profit} 转为 {latest.net_profit}",
                    data_evidence={"field": "net_profit", "value": f"{prev.net_profit} -> {latest.net_profit}", "threshold": "前期 > 0 且当期 < 0"},
                    confidence="high",
                    recommendation="建议补充成本结构数据以分析利润变化原因",
                    scope="full_series",
                    period=latest_period,
                ))

        # consecutive_decline_profit >= 2
        if metrics.consecutive_decline_profit is not None and metrics.consecutive_decline_profit >= 2:
            risks.append(RiskPoint(
                risk_id=next_id(),
                risk_category="多期趋势风险",
                risk_level="中",
                rule_matched=f"5.4 consecutive_decline_profit >= 2 ({metrics.consecutive_decline_profit})",
                description=f"净利润连续 {metrics.consecutive_decline_profit} 期下降",
                data_evidence={"field": "consecutive_decline_profit", "value": metrics.consecutive_decline_profit, "threshold": 2},
                confidence="high" if metrics.consecutive_decline_profit >= 3 else "medium",
                recommendation="建议复核 5.4 趋势规则并补充更多期次数据",
                scope="full_series",
                period=latest_period,
            ))

        # debt_ratio 连续上升 >= 2
        if metrics.consecutive_rise_debt_ratio is not None and metrics.consecutive_rise_debt_ratio >= 2:
            risks.append(RiskPoint(
                risk_id=next_id(),
                risk_category="多期趋势风险",
                risk_level="中",
                rule_matched=f"5.4 debt_ratio 连续上升 >= 2 ({metrics.consecutive_rise_debt_ratio})",
                description=f"资产负债率连续 {metrics.consecutive_rise_debt_ratio} 期上升",
                data_evidence={"field": "consecutive_rise_debt_ratio", "value": metrics.consecutive_rise_debt_ratio, "threshold": 2},
                confidence="high" if metrics.consecutive_rise_debt_ratio >= 3 else "medium",
                recommendation="建议补充负债结构明细字段以分析负债率上升原因",
                scope="full_series",
                period=latest_period,
            ))

        # delta_debt_ratio > 0.1（单期骤升）
        if metrics.delta_debt_ratio:
            max_delta = max(metrics.delta_debt_ratio)
            if max_delta > 0.1:
                risks.append(RiskPoint(
                    risk_id=next_id(),
                    risk_category="多期趋势风险",
                    risk_level="高",
                    rule_matched=f"5.4 delta_debt_ratio > 0.1 (max={max_delta:.4f})",
                    description=f"负债率单期上升幅度为 {max_delta:.4f}，超过 0.1",
                    data_evidence={"field": "delta_debt_ratio", "value": round(max_delta, 6), "threshold": 0.1},
                    confidence="high",
                    recommendation="建议复核负债率数据并排查骤升原因",
                    scope="full_series",
                    period=latest_period,
                ))

    # ----------------------------------------------------------
    # 5.5 综合交叉风险（作用域：最新一期）
    # ----------------------------------------------------------
    cross_risk_triggered = False
    if latest.net_profit is not None and latest.debt_ratio is not None:
        if latest.net_profit < 0 and latest.debt_ratio > 0.7:
            cross_risk_triggered = True
            risks.append(RiskPoint(
                risk_id=next_id(),
                risk_category="综合交叉风险",
                risk_level="严重",
                rule_matched="5.5 net_profit < 0 且 debt_ratio > 0.7",
                description=f"净利润为 {latest.net_profit} 且资产负债率为 {latest.debt_ratio}，净利润为负且负债率超过 0.7",
                data_evidence={"field": "net_profit, debt_ratio", "value": f"net_profit={latest.net_profit}, debt_ratio={latest.debt_ratio}", "threshold": "net_profit < 0 且 debt_ratio > 0.7"},
                confidence="high",
                recommendation="建议补充流动比率、速动比率字段以进一步评估偿债能力",
                scope="latest",
                period=latest_period,
            ))
        elif metrics.net_margin is not None and metrics.net_margin < 0.02 and latest.debt_ratio > 0.6:
            risks.append(RiskPoint(
                risk_id=next_id(),
                risk_category="综合交叉风险",
                risk_level="高",
                rule_matched="5.5 net_margin < 0.02 且 debt_ratio > 0.6",
                description=f"净利率为 {metrics.net_margin:.4f} 且资产负债率为 {latest.debt_ratio}",
                data_evidence={"field": "net_margin, debt_ratio", "value": f"net_margin={round(metrics.net_margin, 6)}, debt_ratio={latest.debt_ratio}", "threshold": "net_margin < 0.02 且 debt_ratio > 0.6"},
                confidence="high",
                recommendation="建议复核 5.5 交叉风险规则",
                scope="latest",
                period=latest_period,
            ))

    # ----------------------------------------------------------
    # 去重策略 A
    # ----------------------------------------------------------
    risks = _dedup_risks(risks, cross_risk_triggered, profit_risk_triggered)

    # ----------------------------------------------------------
    # 排序：严重 > 高 > 中 > 低，同级按 risk_id 升序
    # ----------------------------------------------------------
    risks.sort(key=lambda r: (-LEVEL_WEIGHT.get(r.risk_level, 0), r.risk_id))

    return risks


def _dedup_risks(
    risks: list[RiskPoint],
    cross_risk_triggered: bool,
    profit_risk_triggered: bool,
) -> list[RiskPoint]:
    """
    去重策略 A:
    1. 若触发 5.5 交叉风险（net_profit < 0 且 debt_ratio > 0.7），
       则抑制同周期仅由 5.1 net_profit < 0 触发的单维盈利风险。
    2. 其余按 rule_matched 的规则编号前缀（5.1/5.2/5.3/5.4/5.5）
       + 具体条件键 + period 去重；不同规则条目不得互相挤掉。
       同一规则编号 + 同一条件 + 同一 period 只保留最高等级一条。
    """
    # 步骤 1：抑制 —— 5.5 触发时移除 5.1 的 net_profit < 0
    if cross_risk_triggered and profit_risk_triggered:
        risks = [
            r for r in risks
            if not (
                r.risk_category == "盈利能力风险"
                and "net_profit < 0" in r.rule_matched
            )
        ]

    # 步骤 2：按 (规则编号前缀, 条件键, period) 去重
    groups: dict[str, list[RiskPoint]] = {}
    for r in risks:
        rule_prefix, condition_key = _parse_rule_key(r.rule_matched)
        key = f"{rule_prefix}__{condition_key}__{r.period}"
        groups.setdefault(key, []).append(r)

    deduped: list[RiskPoint] = []
    for key, group in groups.items():
        group.sort(key=lambda r: -LEVEL_WEIGHT.get(r.risk_level, 0))
        deduped.append(group[0])

    return deduped


def _parse_rule_key(rule_matched: str) -> tuple[str, str]:
    """
    从 rule_matched 字符串中提取 (规则编号前缀, 条件键)。

    示例:
      "5.1 net_profit < 0"             -> ("5.1", "net_profit<0")
      "5.1 0.02 <= net_margin < 0.05"  -> ("5.1", "0.02<=net_margin<0.05")
      "5.4 net_profit 由正转负"        -> ("5.4", "net_profit由正转负")
      "5.4 consecutive_decline_profit >= 2 (3)" -> ("5.4", "consecutive_decline_profit>=2")
      "5.5 net_profit < 0 且 debt_ratio > 0.7"  -> ("5.5", "net_profit<0且debt_ratio>0.7")

    返回:
        (规则编号, 条件键) —— 条件键去除空格和括号内容，用于精确区分不同规则条目
    """
    # 提取规则编号前缀（如 "5.1", "5.4"）
    prefix_match = re.match(r"^(\d+\.\d+)\s+", rule_matched)
    if prefix_match:
        prefix = prefix_match.group(1)
        remainder = rule_matched[prefix_match.end():]
    else:
        prefix = "unknown"
        remainder = rule_matched

    # 条件键：去除空格、去除括号内的补充说明、去除序号前缀
    condition_key = remainder.strip()
    # 去除括号内容
    condition_key = re.sub(r"\([^)]*\)", "", condition_key)
    # 去除空格
    condition_key = condition_key.replace(" ", "")

    return (prefix, condition_key)


def summarize_risks(risks: list[RiskPoint]) -> dict:
    """
    生成风险摘要。

    返回:
        {
            "total_risks": int,
            "risk_distribution": {"严重": int, "高": int, "中": int, "低": int},
            "overall_risk_level": str,
        }
    """
    dist = {"严重": 0, "高": 0, "中": 0, "低": 0}
    for r in risks:
        if r.risk_level in dist:
            dist[r.risk_level] += 1

    if not risks:
        overall = "无显著风险"
    else:
        overall = max(risks, key=lambda r: LEVEL_WEIGHT.get(r.risk_level, 0)).risk_level

    return {
        "total_risks": len(risks),
        "risk_distribution": dist,
        "overall_risk_level": overall,
    }


def format_financial_table(financial_data_list: list[FinancialData], metrics: DerivedMetrics) -> list[dict]:
    """
    将财务数据格式化为表格行（供 Streamlit st.dataframe 使用）。

    返回:
        list[dict]，每行包含：期次、营收、净利润、负债率、净利率
    """
    rows = []
    for i, fd in enumerate(financial_data_list):
        row = {
            "期次": fd.period if fd.period else f"第{i+1}期",
            "营收(元)": fd.revenue if fd.revenue is not None else "N/A",
            "净利润(元)": fd.net_profit if fd.net_profit is not None else "N/A",
            "资产负债率": fd.debt_ratio if fd.debt_ratio is not None else "N/A",
        }
        # 净利率仅对最新一期显示
        if i == len(financial_data_list) - 1 and metrics.net_margin is not None:
            row["净利率"] = round(metrics.net_margin, 6)
        else:
            row["净利率"] = "N/A"
        rows.append(row)
    return rows


def risk_point_to_dict(r: RiskPoint) -> dict:
    """将 RiskPoint 转为可 JSON 序列化的 dict。"""
    d = asdict(r)
    return d
