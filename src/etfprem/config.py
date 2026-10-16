"""路径、基金参数与本地环境变量。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(os.environ.get("ETFPREM_ROOT", Path(__file__).resolve().parents[2]))
DATA = ROOT / "data"
REPORTS = ROOT / "reports"

LOCAL_MARKETS = ("sz", "sh")


@dataclass(frozen=True)
class Asset:
    """定期报告里出现的持仓（目前都是 ETF）。"""

    code: str  # 行情代码：美股代码、A 股六位代码或东京四位代码
    name: str
    market: str  # us / sz / sh / jp
    currency: str  # USD / CNY / JPY
    keywords: tuple[str, ...] = ()  # 报告表格中的名称关键词（去掉空格后匹配）


@dataclass(frozen=True)
class FundConfig:
    fund_id: str
    name: str
    market: str  # 场内市场：sz / sh
    holdings: str  # 持仓来源：pcf（每日申赎清单）/ reports（定期报告）
    index_id: str | None  # 跟踪指数（Nasdaq 代码），用于对照；没有则为 None
    fx_pairs: tuple[str, ...]  # 外汇交易中心中间价货币对，第一个为主币种
    annual_fee: float  # 管理费 + 托管费，年化
    dividend_wht: float  # 分红预提所得税率
    first_date: str  # 默认最早抓取日期
    assets: tuple[Asset, ...] = ()

    @property
    def fx_pair(self) -> str:
        return self.fx_pairs[0]


def pair_currency(pair: str) -> tuple[str, float]:
    """'100JPY/CNY' -> ('JPY', 100)。"""
    m = re.fullmatch(r"(\d*)([A-Z]{3})/CNY", pair)
    if not m:
        raise ValueError(f"不支持的货币对：{pair}")
    return m.group(2), float(m.group(1) or 1)


FUNDS: dict[str, FundConfig] = {
    "159509": FundConfig(
        fund_id="159509",
        name="纳指科技ETF景顺",
        market="sz",
        holdings="pcf",
        index_id="NDXTMC",
        fx_pairs=("USD/CNY",),
        annual_fee=0.0100,
        dividend_wht=0.10,
        first_date="2025-09-01",
    ),
    "501225": FundConfig(
        fund_id="501225",
        name="全球芯片LOF",
        market="sh",
        holdings="reports",
        index_id=None,
        fx_pairs=("USD/CNY", "100JPY/CNY"),
        annual_fee=0.0140,
        dividend_wht=0.10,
        first_date="2025-06-20",
        assets=(
            Asset("PSI", "Invesco Dynamic Semiconductors ETF", "us", "USD", ("Dynamic",)),
            Asset("SOXX", "iShares Semiconductor ETF", "us", "USD", ("iShares",)),
            Asset("SMH", "VanEck Semiconductor ETF", "us", "USD", ("VanEck",)),
            Asset("SOXQ", "Invesco PHLX Semiconductor ETF", "us", "USD", ("PHLX",)),
            Asset("159995", "华夏国证半导体芯片ETF", "sz", "CNY", ("华夏国证",)),
            Asset("512760", "国泰CES半导体芯片行业ETF", "sh", "CNY", ("国泰CES",)),
            Asset("159560", "景顺长城中证芯片产业ETF", "sz", "CNY", ("景顺长城中证芯",)),
            Asset("2644", "Global X Semiconductor ETF（东京）", "jp", "JPY", ("GlobalX",)),
        ),
    ),
}


def get_fund(fund_id: str) -> FundConfig:
    try:
        return FUNDS[fund_id]
    except KeyError:
        raise SystemExit(f"未配置的基金：{fund_id}，已配置：{', '.join(FUNDS)}") from None


def load_env(path: Path | None = None) -> dict[str, str]:
    """读取项目根目录 .env，环境变量优先。"""
    env: dict[str, str] = {}
    path = path or ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k.startswith(("JISILU_", "ETFPREM_"))})
    return env
