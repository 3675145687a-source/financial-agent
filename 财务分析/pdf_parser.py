"""
pdf_parser.py
==============
从上传的 PDF 文件中提取文本，并解析出三个关键财务字段：
  - revenue    (营业收入)
  - net_profit (净利润)
  - debt_ratio (资产负债率)

解析失败或字段不全时返回包含错误信息的 ParseResult，不抛异常。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import pdfplumber

from calculator import FinancialData


# ============================================================
# 数据结构
# ============================================================

@dataclass
class ParseResult:
    """PDF 解析结果。"""

    success: bool
    financial_data: Optional[FinancialData] = None
    raw_text: str = ""              # 提取的原始文本（调试用）
    extracted_fields: dict = field(default_factory=dict)  # 已提取的原始字段值
    error: str = ""                 # 错误信息


# ============================================================
# 关键词模式
# ============================================================
# 常见财报中"营业收入"的同义表述（中英文）
# 捕获组 1 = 数值，捕获组 2 = 单位（元/万元/亿元，可能缺失）
REVENUE_PATTERNS = [
    r"营业收入[\s]*[:：]?\s*([\d,，.\-]+)\s*(元|万元|亿元)?",
    r"营业总收入[\s]*[:：]?\s*([\d,，.\-]+)\s*(元|万元|亿元)?",
    r"营收[\s]*[:：]?\s*([\d,，.\-]+)\s*(元|万元|亿元)?",
    r"[Rr]evenu\w*\s*[:：]?\s*([\d,，.\-]+)\s*(元|万元|亿元|yuan)?",
]

# "净利润"的同义表述（中英文）
PROFIT_PATTERNS = [
    r"净利润[\s]*[:：]?\s*([\d,，.\-]+)\s*(元|万元|亿元)?",
    r"归属[于]?上市公司股东[的]?净利润[\s]*[:：]?\s*([\d,，.\-]+)\s*(元|万元|亿元)?",
    r"[Nn]et\s*[Pp]rofit\s*[:：]?\s*([\d,，.\-]+)\s*(元|万元|亿元|yuan)?",
]

# "资产负债率"的同义表述（中英文）
DEBT_RATIO_PATTERNS = [
    r"资产负债率[\s]*[:：]?\s*([\d.]+)\s*%?",
    r"负债率[\s]*[:：]?\s*([\d.]+)\s*%?",
    r"[Dd]ebt\s*[Rr]atio\s*[:：]?\s*([\d.]+)\s*%?",
]

# 单位 -> 换算系数（目标单位：元）
UNIT_MULTIPLIER = {
    None: 1.0,      # 无单位，默认元
    "": 1.0,
    "元": 1.0,
    "万元": 1e4,
    "亿元": 1e8,
    "yuan": 1.0,
}


# ============================================================
# 公开函数
# ============================================================

def parse_pdf(pdf_file) -> ParseResult:
    """
    解析上传的 PDF 文件，提取关键财务数据。

    参数:
        pdf_file: Streamlit UploadedFile 对象或文件路径

    返回:
        ParseResult: 包含成功状态、FinancialData 和错误信息
    """
    # --- 提取文本 ---
    raw_text = ""
    try:
        with pdfplumber.open(pdf_file) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    raw_text += text + "\n"
    except Exception as e:
        return ParseResult(success=False, error=f"PDF 文件读取失败: {e}")

    if not raw_text.strip():
        return ParseResult(success=False, error="PDF 文件内容为空或无法提取文本（可能是扫描件）")

    # --- 提取字段 ---
    extracted: dict = {}

    revenue = _extract_field(raw_text, REVENUE_PATTERNS, "revenue")
    extracted["revenue_raw"] = revenue["raw"]
    extracted["revenue_unit"] = revenue["unit"]
    if revenue["value"] is not None:
        extracted["revenue"] = revenue["value"]

    net_profit = _extract_field(raw_text, PROFIT_PATTERNS, "net_profit")
    extracted["net_profit_raw"] = net_profit["raw"]
    extracted["net_profit_unit"] = net_profit["unit"]
    if net_profit["value"] is not None:
        extracted["net_profit"] = net_profit["value"]

    debt_ratio = _extract_field(raw_text, DEBT_RATIO_PATTERNS, "debt_ratio")
    extracted["debt_ratio_raw"] = debt_ratio["raw"]
    extracted["debt_ratio_unit"] = debt_ratio["unit"]
    if debt_ratio["value"] is not None:
        extracted["debt_ratio"] = debt_ratio["value"]

    # --- 校验完整性 ---
    missing = []
    if "revenue" not in extracted:
        missing.append("营收")
    if "net_profit" not in extracted:
        missing.append("净利润")
    if "debt_ratio" not in extracted:
        missing.append("资产负债率")

    if missing:
        return ParseResult(
            success=False,
            raw_text=raw_text,
            extracted_fields=extracted,
            error=f"以下字段未能从 PDF 中提取: {', '.join(missing)}",
        )

    # --- 构造 FinancialData ---
    fd = FinancialData(
        revenue=extracted["revenue"],
        net_profit=extracted["net_profit"],
        debt_ratio=extracted["debt_ratio"],
        period=None,  # 单期模式
    )

    return ParseResult(
        success=True,
        financial_data=fd,
        raw_text=raw_text,
        extracted_fields=extracted,
    )


# ============================================================
# 内部函数
# ============================================================

def _extract_field(text: str, patterns: list[str], field_name: str) -> dict:
    """
    用多个正则模式尝试从文本中提取一个字段值。
    支持双捕获组：组1=数值，组2=单位（元/万元/亿元，可能缺失）。

    返回:
        {"raw": 原始数值字符串, "unit": 单位字符串或 None, "value": 换算后的 float 或 None}
        - 对于 revenue/net_profit：按 UNIT_MULTIPLIER 换算为元
        - 对于 debt_ratio：做百分比 -> 小数转换，不参与单位换算
    """
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            raw_str = match.group(1)
            # 尝试获取单位捕获组（组2）
            unit = match.group(2) if match.lastindex and match.lastindex >= 2 else None
            value = _parse_number(raw_str)
            if value is not None:
                if field_name == "debt_ratio":
                    # 资产负债率：百分比 -> 小数，不参与金额单位换算
                    if value > 1:
                        value = value / 100
                    return {"raw": raw_str, "unit": "%", "value": value}
                else:
                    # 营收/净利润：按单位换算为元
                    multiplier = UNIT_MULTIPLIER.get(unit, 1.0)
                    converted = value * multiplier
                    unit_label = unit if unit else "元"
                    return {"raw": raw_str, "unit": unit_label, "value": converted}
    return {"raw": "", "unit": None, "value": None}


def _parse_number(raw_str: str) -> Optional[float]:
    """
    将提取到的原始字符串解析为 float。
    处理：中文逗号、千分位逗号、中文句号、空格等。
    """
    if not raw_str:
        return None
    # 清洗：去除中文逗号、千分位逗号
    cleaned = raw_str.replace(",", "").replace("，", "").replace(" ", "").strip()
    # 处理中文句号
    cleaned = cleaned.replace("。", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None
