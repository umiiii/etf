"""路径、基金参数与本地环境变量。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
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
class Series:
    """一条日线：跟踪指数，或联接基金持有的境外 ETF。缓存在 data/index/{key}.json。"""

    key: str  # 缓存文件名，例如 NDX、SPX、HK02830
    source: str  # nasdaq / nasdaq_etf / tencent / eastmoney / csindex / euronext / csop / lionglobal / msci / b3
    symbol: str  # 数据源里的代码：NDX、us.INX、100.GDAXI、930604、hk02830、LCU
    currency: str  # 计价货币：USD / EUR / HKD / BRL / CNY
    name: str
    published: Series | None = None  # 数值要到这个市场的下一个交易日才公布（例如香港 ETF 的净值），用它的交易日对齐
    published_rule: str = "all"  # all：所有休市都顺延；western：只在休市含圣诞、节礼日、耶稣受难日、复活节星期一时顺延


@dataclass(frozen=True)
class FundConfig:
    fund_id: str
    name: str
    market: str  # 场内市场：sz / sh
    holdings: str  # 持仓来源：pcf（每日申赎清单）/ reports（定期报告）/ index（跟踪指数或被投资 ETF 的日线）
    index_id: str | None  # 跟踪指数（Nasdaq 代码），用于对照；没有则为 None
    fx_pairs: tuple[str, ...]  # 外汇交易中心中间价货币对，第一个为主币种
    annual_fee: float  # 管理费 + 托管费，年化
    dividend_wht: float  # 分红预提所得税率
    first_date: str  # 默认最早抓取日期
    assets: tuple[Asset, ...] = ()
    series: Series | None = None  # 指数法用的日线；pcf 基金用作对照，index 基金是唯一的估值依据
    pcf_fund: str | None = None  # 借用另一只深市同指数 ETF 的申赎清单（沪市 ETF 没有历史清单）

    @property
    def fx_pair(self) -> str:
        return self.fx_pairs[0]

    @property
    def index_series(self) -> Series | None:
        if self.series:
            return self.series
        if self.index_id:
            return Series(self.index_id, "nasdaq", self.index_id, "USD", self.index_id)
        return None

    @property
    def pcf_id(self) -> str:
        """申赎清单来自哪只基金。"""
        return self.pcf_fund or self.fund_id


def pair_currency(pair: str) -> tuple[str, float]:
    """'100JPY/CNY' -> ('JPY', 100)。'@' 之后是取价口径：USD/CNY@16 为 16:00 参考汇率，BRL/CNY@BJ22 见 store.update_brl。"""
    m = re.fullmatch(r"(\d*)([A-Z]{3})/CNY(@\w+)?", pair)
    if not m:
        raise ValueError(f"不支持的货币对：{pair}")
    return m.group(2), float(m.group(1) or 1)


CCY_PAIR = {"USD": "USD/CNY", "EUR": "EUR/CNY", "HKD": "HKD/CNY", "SGD": "SGD/CNY", "JPY": "100JPY/CNY", "SAR": "SAR/CNY"}

NDXTMC = Series("NDXTMC", "nasdaq", "NDXTMC", "USD", "纳斯达克科技市值加权指数")
NDX = Series("NDX", "nasdaq", "NDX", "USD", "纳斯达克100指数")
NBI = Series("NBI", "nasdaq", "NBI", "USD", "纳斯达克生物科技指数")
SPX = Series("SPX", "tencent", "us.INX", "USD", "标普500指数")
DJI = Series("DJI", "tencent", "us.DJI", "USD", "道琼斯工业平均指数")
DAX = Series("GDAXI", "eastmoney", "100.GDAXI", "EUR", "德国DAX指数")
CAC = Series("CAC40NR", "euronext", "QS0011131826-XPAR", "EUR", "法国CAC40净收益指数")
XOP = Series("XOP", "nasdaq_etf", "XOP", "USD", "SPDR标普油气开采与生产ETF（XOP，跟踪同一指数）")
XBI = Series("XBI", "nasdaq_etf", "XBI", "USD", "SPDR标普生物科技ETF（XBI，跟踪同一指数）")
MSCI_USA50 = Series("MSCI750108_NETR", "msci", "750108:NETR", "USD", "MSCI美国50净收益指数")
B3_BOVB11 = Series("B3_BOVB11", "b3", "BOVB11", "BRL", "布拉德斯科Ibovespa ETF（BOVB11，B3）")
B3_BOVV11 = Series("B3_BOVV11", "b3", "BOVV11", "BRL", "伊塔乌Ibovespa ETF（BOVV11，B3）")
CSI_930604 = Series("CSI930604_HKD_TR", "csindex", "930604-HKD-TR", "HKD", "中证海外中国互联网30全收益指数（港元，由美元版换算）")
CSI_H30533 = Series("CSIH20533", "csindex", "H20533", "CNY", "中证海外中国互联网50全收益指数（人民币）")
CSI_930796 = Series("CSI930796_HKD_TR_A", "csindex", "930796-HKD-TR-A", "HKD", "中证全球中国互联网全收益指数（港元，扣除 A 股汇率差）")
CSI_931456 = Series("CSI931456_HKD_TR_A", "csindex", "931456-HKD-TR-A", "HKD", "中证全球中国教育主题全收益指数（港元，扣除 A 股汇率差）")
HK_3167 = Series("CSOP3167_HKD", "csop", "3167 HK", "HKD", "工银南方东英标普中国新经济行业ETF（3167.HK）的港元净值")
HSI = Series("HSI", "tencent", "hkHSI", "HKD", "恒生指数（港股交易日历）")
CSOP_2830 = Series("CSOP2830_SAR", "csop", "72830 HK", "SAR", "南方东英沙特阿拉伯ETF（2830.HK）的里亚尔净值", published=HSI)
# 华泰柏瑞在重阳、七一这类单日港股假期当天就用上了 2830 的新净值，只在圣诞、复活节长假时顺延
CSOP_2830_W = replace(CSOP_2830, published_rule="western")
SG_LCU = Series("CSOP_LCU", "csop", "LCU SP", "USD", "南方东英富时亚太精选ETF（LCU.SI）的美元净值")
SG_SQU = Series("CSOP_SQU", "csop", "SQU SP", "USD", "南方东英iEdge东南亚科技ETF（SQU.SI）的美元净值")
SG_EAU = Series("LGI_EAU", "lionglobal", "EAU", "USD", "利安招商新兴亚洲精选ETF（EAU.SI）的美元净值")


def _pcf(fund_id: str, name: str, fee: float, series: Series | None = None) -> FundConfig:
    """深交所 ETF：每日申赎清单里的美股成分篓子。"""
    return FundConfig(fund_id, name, "sz", "pcf", None, ("USD/CNY",), fee, 0.10, "2025-08-01", series=series)


def _sibling(fund_id: str, name: str, fee: float, pcf_fund: str, series: Series | None = None) -> FundConfig:
    """沪市 ETF：借用跟踪同一指数的深市 ETF 的申赎清单。"""
    return FundConfig(fund_id, name, "sh", "pcf", None, ("USD/CNY",), fee, 0.10, "2025-08-01", series=series,
                      pcf_fund=pcf_fund)


def _index(fund_id: str, name: str, market: str, fee: float, series: Series, wht: float = 0.10,
           fx16: bool = False, fx: str | None = None) -> FundConfig:
    """跟踪指数，或联接基金持有的单只境外 ETF。fx16：用 16:00 参考汇率而不是中间价（华夏基金）；fx：直接指定货币对。"""
    pair = fx or CCY_PAIR.get(series.currency, "USD/CNY")
    return FundConfig(fund_id, name, market, "index", None, (pair + "@16" if fx16 else pair,),
                      fee, wht, "2025-08-01", series=series)


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
        series=NDXTMC,
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
    # 纳斯达克 100：指数有官方日线，指数法比清单篓子更准（159509 的对照也是如此），深沪两市都用指数
    "159659": _index("159659", "纳斯达克100ETF招商", "sz", 0.0065, NDX),
    "159632": _index("159632", "纳斯达克ETF华安", "sz", 0.0080, NDX),
    "159660": _index("159660", "纳指ETF汇添富", "sz", 0.0065, NDX),
    "159941": _index("159941", "纳指ETF广发", "sz", 0.0100, NDX),
    "159501": _index("159501", "纳指ETF嘉实", "sz", 0.0060, NDX),
    "159696": _index("159696", "纳指ETF易方达", "sz", 0.0060, NDX),
    "159513": _index("159513", "纳斯达克100ETF大成", "sz", 0.0100, NDX),
    "513300": _index("513300", "纳斯达克ETF华夏", "sh", 0.0080, NDX, fx16=True),
    "513870": _index("513870", "纳指ETF富国", "sh", 0.0060, NDX),
    "513100": _index("513100", "纳指ETF国泰", "sh", 0.0080, NDX),
    "513110": _index("513110", "纳指ETF华泰柏瑞", "sh", 0.0100, NDX),
    "513390": _index("513390", "纳指100ETF博时", "sh", 0.0065, NDX),
    # 标普 500
    "159612": _index("159612", "标普500ETF国泰", "sz", 0.0075, SPX),
    "159655": _index("159655", "标普500ETF华夏", "sz", 0.0075, SPX, fx16=True),
    "513650": _index("513650", "标普500ETF南方", "sh", 0.0075, SPX),
    "513500": _index("513500", "标普500ETF博时", "sh", 0.0080, SPX),
    # 其他美股指数。标普行业指数没有免费日线，用深市 ETF 的申赎清单篓子，沪市同指数 ETF 借用；
    # 跟踪同一指数的美国 ETF（XOP、XBI）作对照，误差是清单篓子的四倍左右
    "513400": _index("513400", "道琼斯ETF鹏华", "sh", 0.0065, DJI),
    "513290": _index("513290", "纳指生物科技ETF汇添富", "sh", 0.0065, NBI),
    "159577": _index("159577", "美国50ETF汇添富", "sz", 0.0065, MSCI_USA50),
    "513850": _index("513850", "美国50ETF易方达", "sh", 0.0060, MSCI_USA50),
    "159518": _pcf("159518", "标普油气ETF嘉实", 0.0060, XOP),
    "513350": _sibling("513350", "标普油气ETF富国", 0.0060, "159518", XOP),
    "159529": _pcf("159529", "标普消费ETF景顺", 0.0070),
    "159502": _index("159502", "标普生物科技ETF嘉实", "sz", 0.0060, XBI),
    # 欧洲
    "159561": _index("159561", "德国ETF嘉实", "sz", 0.0060, DAX),
    "513030": _index("513030", "德国ETF华安", "sh", 0.0100, DAX),
    "513080": _index("513080", "法国ETF华安", "sh", 0.0065, CAC),
    # 中概互联网与教育
    "159605": _index("159605", "中概互联ETF广发", "sz", 0.0060, CSI_930604),
    "159607": _index("159607", "中概互联网ETF嘉实", "sz", 0.0060, CSI_930604),
    "513050": _index("513050", "中概互联网ETF易方达", "sh", 0.0080, CSI_H30533),
    "513220": _index("513220", "中概互联ETF招商", "sh", 0.0065, CSI_930796),
    "513360": _index("513360", "教育ETF博时", "sh", 0.0065, CSI_931456),
    # 联接境外 ETF：持仓只有一只在香港、新加坡或巴西上市的 ETF
    "159822": _index("159822", "新经济ETF银华", "sz", 0.0035, HK_3167),
    "159329": _index("159329", "沙特ETF南方", "sz", 0.0060, CSOP_2830),
    "520830": _index("520830", "沙特ETF华泰柏瑞", "sh", 0.0060, CSOP_2830_W),
    "159687": _index("159687", "亚太精选ETF南方", "sz", 0.0025, SG_LCU),
    "513730": _index("513730", "东南亚科技ETF华泰柏瑞", "sh", 0.0050, SG_SQU),
    "520580": _index("520580", "新兴亚洲ETF招商", "sh", 0.0050, SG_EAU),
    "159100": _index("159100", "巴西ETF华夏", "sz", 0.0060, B3_BOVB11, fx="BRL/CNY@BJ22"),
    "520870": _index("520870", "巴西ETF易方达", "sh", 0.0060, B3_BOVV11, fx="BRL/CNY@LDN16"),
}


def get_fund(fund_id: str) -> FundConfig:
    try:
        return FUNDS[fund_id]
    except KeyError:
        raise SystemExit(f"未配置的基金：{fund_id}，已配置：{', '.join(FUNDS)}") from None


def select_funds(spec: str) -> list[FundConfig]:
    """'159509'、'159509,513100' 或 'all'。"""
    if spec.strip().lower() == "all":
        return list(FUNDS.values())
    return [get_fund(s.strip()) for s in spec.split(",") if s.strip()]


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
