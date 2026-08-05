"""
app.py
=======
财报风险扫描仪 - Streamlit 主应用 (Fynix 风格 Dashboard 版)

设计要点:
- 浅灰页面背景 (#F5F6F8)
- 圆角白色卡片 + 细腻阴影
- 左侧深色导航栏
- 绿色主色调 (#10B981)
- 大数字 Metric 卡片
- 风险评分 SVG 环形图
- 风险分布条形图
- 颜色编码的风险卡片列表

数据流（单向）:
  用户上传 PDF
    -> pdf_parser.parse_pdf()   提取财务数据（含单位换算）
    -> 用户确认/编辑数据（st.number_input）
    -> 用户点「确认数据」-> 写入 session_state.financial_data
    -> calculator.calculate_derived()  计算派生指标
    -> llm_judge.judge()        风险判定（默认规则引擎，USE_LLM=1 走大模型）
    -> 页面展示（Metric 卡片 + 风险仪表盘 + 数据表格 + 风险列表）
"""

from __future__ import annotations

import streamlit as st
import pandas as pd

from calculator import (
    FinancialData,
    DerivedMetrics,
    calculate_derived,
    format_financial_table,
)
from pdf_parser import parse_pdf
from llm_judge import judge, get_engine_mode


# ============================================================
# 工具函数: HTML/CSS 组件
# ============================================================

def _inject_css() -> None:
    """注入全局 CSS，覆盖 Streamlit 默认样式以匹配 Fynix 风格。"""
    css = """
    <style>
    /* 页面背景 */
    .stApp {
        background-color: #F5F6F8 !important;
    }
    /* 隐藏默认 header/footer */
    header {visibility: hidden;}
    footer {visibility: hidden;}
    /* 侧边栏深色 */
    [data-testid="stSidebar"] {
        background-color: #1E293B !important;
        border-right: none !important;
    }
    [data-testid="stSidebar"] .stMarkdown {
        color: #CBD5E1 !important;
    }
    /* 主区域 padding */
    [data-testid="stAppViewContainer"] > .main {
        padding: 0 32px 32px 32px !important;
    }
    /* 按钮样式 */
    .stButton > button {
        background-color: #10B981 !important;
        color: white !important;
        border-radius: 12px !important;
        border: none !important;
        padding: 12px 24px !important;
        font-weight: 600 !important;
        transition: all 0.2s ease !important;
        box-shadow: 0 1px 2px rgba(0,0,0,0.05) !important;
    }
    .stButton > button:hover {
        background-color: #059669 !important;
        box-shadow: 0 4px 12px rgba(16, 185, 129, 0.3) !important;
        transform: translateY(-1px);
    }
    .stButton > button:disabled {
        background-color: #D1D5DB !important;
        color: #9CA3AF !important;
        box-shadow: none !important;
        transform: none !important;
    }
    /* 文件上传区 */
    [data-testid="stFileUploader"] {
        background: white;
        border-radius: 16px;
        padding: 16px;
        border: 2px dashed #E5E7EB;
    }
    /* number_input 容器 */
    [data-testid="stNumberInput"] {
        background: white;
        border-radius: 12px;
        padding: 12px;
    }
    /* dataframe 样式 */
    .dataframe {
        border-radius: 12px !important;
        overflow: hidden !important;
    }
    /* divider */
    hr {
        border-color: #E5E7EB !important;
        margin: 24px 0 !important;
    }
    /* spinner */
    .stSpinner > div {
        border-top-color: #10B981 !important;
    }
    /* alert 样式覆盖 */
    .stAlert {
        border-radius: 12px !important;
    }
    </style>
    """
    st.markdown(css, unsafe_allow_html=True)


