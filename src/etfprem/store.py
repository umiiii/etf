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
from .config import DATA, LOCAL_MARKETS, FundConfig, Series, pair_currency
from .net import FetchError, NotFound
from .sources import (b3, bcb, cfets, csindex, csop, eastmoney, euronext, fund_reports, jisilu, kabutan, lionglobal,
                      msci, nasdaq, sina, szse_pcf, tencent)

Log = Callable[[str], None]

PRICE_DIRS = {"us": "us_prices", "sz": "cn_prices", "sh": "cn_prices", "jp": "jp_prices"}


def _pcf_dir(fund: FundConfig) -> Path:
    return DATA / "pcf" / fund.pcf_id


def _series_path(series: Series) -> Path:
    return DATA / "index" / f"{series.key}.json"


def _reports_dir(fund: FundConfig) -> Path:
    return DATA / "fund_reports" / fund.fund_id


def _fx_path(pair: str) -> Path:
    """USD/CNY -> USD_CNY.json，USD/CNY@16 -> USD_CNY_16h.json，BRL/CNY@BJ22 -> BRL_CNY_BJ22.json。"""
    return DATA / "fx" / f"{pair.replace('/', '_').replace('@16', '_16h').replace('@', '_')}.json"


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
    splits: dict[str, list[tuple[date, float]]] = field(default_factory=dict)  # 代码 -> [(清单改用拆股后股数的首日, 比例)]
    published_days: list[date] = field(default_factory=list)  # 指数日线的公布市场交易日（见 Series.published）

    @property
    def calendar(self) -> Calendar:
        """A 股交易日与境外交易日。没有美股持仓的基金，境外交易日取指数（或被投资 ETF）的交易日。"""
        cn = set(self.pcf) | set(self.nav) | set(self.etf)
        foreign = {d for c, s in self.px.items() if self.markets.get(c, "us") == "us" for d in s} or set(self.index)
        return Calendar(cn, foreign)


# ---------------------------------------------------------------- 下载

def update_pcfs(fund: FundConfig, start: date, end: date, log: Log, pause: float = 0.5, workers: int = 1) -> int:
    """逐个工作日下载深交所申赎清单，每天一个文件。不存在的日子记入 _missing.json。

    深交所对请求频率很敏感：4 个线程连续下载约 300 个文件后会返回 403，并封禁一段时间，所以默认单线程慢速下载。
    """
    from concurrent.futures import ThreadPoolExecutor

    folder = _pcf_dir(fund)
    folder.mkdir(parents=True, exist_ok=True)
    missing_file = folder / "_missing.json"
    missing = set(json.loads(missing_file.read_text())) if missing_file.exists() else set()
    today = date.today()
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    todo = [d for d in days if d.weekday() < 5 and not (folder / f"{d:%Y%m%d}.txt").exists() and f"{d:%Y%m%d}" not in missing]

    def one(d: date) -> str:
        try:
            (folder / f"{d:%Y%m%d}.txt").write_bytes(szse_pcf.download(fund.pcf_id, d))
            return "new"
        except NotFound:
            return "missing" if d < today else "later"
        finally:
            time.sleep(pause)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, todo))
    missing |= {f"{d:%Y%m%d}" for d, r in zip(todo, results) if r == "missing"}
    missing_file.write_text(json.dumps(sorted(missing)))
    new = results.count("new")
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
        # 已退市（被收购等）的股票 Nasdaq 不再提供历史，改用腾讯的未复权价
        series = tencent.us_raw_close(sym, start, end)
        if not series:
            raise FetchError("无数据")
        _merge_prices(_price_path(sym, "us"), series, symbol=sym, source="tencent-raw")
        return
    _merge_prices(_price_path(sym, "us"), series, nasdaq.parse_stock, symbol=ns, source="nasdaq")
    div_path = DATA / "us_divs" / f"{sym}.json"
    if not div_path.exists() or time.time() - div_path.stat().st_mtime > 3 * 86400:
        time.sleep(pause)
        div_path.write_bytes(nasdaq.download_dividends(ns, assetclass))
    time.sleep(pause)


_DONE: set[tuple] = set()  # 本进程内已下载过的 (类别, 代码, 起, 止)，批量更新多只基金时跳过重复下载


def _once(*key) -> bool:
    """第一次见到 key 时返回 True 并记下。"""
    if key in _DONE:
        return False
    _DONE.add(key)
    return True


