"""基金定期报告（季报、中报、年报）：从天天基金公告列表下载 PDF，用 pdftotext 抽文字，解析基金投资明细。

没有每日申赎清单的基金（例如 QDII-LOF）只能靠定期报告了解持仓。报告按发布日生效，
用发布日而不是报告期末来选报告，避免用到当时还没公开的信息。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable

from ..net import fetch

LIST = "https://api.fund.eastmoney.com/f10/JJGG?callback=&fundcode={code}&pageIndex={page}&pageSize=50&type=3"
PDF = "https://pdf.dfcfw.com/pdf/H2_{id}_1.pdf"
_NUM_ROW = re.compile(r"([\d,]+\.\d{2})\s+(\d{1,3}\.\d{2})\s*$")
_PERIOD = re.compile(r"(\d{4})\s*年\s*(?:第\s*([1-4])\s*季度|(中期|半年度)|(年度))\s*报告")
_QUARTER_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


@dataclass
class FundReport:
    id: str
    title: str
    publish_date: date
    period_end: date
    holdings: dict[str, float] = field(default_factory=dict)  # 资产代码 -> 占基金资产净值比例（小数）
    fair_values: dict[str, float] = field(default_factory=dict)  # 资产代码 -> 公允价值（人民币元）
    unknown: list[str] = field(default_factory=list)  # 没能对应到已知资产的行

    def to_json(self) -> dict:
        d = asdict(self)
        d["publish_date"] = self.publish_date.isoformat()
        d["period_end"] = self.period_end.isoformat()
        return d

    @classmethod
    def from_json(cls, d: dict) -> "FundReport":
        d = dict(d)
        d["publish_date"] = date.fromisoformat(d["publish_date"])
        d["period_end"] = date.fromisoformat(d["period_end"])
        return cls(**d)


def period_end(title: str) -> date | None:
    m = _PERIOD.search(title)
    if not m:
        return None
    year = int(m.group(1))
    if m.group(2):
        month, day = _QUARTER_END[int(m.group(2))]
    elif m.group(3):
        month, day = 6, 30
    else:
        month, day = 12, 31
    return date(year, month, day)


def list_reports(code: str) -> list[dict]:
    """[{id, title, publish_date}]，只保留能识别报告期的定期报告。"""
    out = []
    page = 1
    while True:
        j = json.loads(fetch(LIST.format(code=code, page=page), headers={"Referer": "https://fundf10.eastmoney.com/"}))
        batch = j.get("Data") or []
        for r in batch:
            title = r.get("TITLE", "")
            if "摘要" in title or "英文" in title or period_end(title) is None:
                continue
            out.append({"id": r["ID"], "title": title, "publish_date": (r.get("PUBLISHDATEDesc") or r["PUBLISHDATE"])[:10]})
        if not batch or page * 50 >= (j.get("TotalCount") or 0):
            return out
        page += 1


def download_pdf(report_id: str) -> bytes:
    raw = fetch(PDF.format(id=report_id))
    if not raw.startswith(b"%PDF"):
        raise RuntimeError(f"{report_id} 不是 PDF")
    return raw


def pdf_text(path: Path) -> str:
    exe = shutil.which("pdftotext")
    if not exe:
        raise RuntimeError("需要 poppler 的 pdftotext：brew install poppler")
    res = subprocess.run([exe, "-layout", str(path), "-"], capture_output=True, check=True)
    return res.stdout.decode("utf-8", "replace")


def _section(lines: list[str]) -> list[str]:
    """“基金投资明细”一节的正文行（跳过目录里的同名条目）。"""
    start = next(
        (i for i, l in enumerate(lines) if "基金投资明细" in l and "..." not in l and "…" not in l), None
    )
    if start is None:
        return []
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^\s*(\d+\.\d+(\.\d+)?\s+\S|§)", lines[j]):
            end = j
            break
    body = lines[start + 1 : end]
    # 去掉分页处的页眉页脚
    return [l for l in body if not re.search(r"第\s*\d+\s*页|报告\s*$", l)]


def parse_fund_holdings(text: str, assets: Iterable) -> tuple[dict[str, float], dict[str, float], list[str]]:
    """按关键词把表格行对应到已知资产。表格里的名称会跨行折断，所以按“离哪一行数字最近”归属。"""
    lines = _section(text.splitlines())
    rows = [(i, m) for i, l in enumerate(lines) if (m := _NUM_ROW.search(l))]
    if not rows:
        return {}, {}, []
    norm = [re.sub(r"\s+", "", l) for l in lines]
    owner: dict[int, str] = {}
    for a in assets:
        hits = [j for j, l in enumerate(norm) if any(k in l for k in a.keywords)]
        if not hits:
            continue
        nearest = min((r[0] for r in rows), key=lambda i: min(abs(i - j) for j in hits))
        if nearest in owner and owner[nearest] != a.code:
            raise ValueError(f"报告行归属冲突：{owner[nearest]} 与 {a.code}")
        owner[nearest] = a.code
    pct: dict[str, float] = {}
    fv: dict[str, float] = {}
    unknown: list[str] = []
    for i, m in rows:
        value, ratio = float(m.group(1).replace(",", "")), float(m.group(2)) / 100
        if i in owner:
            pct[owner[i]] = pct.get(owner[i], 0.0) + ratio
            fv[owner[i]] = fv.get(owner[i], 0.0) + value
        else:
            lo = max(0, i - 3)
            unknown.append(f"{''.join(norm[lo:i + 3])[:80]}（{ratio:.2%}）")
    return pct, fv, unknown
