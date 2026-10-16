"""本地缓存：update() 把各数据源写进 data/，load() 只读缓存组装成 MarketData。

时间序列一律按日期合并写入（{"series": {日期: 值}}），短窗口刷新不会截断历史。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from .calendar import Calendar
from .config import DATA, LOCAL_MARKETS, FundConfig, pair_currency
from .net import FetchError, NotFound
from .sources import cfets, eastmoney, fund_reports, jisilu, kabutan, nasdaq, sina, szse_pcf, tencent

Log = Callable[[str], None]

PRICE_DIRS = {"us": "us_prices", "sz": "cn_prices", "sh": "cn_prices", "jp": "jp_prices"}


def _pcf_dir(fund: FundConfig) -> Path:
    return DATA / "pcf" / fund.fund_id


def _reports_dir(fund: FundConfig) -> Path:
    return DATA / "fund_reports" / fund.fund_id


def _fx_path(pair: str) -> Path:
    return DATA / "fx" / f"{pair.replace('/', '_')}.json"


def _jisilu_path(fund: FundConfig) -> Path:
    return DATA / f"qdii_{fund.fund_id}_hist.json"


def _price_path(code: str, market: str) -> Path:
    return DATA / PRICE_DIRS[market] / f"{code}.json"


def nasdaq_symbol(code: str) -> str:
    return code.replace("/", ".").replace(" ", ".")


def _read_series(path: Path, raw_parser: Callable[[bytes], dict] | None = None) -> dict:
    """读取缓存的时间序列。新格式为 {"series": {日期: 值}}，旧格式（原始接口响应）交给 raw_parser。"""
    if not path.exists():
        return {}
    raw = path.read_bytes()
    try:
        j = json.loads(raw)
    except ValueError:
        j = None
    if isinstance(j, dict) and "series" in j:
        return {date.fromisoformat(k): v for k, v in j["series"].items()}
    return raw_parser(raw) if raw_parser else {}


def _merge_series(path: Path, new: dict, raw_parser: Callable[[bytes], dict] | None = None, **meta) -> dict:
    """把新数据按日期合并进缓存，避免短窗口刷新截断历史。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = _read_series(path, raw_parser)
    merged.update(new)
    payload = {**meta, "updated": date.today().isoformat(),
               "series": {k.isoformat(): v for k, v in sorted(merged.items())}}
    path.write_text(json.dumps(payload, ensure_ascii=False))
    return merged


def _merge_prices(path: Path, new: dict, raw_parser: Callable[[bytes], dict] | None = None, **meta) -> dict:
    """价格序列合并。复权价格在新发生拆股后会整体改写历史，此时把旧缓存按重叠日期的比例换算到新基准。"""
    old = _read_series(path, raw_parser)
    overlap = sorted(new[d] / old[d] for d in new if old.get(d))
    if overlap:
        ratio = overlap[len(overlap) // 2]
        if abs(ratio - 1) > 0.005:
            old = {d: v * ratio for d, v in old.items()}
    old.update(new)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**meta, "updated": date.today().isoformat(),
               "series": {k.isoformat(): v for k, v in sorted(old.items())}}
    path.write_text(json.dumps(payload, ensure_ascii=False))
    return old


def _nav_rows_parser(raw: bytes) -> dict:
    return eastmoney.parse_nav(json.loads(raw))


def _close_only(bars: dict) -> dict:
    return {d: (b["close"] if isinstance(b, dict) else b) for d, b in bars.items()}


@dataclass
class MarketData:
    fund: FundConfig
    pcf: dict[date, szse_pcf.PCF]  # 每日申赎清单（ETF）
    nav: dict[date, float]  # 官方净值，PCF 基金为高精度值
    nav4: dict[date, float]  # 公布的 4 位小数净值
    etf: dict[date, dict[str, float]]  # 本基金场内日线
    fx: dict[date, float]  # 主币种中间价（每 1 单位外币兑人民币）
    px: dict[str, dict[date, float]]  # 持仓资产收盘价（本币）
    divs: dict[str, dict[date, float]]  # 持仓资产现金分红（本币，按除息日）
    index: dict[date, float]  # 跟踪指数（可选）
    jisilu: dict[date, dict]
    fx_all: dict[str, dict[date, float]] = field(default_factory=dict)  # 币种 -> 中间价
    markets: dict[str, str] = field(default_factory=dict)  # 资产代码 -> 市场
    reports: list[fund_reports.FundReport] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def calendar(self) -> Calendar:
        cn = set(self.pcf) | set(self.nav) | set(self.etf)
        us = {d for c, s in self.px.items() if self.markets.get(c, "us") == "us" for d in s}
        return Calendar(cn, us)