def update_us(symbols: list[str], start: date, end: date, log: Log, assetclass: str = "stocks",
              pause: float = 0.25, workers: int = 4) -> list[str]:
    """Nasdaq 日线与分红，4 个线程并发。每个代码写各自的文件，互不冲突。"""
    from concurrent.futures import ThreadPoolExecutor

    symbols = [s for s in symbols if _once("us", s, start, end)]
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


def _read_fx(pair: str) -> dict[date, float]:
    path = _fx_path(pair)
    return {date.fromisoformat(k): v for k, v in json.loads(path.read_text()).items()} if path.exists() else {}


def _write_fx(pair: str, series: dict[date, float]) -> None:
    _fx_path(pair).write_text(json.dumps({k.isoformat(): v for k, v in sorted(series.items())}, indent=0))


def update_fx(pair: str, start: date, end: date, log: Log) -> None:
    if not _once("fx", pair, start, end):
        return
    if pair.startswith("BRL/CNY@"):
        update_brl(pair, start, end, log)
        return
    ccy = pair.split("/")[0]
    if ccy in cfets.CNY_BASE:
        # 外汇交易中心对这些货币报“1 元人民币兑多少外币”，取倒数
        series = _read_fx(pair)
        series.update({d: 1 / v for d, v in cfets.download(f"CNY/{ccy}", start, end).items() if v})
        _write_fx(pair, series)
        log(f"中间价 {pair}（由 CNY/{ccy} 取倒数）：{len(series)} 天")
        return
    if pair.endswith("@16"):
        series = _read_fx(pair)
        series.update(cfets.download_ref16(pair.removesuffix("@16"), start, end))
        _write_fx(pair, series)
        log(f"16:00 参考汇率 {pair.removesuffix('@16')}：{len(series)} 天")
        return
    path = _fx_path(pair)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {date.fromisoformat(k): v for k, v in json.loads(path.read_text()).items()} if path.exists() else {}
    # 外汇交易中心单次查询跨度不能超过一年，cfets.download 内部分段；本地缓存与新数据合并
    merged.update(cfets.download(pair, start, end))
    path.write_text(json.dumps({k.isoformat(): v for k, v in sorted(merged.items())}, indent=0))
    log(f"中间价 {pair}：{len(merged)} 天")


BRL_FIX = {"BJ22": "北京时间 22:00", "LDN16": "伦敦 16:00"}  # 雷亚尔取价时点，见 sources/bcb.py


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def update_brl(pair: str, start: date, end: date, log: Log) -> None:
    """雷亚尔兑人民币 = 外汇交易中心 16:00 美元参考汇率 / 美元兑雷亚尔（基金合同规定的取价时点）。

    美元兑雷亚尔取巴西央行同一时点的询价公告；巴西外汇休市（或半日市取不到该时点）时基金用的是彭博离岸报价，
    这里用东方财富 119.USDBRL 当日开盘与收盘的均值，再没有就沿用前值。16:00 参考汇率缺失时退回中间价。
    """
    rule = pair.split("@")[1]
    update_fx("USD/CNY@16", start, end, log)
    update_fx("USD/CNY", start, end, log)
    if _once("brl-sources", start, end):
        path = DATA / "fx" / "USD_BRL_PTAX.json"
        bulletins = _read_json(path)
        bulletins.update({d.isoformat(): v for d, v in bcb.download_bulletins(start, end).items()})
        path.write_text(json.dumps(dict(sorted(bulletins.items()))))
        path = DATA / "fx" / "USD_BRL_eastmoney.json"
        bars = _read_json(path)
        time.sleep(1.5)
        bars.update({d.isoformat(): v for d, v in eastmoney.download_kline_bars("119.USDBRL", start, end).items()})
        path.write_text(json.dumps(dict(sorted(bars.items()))))
    bulletins = {date.fromisoformat(k): [tuple(x) for x in v] for k, v in _read_json(DATA / "fx" / "USD_BRL_PTAX.json").items()}
    bars = {date.fromisoformat(k): v for k, v in _read_json(DATA / "fx" / "USD_BRL_eastmoney.json").items()}
    ref16, mid = _read_fx("USD/CNY@16"), _read_fx("USD/CNY")
    out, last = {}, None
    for d in sorted(mid):
        hhmm = "11:00" if rule == "BJ22" else bcb.london_4pm_brt(d)
        brl = bcb.rate_at(bulletins.get(d, []), hhmm)
        if brl is None and d in bars:
            brl = (bars[d]["open"] + bars[d]["close"]) / 2
        brl = brl or last
        cny = ref16.get(d) or mid[d]
        if brl:
            out[d] = cny / brl
            last = brl
    _write_fx(pair, out)
    log(f"雷亚尔（16:00 美元参考汇率 / {BRL_FIX[rule]}美元兑雷亚尔）：{len(out)} 天")