def _card(title: str, content_html: str, icon: str = "") -> None:
    """渲染白色圆角卡片。"""
    html = f"""
    <div style="
        background: white;
        border-radius: 16px;
        padding: 24px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.04);
        margin-bottom: 16px;
        border: 1px solid #E5E7EB;
    ">
        <div style="display: flex; align-items: center; margin-bottom: 16px;">
            <span style="font-size: 20px; margin-right: 10px;">{icon}</span>
            <h3 style="margin: 0; font-size: 16px; font-weight: 700; color: #111827;">{title}</h3>
        </div>
        {content_html}
    </div>
    """
    st.markdown(html, unsafe_allow_html=True)


def _metric_card(label: str, value: str, subtext: str = "", color: str = "#111827", bg_color: str = "white") -> str:
    """返回 metric 卡片的 HTML 字符串（用于嵌入 columns）。"""
    return f"""
    <div style="
        background: {bg_color};
        border-radius: 16px;
        padding: 20px 16px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.08);
        text-align: center;
        border: 1px solid #E5E7EB;
        height: 100%;
    ">
        <div style="font-size: 11px; color: #6B7280; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.8px; font-weight: 600;">{label}</div>
        <div style="font-size: 26px; font-weight: 700; color: {color}; line-height: 1.2;">{value}</div>
        {f'<div style="font-size: 11px; color: #9CA3AF; margin-top: 6px;">{subtext}</div>' if subtext else ''}
    </div>
    """


def _risk_badge(level: str) -> str:
    """返回风险等级徽章 HTML。"""
    styles = {
        "严重": "background: #FEE2E2; color: #991B1B; border: 1px solid #FECACA;",
        "高": "background: #FEE2E2; color: #DC2626; border: 1px solid #FECACA;",
        "中": "background: #FEF3C7; color: #B45309; border: 1px solid #FDE68A;",
        "低": "background: #EFF6FF; color: #1D4ED8; border: 1px solid #BFDBFE;",
        "无显著风险": "background: #D1FAE5; color: #065F46; border: 1px solid #A7F3D0;",
    }
    style = styles.get(level, styles["低"])
    return f'<span style="display: inline-block; padding: 4px 10px; border-radius: 20px; font-size: 11px; font-weight: 700; {style}">{level}</span>'


def _risk_score_ring(level: str, score_pct: int) -> str:
    """返回 SVG 风险评分环形图 HTML。"""
    colors = {
        "严重": "#DC2626",
        "高": "#EF4444",
        "中": "#F59E0B",
        "低": "#6B7280",
        "无显著风险": "#10B981",
    }
    color = colors.get(level, "#6B7280")
    # 环形进度: circumference = 2 * pi * 40 ≈ 251.2
    circ = 251.2
    offset = circ * (1 - score_pct / 100)
    return f"""
    <div style="display: flex; flex-direction: column; align-items: center; justify-content: center;">
        <svg width="140" height="140" viewBox="0 0 100 100">
            <circle cx="50" cy="50" r="40" fill="none" stroke="#E5E7EB" stroke-width="8" />
            <circle cx="50" cy="50" r="40" fill="none" stroke="{color}" stroke-width="8"
                stroke-linecap="round" stroke-dasharray="{circ}" stroke-dashoffset="{offset}"
                transform="rotate(-90 50 50)" />
            <text x="50" y="48" text-anchor="middle" font-size="22" font-weight="700" fill="#111827">{level}</text>
            <text x="50" y="64" text-anchor="middle" font-size="10" fill="#6B7280">风险等级</text>
        </svg>
    </div>
    """


def _risk_distribution_bars(dist: dict) -> str:
    """返回风险分布水平条形图 HTML。"""
    levels = [("严重", "#DC2626"), ("高", "#EF4444"), ("中", "#F59E0B"), ("低", "#6B7280")]
    total = sum(dist.values()) or 1
    bars = ""
    for name, color in levels:
        count = dist.get(name, 0)
        pct = (count / total) * 100 if total > 0 else 0
        bars += f"""
        <div style="display: flex; align-items: center; margin-bottom: 8px;">
            <div style="width: 40px; font-size: 12px; color: #4B5563; font-weight: 500;">{name}</div>
            <div style="flex: 1; height: 8px; background: #F3F4F6; border-radius: 4px; margin: 0 10px; overflow: hidden;">
                <div style="width: {pct}%; height: 100%; background: {color}; border-radius: 4px; transition: width 0.5s ease;"></div>
            </div>
            <div style="width: 24px; font-size: 12px; color: #6B7280; text-align: right; font-weight: 600;">{count}</div>
        </div>
        """
    return bars