# ---------------------------------------------------------------- 下载

def update_pcfs(fund: FundConfig, start: date, end: date, log: Log, pause: float = 0.15) -> int:
    folder = _pcf_dir(fund)
    folder.mkdir(parents=True, exist_ok=True)
    missing_file = folder / "_missing.json"
    missing = set(json.loads(missing_file.read_text())) if missing_file.exists() else set()
    today = date.today()
    new = 0
    d = start
    while d <= end:
        ymd = d.strftime("%Y%m%d")
        path = folder / f"{ymd}.txt"
        if d.weekday() < 5 and not path.exists() and ymd not in missing:
            try:
                path.write_bytes(szse_pcf.download(fund.fund_id, d))
                new += 1
            except NotFound:
                if d < today:
                    missing.add(ymd)
            time.sleep(pause)
        d += timedelta(days=1)
    missing_file.write_text(json.dumps(sorted(missing)))
    log(f"PCF：新增 {new} 份，目录 {folder}")
    return new


def update_reports(fund: FundConfig, since: date, log: Log) -> list[fund_reports.FundReport]:
    """下载 since 之后发布的定期报告 PDF，解析基金投资明细，结果写入 reports.json。"""
    folder = _reports_dir(fund)
    folder.mkdir(parents=True, exist_ok=True)
    cache = {r.id: r for r in load_reports(fund)}
    for item in fund_reports.list_reports(fund.fund_id):
        published = date.fromisoformat(item["publish_date"])
        if published < since or item["id"] in cache:
            continue
        pdf = folder / f"{item['id']}.pdf"
        if not pdf.exists():
            pdf.write_bytes(fund_reports.download_pdf(item["id"]))
            time.sleep(0.3)
        pct, fv, unknown = fund_reports.parse_fund_holdings(fund_reports.pdf_text(pdf), fund.assets)
        rep = fund_reports.FundReport(item["id"], item["title"], published,
                                      fund_reports.period_end(item["title"]), pct, fv, unknown)
        if unknown:
            log(f"  {rep.title}：有 {len(unknown)} 行持仓没能对应到已知资产：{unknown}")
        cache[rep.id] = rep
    reports = sorted(cache.values(), key=lambda r: (r.publish_date, r.id))
    (folder / "reports.json").write_text(
        json.dumps([r.to_json() for r in reports], ensure_ascii=False, indent=1), encoding="utf-8"
    )
    log(f"定期报告：{len(reports)} 份，其中 {sum(1 for r in reports if r.holdings)} 份含基金投资明细")
    return reports


def _update_one_us(sym: str, start: date, end: date, assetclass: str, pause: float) -> None:
    ns = nasdaq_symbol(sym)
    series = nasdaq.parse_stock(nasdaq.download_stock(ns, start, end, assetclass))
    if not series:
        raise FetchError("无数据")
    _merge_prices(_price_path(sym, "us"), series, nasdaq.parse_stock, symbol=ns, source="nasdaq")
    div_path = DATA / "us_divs" / f"{sym}.json"
    if not div_path.exists() or time.time() - div_path.stat().st_mtime > 3 * 86400:
        time.sleep(pause)
        div_path.write_bytes(nasdaq.download_dividends(ns, assetclass))
    time.sleep(pause)


def update_us(symbols: list[str], start: date, end: date, log: Log, assetclass: str = "stocks",
              pause: float = 0.25, workers: int = 4) -> list[str]:
    """Nasdaq 日线与分红，4 个线程并发。每个代码写各自的文件，互不冲突。"""
    from concurrent.futures import ThreadPoolExecutor

    failed = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {sym: pool.submit(_update_one_us, sym, start, end, assetclass, pause) for sym in symbols}
        for sym, fut in futures.items():
            try:
                fut.result()
            except (FetchError, ValueError) as e:
                failed.append(sym)
                log(f"  {sym} 下载失败：{e}")
    log(f"美股日线与分红：{len(symbols) - len(failed)}/{len(symbols)} 只")
    return failed


def update_cn(codes: list[tuple[str, str]], start: date, end: date, log: Log) -> None:
    """场内 ETF 用腾讯后复权收盘价，避免份额拆分日的价格跳变。"""
    for code, market in codes:
        series = tencent.download_kline(code, market, start, end, "hfq")
        if not series:
            raise FetchError(f"{code} 没有取到复权行情")
        merged = _merge_prices(_price_path(code, market), series, source="tencent-hfq")
        log(f"A 股 {code}：{len(merged)} 天（后复权）")
        time.sleep(0.3)