def download_series(series: Series, start: date, end: date) -> dict[date, float]:
    if series.source == "nasdaq":
        return nasdaq.parse_index_history(nasdaq.download_index_history(series.symbol, start, end))
    if series.source == "nasdaq_etf":
        return nasdaq.parse_stock(nasdaq.download_stock(series.symbol, start, end, "etf"))
    if series.source == "tencent":
        return tencent.download_kline(series.symbol[2:], series.symbol[:2], start, end, fq="")
    if series.source == "eastmoney":
        time.sleep(1.5)  # 行情接口请求过快会断开
        return eastmoney.download_kline(series.symbol, start, end)
    if series.source == "csindex":
        if series.symbol in csindex.RECIPES:
            return csindex.download_recipe(series.symbol, start, end, _read_fx("HKD/CNY"))
        return csindex.download(series.symbol, start, end)
    if series.source == "lionglobal":
        return lionglobal.download(series.symbol, start, end)
    if series.source == "euronext":
        return euronext.download(series.symbol, start, end)
    if series.source == "csop":
        return csop.download(series.symbol, start, end)
    if series.source == "b3":
        return b3.download({series.symbol}, start, end).get(series.symbol, {})
    if series.source == "msci":
        code, _, variant = series.symbol.partition(":")
        return msci.download(code, start, end, variant or "NETR")
    raise ValueError(f"未知数据源：{series.source}")


def _update_b3(start: date, end: date, log: Log) -> None:
    """B3 的行情文件很大、下载很慢，一次取回所有已配置的巴西代码，分别写入各自的缓存。"""
    from .config import FUNDS

    wanted = {s.symbol: s for f in FUNDS.values() if (s := f.index_series) and s.source == "b3"}
    for symbol, closes in b3.download(set(wanted), start, end).items():
        merged = _merge_series(_series_path(wanted[symbol]), closes, source="b3", symbol=symbol, currency="BRL")
        log(f"{wanted[symbol].name}：{len(merged)} 天")


def update_series(series: Series, start: date, end: date, log: Log) -> None:
    """指数或被投资 ETF 的日线，按日期合并写入 data/index/{key}.json。"""
    if series.source == "b3":
        cached = _read_series(_series_path(series))
        # 增量更新只下载缓存之后的日子；首次下载从 start 开始
        since = max(start, max(cached) + timedelta(days=1)) if cached and min(cached) <= start + timedelta(days=7) else start
        if since <= end and _once("b3", since, end):
            _update_b3(since, end, log)
        return
    if not _once("series", series.key, start, end):
        return
    path = _series_path(series)
    xlsx = path.with_suffix(".xlsx")  # 旧版缓存的 Nasdaq 导出文件
    base = _read_series(path) or (nasdaq.parse_index_history(xlsx.read_bytes()) if xlsx.exists() else {})
    base.update(download_series(series, start, end))
    merged = _merge_series(path, base, source=series.source, symbol=series.symbol, currency=series.currency)
    xlsx.unlink(missing_ok=True)
    log(f"{series.name}：{len(merged)} 天")


# ---------------------------------------------------------------- 拆股

SPLIT_RATIOS = (1.5, 2, 2.5, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50)


def _is_split_ratio(ratio: float, tol: float = 0.05) -> float | None:
    """清单股数前后之比接近常见拆股比例（或其倒数，即合股）时返回该比例。

    拆股常和指数调仓同一天生效，股数比会偏离整数几个百分点，所以容差放到 5%，最终比例以价格核对为准。
    """
    k = ratio if ratio >= 1 else 1 / ratio
    if k < 1.4:
        return None
    best = min(SPLIT_RATIOS, key=lambda x: abs(k / x - 1))
    if abs(k / best - 1) > tol:
        return None
    return float(best) if ratio >= 1 else 1 / best