def _format_number(val) -> str:
    """格式化大数字，带千分位。"""
    if val is None or val == "N/A":
        return "N/A"
    try:
        v = float(val)
        if abs(v) >= 1e8:
            return f"{v/1e8:.2f} 亿"
        elif abs(v) >= 1e4:
            return f"{v/1e4:.2f} 万"
        else:
            return f"{v:,.2f}"
    except (ValueError, TypeError):
        return str(val)


# ============================================================
# 页面配置
# ============================================================

st.set_page_config(
    page_title="财报风险扫描仪",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)
_inject_css()

# ============================================================
# 侧边栏 (深色导航)
# ============================================================

with st.sidebar:
    st.markdown("""
    <div style="padding: 16px 0 24px 0; border-bottom: 1px solid #334155; margin-bottom: 16px;">
        <div style="font-size: 22px; font-weight: 700; color: white;">📊 财报扫描</div>
        <div style="font-size: 12px; color: #94A3B8; margin-top: 4px;">智能财务风险分析</div>
    </div>
    """, unsafe_allow_html=True)

    # 菜单项
    menu_items = [
        ("🏠", "Dashboard", True),
        ("📄", "财报解析", True),
        ("⚠️", "风险报告", False),
        ("📈", "趋势分析", False),
        ("⚙️", "设置", False),
    ]
    for icon, label, active in menu_items:
        bg = "background: #334155; border-radius: 8px;" if active else ""
        color = "white" if active else "#94A3B8"
        st.markdown(f"""
        <div style="padding: 10px 12px; margin-bottom: 4px; cursor: pointer; {bg}">
            <span style="color: {color}; font-size: 14px; font-weight: 500;">{icon} &nbsp; {label}</span>
        </div>
        """, unsafe_allow_html=True)

    st.markdown("""
    <div style="margin-top: 32px; padding-top: 16px; border-top: 1px solid #334155;">
        <div style="font-size: 10px; color: #64748B; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px;">系统信息</div>
    </div>
    """, unsafe_allow_html=True)

    _engine = get_engine_mode()
    engine_label = "🤖 大模型" if _engine == "llm" else "⚙️ 规则引擎"
    st.markdown(f"""
    <div style="padding: 10px 12px; background: #0F172A; border-radius: 8px; margin-bottom: 8px;">
        <div style="font-size: 11px; color: #64748B; margin-bottom: 2px;">当前引擎</div>
        <div style="font-size: 13px; color: #E2E8F0; font-weight: 600;">{engine_label}</div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("""
    <div style="margin-top: auto; padding: 16px; background: #0F172A; border-radius: 12px; margin-top: 24px;">
        <div style="font-size: 12px; color: #94A3B8; line-height: 1.5;">
            💡 <strong style="color: #E2E8F0;">提示</strong><br>
            上传 PDF 财报文件，系统将自动提取关键财务指标并进行风险分析。
        </div>
    </div>
    """, unsafe_allow_html=True)


# ============================================================
# 顶部 Header Bar
# ============================================================

st.markdown("""
<div style="
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 20px 0 24px 0;
    margin-bottom: 8px;
">
    <div>
        <h1 style="margin: 0; font-size: 24px; font-weight: 700; color: #111827;">财报风险扫描仪</h1>
        <p style="margin: 4px 0 0 0; font-size: 13px; color: #6B7280;">上传财报 PDF，AI 自动提取数据并生成风险分析报告</p>
    </div>
    <div style="display: flex; align-items: center; gap: 12px;">
        <div style="
            width: 36px; height: 36px; border-radius: 50%; background: #10B981;
            display: flex; align-items: center; justify-content: center;
            color: white; font-size: 14px; font-weight: 600;
        ">AI</div>
    </div>
</div>
""", unsafe_allow_html=True)


# ============================================================
# Session State 初始化
# ============================================================

if "parse_result" not in st.session_state:
    st.session_state.parse_result = None
if "financial_data" not in st.session_state:
    st.session_state.financial_data = None
if "metrics" not in st.session_state:
    st.session_state.metrics = None
if "judge_result" not in st.session_state:
    st.session_state.judge_result = None
if "data_confirmed" not in st.session_state:
    st.session_state.data_confirmed = False
if "edited_values" not in st.session_state:
    st.session_state.edited_values = None


# ============================================================
# 区域 1: 上传与解析 (Card)
# ============================================================

upload_content = ""
if st.session_state.parse_result is None or not st.session_state.parse_result.success:
    # 未解析或解析失败: 显示上传区
    uploaded_file = st.file_uploader(
        "📁 拖拽或点击上传财报 PDF",
        type=["pdf"],
        help="支持上传 PDF 格式的财报文件，系统将自动提取营收、净利润、资产负债率三项关键数据",
        label_visibility="collapsed",
    )
    if uploaded_file is not None:
        if st.button("🔍 解析 PDF", type="primary", use_container_width=True):
            with st.spinner("正在解析 PDF..."):
                result = parse_pdf(uploaded_file)

            st.session_state.parse_result = result
            st.session_state.data_confirmed = False
            st.session_state.financial_data = None
            st.session_state.metrics = None
            st.session_state.judge_result = None

            if not result.success:
                st.error(f"解析失败: {result.error}")
            else:
                st.success("PDF 解析成功，请在下方确认或修改数据")
                st.rerun()
else:
    # 已解析成功: 显示解析状态 + 重新上传按钮
    pr = st.session_state.parse_result
    st.success(f"✅ PDF 已解析 ({pr.filename if hasattr(pr, 'filename') else '已上传'})")
    if st.button("🔄 重新上传", use_container_width=False):
        st.session_state.parse_result = None
        st.session_state.data_confirmed = False
        st.session_state.financial_data = None
        st.session_state.metrics = None
        st.session_state.judge_result = None
        st.rerun()


# ============================================================
# 区域 2: 数据确认/编辑 (Card) - 解析成功后显示
# ============================================================

if st.session_state.parse_result is not None and st.session_state.parse_result.success:
    pr = st.session_state.parse_result
    ef = pr.extracted_fields

    st.markdown("""
    <div style="margin-top: 16px;"></div>
    """, unsafe_allow_html=True)

    with st.expander("📋 提取数据确认（点击展开）"):
        st.caption("PDF 提取结果不可完全信任，请核对以下数据。修改后点击「确认数据」以继续。")

        col_rev, col_prof, col_debt = st.columns(3)

        with col_rev:
            revenue_input = st.number_input(
                "营收 (元)",
                value=float(ef.get("revenue", 0.0)),
                step=1e6,
                format="%.2f",
                help=f"PDF 提取值: {ef.get('revenue_raw', '')} (单位: {ef.get('revenue_unit', '')})",
            )

        with col_prof:
            net_profit_input = st.number_input(
                "净利润 (元)",
                value=float(ef.get("net_profit", 0.0)),
                step=1e6,
                format="%.2f",
                help=f"PDF 提取值: {ef.get('net_profit_raw', '')} (单位: {ef.get('net_profit_unit', '')})",
            )

        with col_debt:
            debt_ratio_input = st.number_input(
                "资产负债率",
                value=float(ef.get("debt_ratio", 0.0)),
                step=0.01,
                format="%.4f",
                help=f"PDF 提取值: {ef.get('debt_ratio_raw', '')}",
            )

        st.session_state.edited_values = {
            "revenue": revenue_input,
            "net_profit": net_profit_input,
            "debt_ratio": debt_ratio_input,
        }

        # 确认按钮
        col1, col2, col3 = st.columns([1, 1, 1])
        with col2:
            confirm_clicked = st.button("✅ 确认数据", type="primary", use_container_width=True)

        if confirm_clicked:
            ev = st.session_state.edited_values
            fd = FinancialData(
                revenue=ev["revenue"],
                net_profit=ev["net_profit"],
                debt_ratio=ev["debt_ratio"],
                period=None,
            )
            st.session_state.financial_data = [fd]
            st.session_state.metrics = calculate_derived([fd])
            st.session_state.data_confirmed = True
            st.session_state.judge_result = None
            st.success("数据已确认，可点击「风险分析」按钮进行分析")
            st.rerun()

    # PDF 原始信息折叠区
    with st.expander("🔍 PDF 原始提取详情"):
        for key, val in ef.items():
            st.text(f"  {key}: {val}")
        st.text("PDF 原始文本（前 2000 字）:")
        st.text(pr.raw_text[:2000])


# ============================================================
# 区域 3: Metric 卡片行 (数据确认后显示)
# ============================================================

if st.session_state.financial_data is not None:
    fd = st.session_state.financial_data[-1]
    metrics = st.session_state.metrics

    # 计算指标值和颜色
    rev_val = _format_number(fd.revenue)
    prof_val = _format_number(fd.net_profit)
    debt_val = f"{fd.debt_ratio:.2%}" if fd.debt_ratio is not None else "N/A"

    rev_color = "#111827"
    prof_color = "#DC2626" if fd.net_profit is not None and fd.net_profit < 0 else "#10B981"
    debt_color = "#DC2626" if fd.debt_ratio is not None and fd.debt_ratio > 0.7 else "#111827"

    margin_val = "N/A"
    margin_color = "#111827"
    if metrics and metrics.net_margin is not None:
        margin_val = f"{metrics.net_margin:.2%}"
        if metrics.net_margin < 0:
            margin_color = "#DC2626"
        elif metrics.net_margin < 0.02:
            margin_color = "#F59E0B"
        elif metrics.net_margin < 0.05:
            margin_color = "#6B7280"
        else:
            margin_color = "#10B981"

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(_metric_card("营业收入", rev_val, color=rev_color), unsafe_allow_html=True)
    with c2:
        st.markdown(_metric_card("净利润", prof_val, color=prof_color), unsafe_allow_html=True)
    with c3:
        st.markdown(_metric_card("资产负债率", debt_val, color=debt_color), unsafe_allow_html=True)
    with c4:
        st.markdown(_metric_card("净利率", margin_val, color=margin_color), unsafe_allow_html=True)


# ============================================================
# 区域 4: 分析按钮
# ============================================================

if st.session_state.financial_data is not None:
    analyze_disabled = not st.session_state.data_confirmed

    col_btn1, col_btn2, col_btn3 = st.columns([1, 1, 1])
    with col_btn2:
        if st.button("🚀 运行风险分析", disabled=analyze_disabled, type="primary", use_container_width=True):
            if st.session_state.financial_data is not None and st.session_state.metrics is not None:
                with st.spinner("正在进行风险分析..."):
                    result = judge(
                        st.session_state.financial_data,
                        st.session_state.metrics,
                    )

                st.session_state.judge_result = result

                if not result.success:
                    st.error(f"风险分析失败: {result.error}")
                else:
                    st.success(f"分析完成 (引擎: {result.engine})")
                    st.rerun()


# ============================================================
# 区域 5: 风险仪表盘 + 数据表格 (两栏)
# ============================================================

if st.session_state.financial_data is not None:
    st.markdown("<div style='margin-top: 8px;'></div>", unsafe_allow_html=True)

    col_left, col_right = st.columns([1, 1.3])

    # ---- 左栏: 财务数据表格 ----
    with col_left:
        _card("📋 财务数据明细", "", icon="")

        table_data = format_financial_table(
            st.session_state.financial_data,
            st.session_state.metrics if st.session_state.metrics is not None else DerivedMetrics(),
        )
        df = pd.DataFrame(table_data)
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "期次": st.column_config.TextColumn("期次", width="small"),
                "营收(元)": st.column_config.NumberColumn("营收", format="%.2f"),
                "净利润(元)": st.column_config.NumberColumn("净利润", format="%.2f"),
                "资产负债率": st.column_config.NumberColumn("负债率", format="%.4f"),
                "净利率": st.column_config.NumberColumn("净利率", format="%.4f"),
            },
        )

    # ---- 右栏: 风险仪表盘 ----
    with col_right:
        if st.session_state.judge_result is None:
            _card("⚠️ 风险仪表盘", """
            <div style="text-align: center; padding: 40px 0; color: #9CA3AF;">
                <div style="font-size: 48px; margin-bottom: 12px;">🔍</div>
                <div style="font-size: 14px;">点击「运行风险分析」生成报告</div>
            </div>
            """, icon="")
        elif not st.session_state.judge_result.success:
            _card("❌ 分析失败", f"""
            <div style="text-align: center; padding: 20px; color: #DC2626;">
                <div style="font-size: 14px;">{st.session_state.judge_result.error}</div>
            </div>
            """, icon="")
        else:
            jr = st.session_state.judge_result
            overall = jr.summary.get("overall_risk_level", "未知")
            dist = jr.summary.get("risk_distribution", {})
            total = jr.summary.get("total_risks", 0)

            # 计算风险评分百分比 (用于环形图)
            level_score = {"严重": 100, "高": 75, "中": 50, "低": 25, "无显著风险": 0}
            score_pct = level_score.get(overall, 50)

            # 仪表盘内容
            dashboard_html = f"""
            <div style="display: flex; gap: 24px; align-items: flex-start;">
                <div style="flex-shrink: 0;">
                    {_risk_score_ring(overall, score_pct)}
                </div>
                <div style="flex: 1; min-width: 0;">
                    <div style="font-size: 12px; color: #6B7280; margin-bottom: 8px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;">风险分布</div>
                    {_risk_distribution_bars(dist)}
                    <div style="margin-top: 12px; padding-top: 12px; border-top: 1px solid #F3F4F6;">
                        <div style="display: flex; justify-content: space-between; font-size: 12px; color: #6B7280;">
                            <span>风险点总数</span>
                            <span style="font-weight: 700; color: #111827;">{total}</span>
                        </div>
                        <div style="display: flex; justify-content: space-between; font-size: 12px; color: #6B7280; margin-top: 4px;">
                            <span>判定引擎</span>
                            <span style="font-weight: 600; color: #111827;">{jr.engine}</span>
                        </div>
                    </div>
                </div>
            </div>
            """
            _card("⚠️ 风险仪表盘", dashboard_html, icon="")


# ============================================================
# 区域 6: 风险详情卡片列表
# ============================================================

if st.session_state.judge_result is not None and st.session_state.judge_result.success:
    jr = st.session_state.judge_result

    if jr.risk_points:
        st.markdown("""
        <div style="margin-top: 8px; margin-bottom: 16px;">
            <h2 style="font-size: 18px; font-weight: 700; color: #111827; margin: 0;">🚨 风险详情</h2>
        </div>
        """, unsafe_allow_html=True)

        for rp in jr.risk_points:
            level = rp.get("risk_level", "")
            category = rp.get("risk_category", "")
            rule = rp.get("rule_matched", "")
            desc = rp.get("description", "")
            conf = rp.get("confidence", "")
            rec = rp.get("recommendation", "")
            scope = rp.get("scope", "")
            period = rp.get("period", "")

            # 边框颜色
            border_colors = {
                "严重": "#DC2626",
                "高": "#EF4444",
                "中": "#F59E0B",
                "低": "#6B7280",
            }
            border_color = border_colors.get(level, "#6B7280")

            # 证据数据
            evidence = rp.get("data_evidence", {})
            evidence_str = ""
            if evidence:
                field = evidence.get("field", "")
                val = evidence.get("value", "")
                thresh = evidence.get("threshold", "")
                evidence_str = f"<span style='color: #6B7280;'>字段</span> <code style='background: #F3F4F6; padding: 2px 6px; border-radius: 4px; font-size: 12px;'>{field}</code> &nbsp; <span style='color: #6B7280;'>值</span> <code style='background: #F3F4F6; padding: 2px 6px; border-radius: 4px; font-size: 12px;'>{val}</code> &nbsp; <span style='color: #6B7280;'>阈值</span> <code style='background: #F3F4F6; padding: 2px 6px; border-radius: 4px; font-size: 12px;'>{thresh}</code>"

            card_html = f"""
            <div style="
                background: white;
                border-radius: 12px;
                padding: 20px;
                margin-bottom: 12px;
                box-shadow: 0 1px 3px rgba(0,0,0,0.06);
                border-left: 4px solid {border_color};
                border-top: 1px solid #E5E7EB;
                border-right: 1px solid #E5E7EB;
                border-bottom: 1px solid #E5E7EB;
            ">
                <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px;">
                    <div>
                        <div style="font-size: 14px; font-weight: 600; color: #111827; margin-bottom: 4px;">{category}</div>
                        <div style="font-size: 12px; color: #6B7280;">{rule}</div>
                    </div>
                    <div>{_risk_badge(level)}</div>
                </div>
                <div style="font-size: 13px; color: #374151; line-height: 1.6; margin-bottom: 10px;">
                    {desc}
                </div>
                {f'<div style="font-size: 12px; margin-bottom: 8px;">{evidence_str}</div>' if evidence_str else ''}
                <div style="display: flex; gap: 16px; font-size: 11px; color: #9CA3AF; margin-top: 8px; padding-top: 8px; border-top: 1px solid #F3F4F6;">
                    <span>💡 {rec}</span>
                    <span>🔍 置信度: {conf}</span>
                    <span>📍 作用域: {scope}</span>
                    {f'<span>📅 期次: {period}</span>' if period else ''}
                </div>
            </div>
            """
            st.markdown(card_html, unsafe_allow_html=True)
    else:
        st.markdown("""
        <div style="background: white; border-radius: 16px; padding: 40px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.08); border: 1px solid #E5E7EB;">
            <div style="font-size: 48px; margin-bottom: 12px;">🎉</div>
            <div style="font-size: 16px; font-weight: 600; color: #10B981; margin-bottom: 8px;">未检测到显著风险</div>
            <div style="font-size: 13px; color: #6B7280;">当前财务指标处于正常范围内</div>
        </div>
        """, unsafe_allow_html=True)

    # 免责声明
    st.markdown(f"""
    <div style="margin-top: 16px; padding: 12px 16px; background: #F9FAFB; border-radius: 8px; font-size: 11px; color: #9CA3AF; line-height: 1.5; border: 1px solid #E5E7EB;">
        ⚠️ {jr.disclaimer}
    </div>
    """, unsafe_allow_html=True)


# ============================================================
# 调试区（可折叠）
# ============================================================

if st.session_state.judge_result and st.session_state.judge_result.success:
    with st.expander("🔧 调试信息"):
        st.text(f"判定引擎: {st.session_state.judge_result.engine}")
        st.text("输入 JSON:")
        st.code(st.session_state.judge_result.input_json, language="json")
        if st.session_state.judge_result.raw_response:
            st.text("大模型原始返回:")
            st.code(st.session_state.judge_result.raw_response, language="json")
        else:
            st.caption("(规则引擎模式无原始返回)")