def update_jp(codes: list[str], since: date, log: Log) -> None:
    for code in codes:
        merged = _merge_prices(_price_path(code, "jp"), kabutan.download_daily(code, since), source="kabutan")
        log(f"东京 {code}：{len(merged)} 天")


def update_fx(pair: str, start: date, end: date, log: Log) -> None:
    path = _fx_path(pair)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {date.fromisoformat(k): v for k, v in json.loads(path.read_text()).items()} if path.exists() else {}
    # 外汇交易中心单次查询跨度不能超过一年，cfets.download 内部分段；本地缓存与新数据合并
    merged.update(cfets.download(pair, start, end))
    path.write_text(json.dumps({k.isoformat(): v for k, v in sorted(merged.items())}, indent=0))
    log(f"中间价 {pair}：{len(merged)} 天")


def update(fund: FundConfig, start: date, end: date, *, log: Log = print, jisilu_hist: bool = True) -> None:
    """下载 [start, end] 需要的数据。时间序列多取 120 天，供敞口回归使用。"""
    for sub in ("us_prices", "us_divs", "fx", "nav", "etf", "index"):
        (DATA / sub).mkdir(parents=True, exist_ok=True)
    pad = start - timedelta(days=120)

    if fund.holdings == "pcf":
        update_pcfs(fund, start, end + timedelta(days=1), log)
        pcfs = load_pcfs(fund)
        symbols = sorted({c for p in pcfs.values() if start <= p.trade_date for c, h in p.holdings.items()
                          if h.market not in ("深圳市场", "上海市场")})
        update_us(symbols, pad, end, log)
    else:
        # 报告按发布日生效，最早要覆盖起始日当时正在用的那份
        update_reports(fund, pad - timedelta(days=120), log)
        update_us([a.code for a in fund.assets if a.market == "us"], pad, end, log, assetclass="etf")
        update_cn([(a.code, a.market) for a in fund.assets if a.market in LOCAL_MARKETS], pad, end, log)
        update_jp([a.code for a in fund.assets if a.market == "jp"], pad, log)

    for pair in fund.fx_pairs:
        update_fx(pair, pad, end, log)

    bars = sina.parse_kline(sina.download_kline(fund.fund_id, fund.market, n=max(300, (end - start).days)))
    bars = _merge_series(DATA / "etf" / f"{fund.fund_id}_sina.json", bars, sina.parse_kline, source="sina")
    log(f"场内日线：{len(bars)} 天")

    navs = eastmoney.parse_nav(eastmoney.download_nav(fund.fund_id, pad, end))
    navs = _merge_series(DATA / "nav" / f"{fund.fund_id}_eastmoney.json", navs, _nav_rows_parser, source="eastmoney")
    log(f"天天基金净值：{len(navs)} 条")

    if fund.index_id:
        idx_new = nasdaq.parse_index_history(nasdaq.download_index_history(fund.index_id, pad, end))
        idx_json, idx_xlsx = DATA / "index" / f"{fund.index_id}.json", DATA / "index" / f"{fund.index_id}.xlsx"
        base = _read_series(idx_json) or (nasdaq.parse_index_history(idx_xlsx.read_bytes()) if idx_xlsx.exists() else {})
        base.update(idx_new)
        idx_all = _merge_series(idx_json, base, source="nasdaq")
        idx_xlsx.unlink(missing_ok=True)
        log(f"指数 {fund.index_id}：{len(idx_all)} 天")

    if jisilu_hist:
        try:
            merge_jisilu(fund, jisilu.download_hist(fund.fund_id))
            log("集思录历史溢价：已合并")
        except (RuntimeError, FetchError) as e:
            log(f"集思录历史溢价跳过：{e}")


def update_long_history(fund: FundConfig, log: Log = print, us_calendar_since: date = date(2023, 1, 1)) -> None:
    """建模用的长历史：成立以来的净值与场内日线，以及作为美股交易日历的 NDXTMC 指数。"""
    navs = _merge_series(DATA / "nav" / f"{fund.fund_id}_eastmoney.json", eastmoney.download_nav_full(fund.fund_id),
                         _nav_rows_parser, source="eastmoney")
    bars = _merge_series(DATA / "etf" / f"{fund.fund_id}_sina.json",
                         sina.parse_kline(sina.download_kline(fund.fund_id, fund.market, n=1500)),
                         sina.parse_kline, source="sina")
    idx_json = DATA / "index" / "NDXTMC.json"
    idx = nasdaq.parse_index_history(nasdaq.download_index_history("NDXTMC", us_calendar_since, date.today()))
    idx_all = _merge_series(idx_json, idx, source="nasdaq")
    log(f"{fund.fund_id} 长历史：净值 {len(navs)} 天，日线 {len(bars)} 天；美股交易日历 {len(idx_all)} 天")