def _snap_fraction(x: float) -> float:
    from fractions import Fraction

    return float(Fraction(x).limit_denominator(4))


def split_candidates(pcfs: dict[date, szse_pcf.PCF]) -> list[tuple[str, date, date, float]]:
    """清单股数突然按整数比变化的 (代码, 上一份清单日, 变化当日, 比例)。

    股数太小时取整也会造成 1→2 这样的跳变，所以 1.5 到 3 倍之间的变化要求前后股数都不少于 4。
    """
    last: dict[str, tuple[date, int]] = {}
    out = []
    for T in sorted(pcfs):
        for code, h in pcfs[T].holdings.items():
            if h.market in ("深圳市场", "上海市场", "香港市场") or h.qty <= 0:
                continue
            if code in last:
                d0, q0 = last[code]
                k = _is_split_ratio(h.qty / q0)
                if k and (max(k, 1 / k) >= 3 or min(q0, h.qty) >= 4):
                    out.append((code, d0, T, k))
            last[code] = (T, h.qty)
    return out


def _median(xs: list[float]) -> float | None:
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def confirm_split(code: str, d0: date, T: date, k: float, adj: dict[date, float]) -> tuple[str, float]:
    """用腾讯未复权价核对：未复权价 / Nasdaq 复权价 在变化前后之比就是拆股比例。

    返回 (confirmed / rejected / unverified, 比例)。核对通过时比例取价格算出的值（吸附到简单分数），否则沿用股数比。
    """
    try:
        raw = tencent.us_raw_close(code, d0 - timedelta(days=40), T + timedelta(days=10))
    except FetchError:
        raw = {}
    before = _median([raw[d] / adj[d] for d in raw if d in adj and d <= d0 - timedelta(days=3)])
    after = _median([raw[d] / adj[d] for d in raw if d in adj and d >= T])
    if not before or not after:
        return "unverified", k
    f = before / after
    if abs(f / k - 1) < 0.08:
        return "confirmed", _snap_fraction(f)
    return "rejected", k


def rebalance_days(pcfs: dict[date, szse_pcf.PCF], share: float = 0.3) -> set[date]:
    """指数调仓日：超过三成成分股的股数同时变化 5% 以上。"""
    out, days = set(), sorted(pcfs)
    for a, b in zip(days, days[1:]):
        prev, cur = pcfs[a].holdings, pcfs[b].holdings
        common = [c for c in cur if c in prev and prev[c].qty > 0 and cur[c].qty > 0]
        if common and sum(abs(cur[c].qty / prev[c].qty - 1) > 0.05 for c in common) >= share * len(common):
            out.add(b)
    return out


def update_splits(fund: FundConfig, pcfs: dict[date, szse_pcf.PCF], log: Log) -> None:
    """找出清单里的拆股并逐个核对，结果写入 data/pcf/{清单基金}/_splits.json。已核对过的不再请求。

    价格核对不了（腾讯没有该股的未复权价）时，调仓日的股数变化按调仓处理，其余日子按股数比当作拆股。
    """
    path = _pcf_dir(fund) / "_splits.json"
    known = {(r["code"], r["date"], r.get("qty_ratio", r["factor"])): r
             for r in (json.loads(path.read_text()) if path.exists() else [])}
    rebalances = rebalance_days(pcfs)
    for code, d0, T, k in split_candidates(pcfs):
        key = (code, T.isoformat(), k)
        if key in known:
            continue
        adj = _read_series(_price_path(code, "us"), nasdaq.parse_stock)
        status, factor = confirm_split(code, d0, T, k, adj)
        if status == "unverified" and T in rebalances:
            status = "rejected"  # 调仓日，股数变化来自调仓
        known[key] = {"code": code, "date": T.isoformat(), "prev": d0.isoformat(), "qty_ratio": k, "factor": factor,
                      "status": status}
        if status != "rejected":
            log(f"  拆股 {code}：{d0} → {T} 比例 {factor:g}（{'已用未复权价核对' if status == 'confirmed' else '未能核对，按股数比'}）")
        time.sleep(0.2)
    path.write_text(json.dumps(sorted(known.values(), key=lambda r: (r["date"], r["code"])), ensure_ascii=False, indent=1))


