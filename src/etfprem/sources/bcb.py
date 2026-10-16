"""巴西央行 PTAX 美元兑雷亚尔询价公告。

外汇交易中心不公布雷亚尔中间价，两只巴西 ETF 的基金合同都规定以美元为中介套算：美元兑人民币用外汇交易中心
16:00 参考汇率，美元兑雷亚尔用彭博报价，取价时点不同——
- 华夏（159100）：北京时间 22:00，即巴西利亚 11:00，对应央行 11 点那一轮询价；
- 易方达（520870）：伦敦 16:00，英国夏令时期间是巴西利亚 12:00，其余时间 13:00。
彭博历史报价没有免费来源，央行每天 10、11、12、13 点各公布一次询价结果（整点后约 7 分钟），取同一时点的那条。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from ..net import fetch

URL = (
    "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
    "CotacaoMoedaPeriodo(moeda=@moeda,dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)"
    "?@moeda='USD'&@dataInicial='{start}'&@dataFinalCotacao='{end}'&$format=json"
)


def download_bulletins(start: date, end: date, chunk_days: int = 180) -> dict[date, list[tuple[str, float]]]:
    """各轮询价公告的买卖中间价（不含收盘 PTAX）：{日期: [('HH:MM:SS', USD/BRL), ...]}，巴西利亚时间。

    正常交易日 4 条；半日市（12-24）只有 10、11 点；12-31 只有一条沿用前一日 PTAX 的公告；偶尔延迟到下一个小时。
    """
    out: dict[date, list[tuple[str, float]]] = {}
    cur = start
    while cur <= end:
        stop = min(end, cur + timedelta(days=chunk_days))
        raw = fetch(URL.format(start=cur.strftime("%m-%d-%Y"), end=stop.strftime("%m-%d-%Y")))
        for r in json.loads(raw).get("value") or []:
            if r["tipoBoletim"] != "Fechamento":
                ts = r["dataHoraCotacao"]
                out.setdefault(date.fromisoformat(ts[:10]), []).append((ts[11:19], (r["cotacaoCompra"] + r["cotacaoVenda"]) / 2))
        cur = stop + timedelta(days=1)
    return {d: sorted(v) for d, v in sorted(out.items())}


def rate_at(bulletins: list[tuple[str, float]], hhmm: str, tolerance: int = 45) -> float | None:
    """离 hhmm（巴西利亚时间）最近且相差不超过 tolerance 分钟的公告；巴西外汇休市或半日市取不到时返回 None。"""
    def minutes(t: str) -> int:
        return int(t[:2]) * 60 + int(t[3:5])

    if not bulletins:
        return None
    t, v = min(bulletins, key=lambda b: abs(minutes(b[0]) - minutes(hhmm)))
    return v if abs(minutes(t) - minutes(hhmm)) <= tolerance else None


def london_4pm_brt(d: date) -> str:
    """伦敦 16:00 对应的巴西利亚时间：英国夏令时（3 月最后一个周日至 10 月最后一个周日）为 12:00，否则 13:00。"""
    def last_sunday(month: int) -> date:
        x = date(d.year, month + 1, 1) - timedelta(days=1)
        return x - timedelta(days=(x.weekday() + 1) % 7)

    return "12:00" if last_sunday(3) <= d < last_sunday(10) else "13:00"