def merge_jisilu(fund: FundConfig, raw: bytes) -> None:
    path = _jisilu_path(fund)
    rows = {}
    if path.exists():
        rows = {r["id"]: r for r in json.loads(path.read_text()).get("rows", [])}
    for r in json.loads(raw).get("rows", []):
        rows[r["id"]] = r
    ordered = [rows[k] for k in sorted(rows, reverse=True)]
    path.write_text(json.dumps({"page": 1, "rows": ordered}, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- 读取

def load_pcfs(fund: FundConfig) -> dict[date, szse_pcf.PCF]:
    out = {}
    for path in sorted(_pcf_dir(fund).glob("*.txt")):
        pcf = szse_pcf.parse(path.read_bytes(), fund.fund_id)
        out[pcf.trade_date] = pcf
    return out


def load_reports(fund: FundConfig) -> list[fund_reports.FundReport]:
    path = _reports_dir(fund) / "reports.json"
    if not path.exists():
        return []
    return [fund_reports.FundReport.from_json(d) for d in json.loads(path.read_text(encoding="utf-8"))]


def load(fund: FundConfig) -> MarketData:
    issues: list[str] = []
    pcf = load_pcfs(fund) if fund.holdings == "pcf" else {}
    reports = load_reports(fund) if fund.holdings == "reports" else []

    nav: dict[date, float] = {}
    nav4: dict[date, float] = {}
    for p in pcf.values():
        if p.nav_date and p.nav is not None:
            nav[p.nav_date] = p.nav
            nav4[p.nav_date] = p.nav4 if p.nav4 is not None else round(p.nav, 4)
    em_path = DATA / "nav" / f"{fund.fund_id}_eastmoney.json"
    for d, v in _read_series(em_path, _nav_rows_parser).items():
        if d in nav4 and abs(nav4[d] - v) > 1e-9:
            issues.append(f"净值不一致 {d}：PCF {nav4[d]} / 天天基金 {v}")
        nav4.setdefault(d, v)
        nav.setdefault(d, v)

    if fund.holdings == "pcf":
        codes = {c: "us" for p in pcf.values() for c, h in p.holdings.items()
                 if h.market not in ("深圳市场", "上海市场")}
    else:
        codes = {a.code: a.market for a in fund.assets}
    px = {}
    for code, market in codes.items():
        series = _read_series(_price_path(code, market), nasdaq.parse_stock if market == "us" else None)
        if series:
            px[code] = dict(sorted(series.items()))
        else:
            issues.append(f"缺少 {code} 的行情")
    divs = {}
    for code, market in codes.items():
        p = DATA / "us_divs" / f"{code}.json"
        if market == "us" and p.exists():
            divs[code] = nasdaq.parse_dividends(p.read_bytes())

    fx_all: dict[str, dict[date, float]] = {}
    for pair in fund.fx_pairs:
        ccy, unit = pair_currency(pair)
        path = _fx_path(pair)
        if path.exists():
            fx_all[ccy] = {date.fromisoformat(k): v / unit for k, v in json.loads(path.read_text()).items()}
    fx = fx_all.get(pair_currency(fund.fx_pair)[0], {})

    etf = _read_series(DATA / "etf" / f"{fund.fund_id}_sina.json", sina.parse_kline)
    index: dict = {}
    if fund.index_id:
        idx_json, idx_xlsx = DATA / "index" / f"{fund.index_id}.json", DATA / "index" / f"{fund.index_id}.xlsx"
        index = _read_series(idx_json) or (nasdaq.parse_index_history(idx_xlsx.read_bytes()) if idx_xlsx.exists() else {})
    jpath = _jisilu_path(fund)
    js = jisilu.parse_hist(jpath.read_bytes()) if jpath.exists() else {}

    return MarketData(
        fund=fund,
        pcf=pcf,
        nav=dict(sorted(nav.items())),
        nav4=dict(sorted(nav4.items())),
        etf=dict(sorted(etf.items())),
        fx=fx,
        px=px,
        divs=divs,
        index=dict(sorted(index.items())),
        jisilu=js,
        fx_all=fx_all,
        markets=codes,
        reports=reports,
        issues=issues,
    )
