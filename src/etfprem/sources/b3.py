"""巴西交易所 B3 官方历史行情 COTAHIST：巴西上市 ETF 的每日收盘价。

按日 COTAHIST_DddMMyyyy.ZIP（约 0.6MB，巴西利亚时间当晚发布，北京时间次日 8 点前），
按月 COTAHIST_MmmYYYY.ZIP（约 9MB，月份结束后才有）。非交易日的日文件、尚未结束的月份返回 404。
服务器单连接只有 20 到 120KB/s，并发可以叠加，所以按文件并发下载。
文件为定长 245 字符、Latin-1 编码的文本；价格是 11 位整数加 2 位小数（雷亚尔），再除以报价因子。
"""

from __future__ import annotations

import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from ..net import NotFound, fetch

URL = "https://bvmf.bmfbovespa.com.br/InstDados/SerHist/COTAHIST_{stamp}.ZIP"


def _get(stamp: str) -> bytes | None:
    try:
        return fetch(URL.format(stamp=stamp), timeout=300)
    except NotFound:
        return None


def parse(raw: bytes, tickers: set[str]) -> dict[str, dict[date, float]]:
    """{代码: {交易日: 收盘价}}，只取现货市场（TPMERC=010）。"""
    out: dict[str, dict[date, float]] = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for name in z.namelist():
            for line in z.read(name).decode("latin-1").splitlines():
                code = line[12:24].strip()
                if line[:2] != "01" or line[24:27] != "010" or code not in tickers:
                    continue
                factor = int(line[210:217]) or 1
                d = date(int(line[2:6]), int(line[6:8]), int(line[8:10]))
                out.setdefault(code, {})[d] = int(line[108:121]) / 100 / factor
    return out


def download(tickers: set[str], start: date, end: date, workers: int = 6) -> dict[str, dict[date, float]]:
    """[start, end] 内的收盘价。已结束且需要超过 8 个工作日的月份用月文件，其余逐日下载。"""
    stamps = []
    month = date(start.year, start.month, 1)
    while month <= end:
        nxt = date(month.year + month.month // 12, month.month % 12 + 1, 1)
        first, last = max(month, start), min(nxt - timedelta(days=1), end)
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        days = [d for d in days if d.weekday() < 5]
        if len(days) > 8 and nxt <= date.today():
            stamps.append(f"M{month.month:02d}{month.year}")
        else:
            stamps += ["D" + d.strftime("%d%m%Y") for d in days]
        month = nxt
    out: dict[str, dict[date, float]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for raw in pool.map(_get, stamps):
            for code, bars in (parse(raw, tickers) if raw else {}).items():
                out.setdefault(code, {}).update({d: v for d, v in bars.items() if start <= d <= end})
    return {c: dict(sorted(s.items())) for c, s in out.items()}