def load_splits(fund: FundConfig) -> dict[str, list[tuple[date, float]]]:
    path = _pcf_dir(fund) / "_splits.json"
    out: dict[str, list[tuple[date, float]]] = {}
    for r in json.loads(path.read_text()) if path.exists() else []:
        if r["status"] != "rejected":
            out.setdefault(r["code"], []).append((date.fromisoformat(r["date"]), r["factor"]))
    return out


def update_bars(fund: FundConfig, start: date, end: date, log: Log) -> None:
    """场内日线。收盘后新浪日线要过一阵才有当天这一根，先用实时行情补上（15:00 以后的实时价就是收盘价），
    第二天下载到正式日线时按日期覆盖。"""
    from datetime import datetime, timezone

    bars = sina.parse_kline(sina.download_kline(fund.fund_id, fund.market, n=max(300, (end - start).days)))
    now = datetime.now(timezone(timedelta(hours=8)))
    if end >= now.date() and now.date() not in bars and now.weekday() < 5 and (now.hour, now.minute) >= (15, 5):
        try:
            q = sina.realtime(fund.fund_id, fund.market)
            if q["date"] == now.date() and q["price"] > 0:
                bars[now.date()] = {"open": q["open"], "high": q["high"], "low": q["low"], "close": q["price"]}
        except (RuntimeError, FetchError, ValueError):
            pass
    bars = _merge_series(DATA / "etf" / f"{fund.fund_id}_sina.json", bars, sina.parse_kline, source="sina")
    log(f"场内日线：{len(bars)} 天")


def update(fund: FundConfig, start: date, end: date, *, log: Log = print, jisilu_hist: bool = True) -> None:
    """下载 [start, end] 需要的数据。时间序列多取 120 天，供敞口回归使用。"""
    for sub in ("us_prices", "us_divs", "fx", "nav", "etf", "index"):
        (DATA / sub).mkdir(parents=True, exist_ok=True)
    pad = start - timedelta(days=120)

    if fund.holdings == "pcf":
        if _once("pcf", fund.pcf_id, start, end):
            update_pcfs(fund, start, end + timedelta(days=1), log)
        pcfs = load_pcfs(fund)
        symbols = sorted({c for p in pcfs.values() if start <= p.trade_date for c, h in p.holdings.items()
                          if h.market not in ("深圳市场", "上海市场", "香港市场")})
        update_us(symbols, pad, end, log)
        if _once("splits", fund.pcf_id):
            update_splits(fund, pcfs, log)
    elif fund.holdings == "reports":
        # 报告按发布日生效，最早要覆盖起始日当时正在用的那份
        update_reports(fund, pad - timedelta(days=120), log)
        update_us([a.code for a in fund.assets if a.market == "us"], pad, end, log, assetclass="etf")
        update_cn([(a.code, a.market) for a in fund.assets if a.market in LOCAL_MARKETS], pad, end, log)
        update_jp([a.code for a in fund.assets if a.market == "jp"], pad, log)

    for pair in fund.fx_pairs:
        update_fx(pair, pad, end, log)

    update_bars(fund, start, end, log)

    # 成立以来的净值一次取回，最近一个月再用分页接口补一遍（全量接口偶尔晚一天）
    navs = {d: v for d, v in eastmoney.download_nav_full(fund.fund_id).items() if d >= pad}
    navs.update(eastmoney.parse_nav(eastmoney.download_nav(fund.fund_id, max(pad, end - timedelta(days=30)), end)))
    navs = _merge_series(DATA / "nav" / f"{fund.fund_id}_eastmoney.json", navs, _nav_rows_parser, source="eastmoney")
    log(f"天天基金净值：{len(navs)} 条")

    if fund.index_series:
        since = max(pad, min(navs) - timedelta(days=14)) if navs else pad  # 基金成立前的日线用不上
        update_series(fund.index_series, since, end, log)
        if fund.index_series.published:
            update_series(fund.index_series.published, since, end, log)

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
    for p in pcf.values() if fund.pcf_fund is None else ():  # 借用的清单里是别的基金的净值
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
                 if h.market not in ("深圳市场", "上海市场", "香港市场")}
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
    if fund.index_series:
        idx_json = _series_path(fund.index_series)
        idx_xlsx = idx_json.with_suffix(".xlsx")
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
        splits=load_splits(fund) if fund.holdings == "pcf" else {},
        published_days=sorted(_read_series(_series_path(fund.index_series.published)))
        if fund.index_series and fund.index_series.published else [],
    )
