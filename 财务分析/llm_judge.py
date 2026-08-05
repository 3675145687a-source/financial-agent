"""
llm_judge.py
=============
风险判定模块。

默认路径: 规则引擎 (calculator.assess_risks + summarize_risks)
  - 基于固定阈值和判定规则生成风险点，结果权威且可复现
  - 无需任何外部依赖，启动即可用

可选路径: 大模型 API (仅当环境变量 USE_LLM=1 时启用)
  - 加载 financial_risk_system_prompt.md 作为 System Prompt
  - 将财务数据构造为输入 JSON，发送给大模型
  - 解析大模型返回的 JSON，转为结构化结果
  - 未配置 API 或调用失败时，返回明确错误，不静默回退到规则引擎

通过 JudgeResult.engine 字段可区分结果来源。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from typing import Optional

from calculator import (
    FinancialData,
    DerivedMetrics,
    RiskPoint,
    assess_risks,
    summarize_risks,
    risk_point_to_dict,
)


# ============================================================
# 数据结构
# ============================================================

@dataclass
class JudgeResult:
    """风险判定结果。"""

    success: bool
    engine: str = ""                       # "rule_engine" | "llm" | ""
    input_json: str = ""                   # 构造的输入 JSON（调试用）
    risk_points: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    disclaimer: str = ""
    raw_response: str = ""                 # 原始返回（调试用，仅 LLM 模式有值）
    error: str = ""


# ============================================================
# 常量
# ============================================================

PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "financial_risk_system_prompt.md")

# 默认免责声明
DEFAULT_DISCLAIMER = (
    "本分析仅基于输入数据中的营收、净利润、负债率三项指标"
    "及本Prompt定义的派生指标白名单生成，"
    "不构成投资建议、不构成对任何实体的评价。"
    "分析结果受限于输入数据的完整性与准确性。"
)


# ============================================================
# 公开函数
# ============================================================

def get_engine_mode() -> str:
    """返回当前判定引擎模式: 'llm' 或 'rule_engine'。"""
    return "llm" if os.environ.get("USE_LLM") == "1" else "rule_engine"


def load_system_prompt() -> str:
    """
    加载 financial_risk_system_prompt.md 的内容。
    仅在 LLM 模式下使用。
    """
    try:
        with open(PROMPT_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def build_input_json(financial_data_list: list[FinancialData]) -> str:
    """
    根据财务数据构造符合 System Prompt 第二节"输入模式"的 JSON。
    单期 -> single 模式；多期 -> multi 模式。
    """
    n = len(financial_data_list)
    if n == 1:
        fd = financial_data_list[0]
        data = {
            "revenue": fd.revenue,
            "net_profit": fd.net_profit,
            "debt_ratio": fd.debt_ratio,
        }
    else:
        periods = []
        for fd in financial_data_list:
            periods.append({
                "period": fd.period or "",
                "revenue": fd.revenue,
                "net_profit": fd.net_profit,
                "debt_ratio": fd.debt_ratio,
            })
        data = {"periods": periods}

    return json.dumps(data, ensure_ascii=False)


def judge(
    financial_data_list: list[FinancialData],
    metrics: DerivedMetrics,
) -> JudgeResult:
    """
    主入口：根据引擎模式选择判定路径。

    - 默认 (USE_LLM 未设置或 != 1): 调用规则引擎 assess_risks + summarize_risks
    - USE_LLM=1: 调用大模型 API (需要配置 API_KEY 等)
    """
    input_json = build_input_json(financial_data_list)
    mode = get_engine_mode()

    if mode == "llm":
        return _judge_via_llm(financial_data_list, metrics, input_json)
    else:
        return _judge_via_rule_engine(financial_data_list, metrics, input_json)


# ============================================================
# 规则引擎路径（默认）
# ============================================================

def _judge_via_rule_engine(
    financial_data_list: list[FinancialData],
    metrics: DerivedMetrics,
    input_json: str,
) -> JudgeResult:
    """使用 calculator 规则引擎生成风险判定结果。"""
    risks = assess_risks(financial_data_list, metrics)
    summary = summarize_risks(risks)

    return JudgeResult(
        success=True,
        engine="rule_engine",
        input_json=input_json,
        risk_points=[risk_point_to_dict(r) for r in risks],
        summary=summary,
        disclaimer=DEFAULT_DISCLAIMER,
    )


# ============================================================
# 大模型路径（可选，USE_LLM=1 时启用）
# ============================================================

def _judge_via_llm(
    financial_data_list: list[FinancialData],
    metrics: DerivedMetrics,
    input_json: str,
) -> JudgeResult:
    """
    使用大模型 API 进行风险判定。
    需要配置环境变量:
      - USE_LLM=1
      - LLM_API_KEY: API 密钥
      - LLM_API_URL: API 端点 (可选，有默认值)

    未配置 API_KEY 时返回明确错误，不静默回退。
    """
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        return JudgeResult(
            success=False,
            engine="llm",
            input_json=input_json,
            error="LLM 模式已启用 (USE_LLM=1) 但未配置 LLM_API_KEY 环境变量，无法调用大模型。请设置 LLM_API_KEY 或取消 USE_LLM 以使用规则引擎。",
        )

    system_prompt = load_system_prompt()
    if not system_prompt:
        return JudgeResult(
            success=False,
            engine="llm",
            input_json=input_json,
            error="System Prompt 文件未找到或为空，无法调用大模型。",
        )

    # 调用大模型 API
    llm_response = _call_llm_api(input_json, system_prompt, api_key)

    if llm_response.get("error"):
        return JudgeResult(
            success=False,
            engine="llm",
            input_json=input_json,
            error=llm_response["error"],
        )

    # 解析大模型返回
    risk_points_raw = llm_response.get("risk_points", [])
    risk_points = [_dict_to_risk_point(rp) for rp in risk_points_raw]
    summary = llm_response.get("summary", summarize_risks(risk_points))
    disclaimer = llm_response.get("disclaimer", DEFAULT_DISCLAIMER)

    return JudgeResult(
        success=True,
        engine="llm",
        input_json=input_json,
        risk_points=[risk_point_to_dict(rp) for rp in risk_points],
        summary=summary,
        disclaimer=disclaimer,
        raw_response=json.dumps(llm_response, ensure_ascii=False, indent=2),
    )


# ============================================================
# 内部函数
# ============================================================

def _call_llm_api(input_json: str, system_prompt: str, api_key: str) -> dict:
    """
    调用大模型 API。

    TODO: 替换为真实 API 调用。
    当前为伪代码框架，实际调用时需取消注释并填入正确的端点和参数。

    接入示例::

        import requests
        api_url = os.environ.get("LLM_API_URL", "https://api.example.com/v1/chat/completions")
        resp = requests.post(
            api_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": os.environ.get("LLM_MODEL", "your-model"),
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": input_json},
                ],
            },
            timeout=30,
        )
        resp.raise_for_status()
        return json.loads(resp.json()["choices"][0]["message"]["content"])
    """
    # 未接入真实 API，返回明确错误
    return {
        "error": "大模型 API 尚未接入。请在 _call_llm_api() 中实现真实 API 调用，或取消设置 USE_LLM 以使用规则引擎。"
    }


def _dict_to_risk_point(d: dict) -> RiskPoint:
    """将 dict 转为 RiskPoint 对象。"""
    return RiskPoint(
        risk_id=d.get("risk_id", ""),
        risk_category=d.get("risk_category", ""),
        risk_level=d.get("risk_level", ""),
        rule_matched=d.get("rule_matched", ""),
        description=d.get("description", ""),
        data_evidence=d.get("data_evidence", {}),
        confidence=d.get("confidence", ""),
        recommendation=d.get("recommendation", ""),
        scope=d.get("scope", ""),
        period=d.get("period"),
    )
