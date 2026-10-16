"""只用标准库读取简单 xlsx（Nasdaq 指数历史导出）。"""

from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_rows(data: bytes, sheet: int = 1) -> list[list[object]]:
    z = zipfile.ZipFile(io.BytesIO(data))
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.iter(f"{_NS}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
    root = ET.fromstring(z.read(f"xl/worksheets/sheet{sheet}.xml"))
    rows: list[list[object]] = []
    for row in root.iter(f"{_NS}row"):
        cells: dict[int, object] = {}
        for c in row.iter(f"{_NS}c"):
            v = c.find(f"{_NS}v")
            t = c.get("t")
            if t == "inlineStr":
                cells[_col_index(c.get("r"))] = "".join(x.text or "" for x in c.iter(f"{_NS}t"))
            elif v is None:
                continue
            elif t == "s":
                cells[_col_index(c.get("r"))] = shared[int(v.text)]
            elif t in ("str", "b"):
                cells[_col_index(c.get("r"))] = v.text
            else:
                cells[_col_index(c.get("r"))] = float(v.text)
        if cells:
            rows.append([cells.get(i) for i in range(max(cells) + 1)])
    return rows


def excel_date(serial: float) -> date:
    return date(1899, 12, 30) + timedelta(days=int(serial))
