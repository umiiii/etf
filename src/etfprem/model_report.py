"""溢价率建模报告：终端摘要、Markdown、HTML（含交互图表）。"""

from __future__ import annotations

import json
import math
from html import escape
from pathlib import Path

import numpy as np

from .analysis import HORIZONS, PREDICTORS, TARGETS, FundAnalysis

SHORT = (1, 5, 10, 20)


def _t(r: dict | None) -> str:
    return "—" if not r else f"{r['b']:+.3f}（t {r['t']:+.1f}）"


def _pct(x: float, nd: int = 2) -> str:
    return f"{x * 100:+.{nd}f}%"


def key_numbers(a: FundAnalysis) -> dict:
    regs = a.regressions
    price = [regs[(x, "rP", h)] for x in PREDICTORS for h in SHORT if regs[(x, "rP", h)]]
    prem = [regs[(x, "dZ", h)] for x in PREDICTORS for h in SHORT if regs[(x, "dZ", h)]]
    s = a.structural
    return {
        "price_total": len(price),
        "price_sig": sum(abs(r["t"]) >= 1.96 for r in price),
        "price_max_r2": max(r["r2"] for r in price),
        "prem_sig": sum(abs(r["t"]) >= 1.96 and r["b"] < 0 for r in prem),
        "prem_total": len(prem),
        "nav_day1_t": a.nav_day1[("dz1", 1)]["t"],
        "oos_price_max_r2": max(s["oos"][f"rP{h}"]["r2"] for h in SHORT if f"rP{h}" in s["oos"]),
        "oos_price_max_t": max(abs(s["oos"][f"rP{h}"]["t"]) for h in SHORT if f"rP{h}" in s["oos"]),
        "ceiling_max": max(s["ceiling"][h] for h in SHORT),
    }


# ------------------------------------------------------------------ 终端

def console_summary(analyses: list[FundAnalysis]) -> str:
    out = []
    for a in analyses:
        k, s, p = key_numbers(a), a.structural, a.structural["params"]
        df = a.panel
        out += [
            f"{a.fund.fund_id} {a.fund.name}  {df.index[0]} ~ {df.index[-1]}，有效 {int(df['valid'].sum())} 天",
            f"  溢价动态：全样本 AR(1) 半衰期 {a.dynamics['half_life']:.0f} 天，ADF p={a.dynamics['adf_p']:.2f}（不能拒绝单位根）",
            f"  结构模型：锚日波动 {p['sigma_anchor']:.2%}，错误定价半衰期 {s['half_life']:.1f} 天，"
            f"信息领先占溢价日变化 {s['dz_share']['信息领先']:.0%}，λ={p['lam']:.2f}",
            f"  溢价变化可预测：{k['prem_sig']}/{k['prem_total']} 个回归显著为负；次日净值被当日溢价变化预测，t={k['nav_day1_t']:+.1f}",
            f"  价格可预测性：{k['price_sig']}/{k['price_total']} 个回归显著，最大 R² {k['price_max_r2']:.1%}；"
            f"结构模型样本外最大 R² {k['oos_price_max_r2']:.1%}，理论上限 {k['ceiling_max']:.1%}",
            "  按偏离 20 日均值分五组，之后 5 日平均：价格 / 净值 / 溢价变化",
        ]
        for q in a.quintiles:
            out.append(f"    第{q['q']}组 偏离{_pct(q['x'])}  {_pct(q['rP'])} / {_pct(q['rN'])} / {_pct(q['dZ'])}")
    return "\n".join(out)


# ------------------------------------------------------------------ 公共文字

CONCLUSIONS = [
    "溢价率会回归，但回归主要靠净值追上价格完成，而不是价格下跌。",
    "溢价率对之后 1 到 20 个交易日的价格涨跌没有稳定的预测力。",
    "溢价越高，价格中与净值无关的波动越大，持有高溢价份额承担了额外风险，样本里没有看到相应的额外收益。",
]


def method_block() -> list[str]:
    return [
        "**恒等式。** 记 p 为对数收盘价，n 为对数参考净值（前一 A 股交易日的官方净值），z = p − n 为对数溢价。"
        "任意 h 日都有：价格收益 = 净值收益 + 溢价变化。所以溢价对价格的预测力，等于它对净值的预测力加上它对溢价变化的预测力。",
        "**结构模型。** z = 锚 m + 错误定价 x + 信息领先 u。锚是随机游走，代表申购额度、暂停申购等体制；"
        "错误定价是 AR(1)，系数 φ；信息领先是场内交易时段已经反映、参考净值要到次日才反映的美股信息，"
        "次日净值收益 = μ + λ·u + 噪声。模型用卡尔曼滤波和极大似然估计，t 日只用 t 日及以前可见的数据。",
        "**推论。** 未来 h 日价格的预期收益 = h·μ + (φ^h − 1)·x + (λ − 1)·u。锚不可预测；信息领先在 λ≈1 时由净值兑现，"
        "不影响价格；只有错误定价会让价格相对净值回落。",
        "**样本。** 两只基金上市以来的全部交易日。剔除停牌日和长假后首个交易日，因为这两类日子的收盘价或参考净值不可比。",
        "**检验。** 预测回归用 Newey-West 标准误处理重叠收益；另做区块自助法置信区间、不重叠样本回归、分体制回归，"
        "以及每 60 天滚动重估参数的严格样本外检验。",
    ]


# ------------------------------------------------------------------ Markdown

def write_markdown(analyses: list[FundAnalysis], path: Path) -> None:
    names = "与".join(a.fund.fund_id for a in analyses)
    out = [f"# 溢价率与后续价格走势：{names}", "", "## 结论", ""]
    out += [f"{i}. {c}" for i, c in enumerate(CONCLUSIONS, 1)]
    out += [""]
    for a in analyses:
        k = key_numbers(a)
        out.append(
            f"- **{a.fund.fund_id}**：{k['price_total']} 个价格预测回归中 {k['price_sig']} 个显著，最大 R² {k['price_max_r2']:.1%}；"
            f"同样的变量预测溢价变化时 {k['prem_sig']}/{k['prem_total']} 个显著为负。"
            f"结构模型严格样本外预测价格的最大 R² 为 {k['oos_price_max_r2']:.1%}。"
        )
    out += ["", "## 方法", ""] + [f"- {m}" for m in method_block()] + [""]

    out += ["## 溢价率本身的动态", "", "| 指标 | " + " | ".join(a.fund.fund_id for a in analyses) + " |",
            "|---|" + "---|" * len(analyses)]
    rows = [
        ("样本（有效交易日）", lambda a: f"{a.panel.index[0]} 起，{int(a.panel['valid'].sum())} 天"),
        ("全样本 AR(1) 系数 / 半衰期", lambda a: f"{a.dynamics['rho']:.3f} / {a.dynamics['half_life']:.0f} 天"),
        ("ADF 单位根检验 p 值", lambda a: f"{a.dynamics['adf_p']:.2f}"),
        ("低溢价体制半衰期", lambda a: f"{a.dynamics.get('half_life_low', float('nan')):.1f} 天（{a.dynamics.get('n_low', 0)} 天）"),
        ("高溢价体制半衰期", lambda a: f"{a.dynamics.get('half_life_high', float('nan')):.1f} 天（{a.dynamics.get('n_high', 0)} 天）"),
    ]
    out += [f"| {n} | " + " | ".join(f(a) for a in analyses) + " |" for n, f in rows]
    out += ["", "高溢价体制指过去 60 日平均溢价超过 3%，此时申购套利受限。全样本更像非平稳过程，因为溢价会在体制之间整体平移。", ""]

    out += ["## 结构模型参数", "", "| 参数 | " + " | ".join(a.fund.fund_id for a in analyses) + " |",
            "|---|" + "---|" * len(analyses)]

    def par(name, fmt):
        return lambda a: fmt(a.structural["params"][name], a.structural["se"][name])

    rows = [
        ("锚的日波动 σ_η", par("sigma_anchor", lambda v, s: f"{v:.2%}（{s:.2%}）")),
        ("错误定价的日波动 σ_ε", par("sigma_mispricing", lambda v, s: f"{v:.2%}（{s:.2%}）")),
        ("错误定价持续性 φ", par("phi", lambda v, s: f"{v:.2f}（{s:.2f}）")),
        ("错误定价半衰期", lambda a: f"{a.structural['half_life']:.1f} 天"),
        ("信息领先的日波动 σ_u", par("sigma_info", lambda v, s: f"{v:.2%}（{s:.2%}）")),
        ("净值兑现比例 λ", par("lam", lambda v, s: f"{v:.2f}（{s:.2f}）")),
        ("溢价日变化方差：锚 / 错价 / 信息", lambda a: " / ".join(f"{v:.0%}" for v in a.structural["dz_share"].values())),
        ("价格可预测性理论上限（R²）", lambda a: " / ".join(f"{a.structural['ceiling'][h]:.1%}" for h in SHORT) + "（1/5/10/20 日）"),
        ("当前锚 / 错误定价", lambda a: f"{math.expm1(a.structural['states']['anchor'].iloc[-1]):.1%} / {a.structural['states']['mispricing'].iloc[-1]:+.2%}"),
    ]
    out += [f"| {n} | " + " | ".join(f(a) for a in analyses) + " |" for n, f in rows]
    out += ["", "括号内为标准误。理论上限假设能完全看清错误定价与信息领先成分，实际只能从价格里实时推断，达不到这个上限。", ""]

    for a in analyses:
        out += [f"## {a.fund.fund_id} 预测回归", "", "系数为回归斜率，括号内为 Newey-West t 值。", "",
                "| 预测变量 | 期限 | 价格 | 净值 | 溢价变化 |", "|---|---|---|---|---|"]
        for x, label in PREDICTORS.items():
            for h in HORIZONS:
                r = [a.regressions[(x, k, h)] for k in TARGETS]
                out.append(f"| {label} | {h} 日 | " + " | ".join(_t(v) for v in r) + " |")
        out += ["", "60 日期限只有约 12 个独立区间，仅供参考。", ""]
        out += [f"### {a.fund.fund_id} 按偏离 20 日均值分五组，之后 5 日平均", "",
                "| 分组 | 天数 | 平均偏离 | 价格 | 净值 | 溢价变化 |", "|---|---|---|---|---|---|"]
        for q in a.quintiles:
            out.append(f"| 第 {q['q']} 组 | {q['n']} | {_pct(q['x'])} | {_pct(q['rP'])} | {_pct(q['rN'])} | {_pct(q['dZ'])} |")
        out += ["", f"### {a.fund.fund_id} 价格发现检验", "",
                "| 预测变量 | 次日净值 | 第 2 至 5 日 | 第 2 至 10 日 | 第 2 至 20 日 |", "|---|---|---|---|---|"]
        for x in ("d20", "dz1"):
            out.append(f"| {PREDICTORS[x]} | " + " | ".join(_t(a.nav_day1[(x, h)]) for h in (1, 5, 10, 20)) + " |")
        mom = a.nav_day1["momentum"]
        out += ["", "控制过去 20 日净值涨幅后，偏离 20 日均值对净值的系数："
                + "、".join(f"{h} 日 {_t(mom[h])}" for h in (1, 5, 10, 20)) + "。", ""]
        out += [f"### {a.fund.fund_id} 稳健性", "", "| 回归 | 自助法 95% 区间 | 不重叠样本 |", "|---|---|---|"]
        for (x, h), r in a.robust.items():
            no = r["nonoverlap"]
            out.append(f"| {h} 日价格 ~ {PREDICTORS[x]} | [{r['ci'][0]:+.3f}, {r['ci'][1]:+.3f}] | "
                       + (f"{no['b']:+.3f}（t {no['t']:+.1f}，{no['n']} 个）" if no else "—") + " |")
        out += ["", "| 体制 | 天数 | 5 日价格 ~ 溢价水平 | 5 日溢价变化 ~ 溢价水平 | 20 日价格 ~ 溢价水平 | 20 日溢价变化 ~ 溢价水平 |",
                "|---|---|---|---|---|---|"]
        for name, label in (("low", "低溢价"), ("high", "高溢价")):
            out.append(f"| {label} | {a.regimes[(name, 'n')]} | " + " | ".join(
                _t(a.regimes[(name, 'z', k, h)]) for h in (5, 20) for k in ("rP", "dZ")) + " |")
        out += ["", f"### {a.fund.fund_id} 结构模型样本外检验", "",
                f"从 {a.structural['oos_from']} 起，每 60 天只用当时之前的数据重估参数，再做实时滤波预测。表中为实际值对预测值的回归斜率、t 值和 R²，斜率接近 1 才算预测有效。",
                "", "| 期限 | 价格 | 净值 | 溢价变化 |", "|---|---|---|---|"]
        for h in HORIZONS:
            cells = []
            for k in TARGETS:
                r = a.structural["oos"].get(f"{k}{h}")
                cells.append(f"{r['slope']:+.2f}（t {r['t']:+.1f}，R² {r['r2']:.1%}）" if r else "—")
            out.append(f"| {h} 日 | " + " | ".join(cells) + " |")
        out += [""]
        if a.variance:
            out += [f"### {a.fund.fund_id} 波动分解", "", "| 体制 | 价格日波动 | 净值日波动 | 溢价变化日波动 | 净值与溢价变化相关 | 溢价项占价格方差 |",
                    "|---|---|---|---|---|---|"]
            for name, label in (("low", "低溢价"), ("high", "高溢价")):
                v = a.variance.get(name)
                if v:
                    out.append(f"| {label} | {v['sd_price']:.2%} | {v['sd_nav']:.2%} | {v['sd_premium']:.2%} | {v['corr']:+.2f} | {v['premium_share']:.0%} |")
            out += [""]
    out += ["## 实际含义", "",
            "- 不要把溢价率当作买卖时点信号。溢价偏高之后价格并不系统性地下跌，因为偏高的部分往往是场内先反映了当晚美股的走势。",
            "- 真正会让价格相对净值回落的错误定价，一到两天就衰减一半，而且幅度远小于净值本身的波动。",
            "- 高溢价的代价主要是风险：锚是随机游走，恢复申购、额度放开等体制变化会让溢价快速收缩，这类变化无法从溢价水平提前预测。",
            "- 想要美股科技或半导体敞口时，高溢价阶段相当于以更高的波动换取同样的预期收益。",
            "", "## 局限", "",
            "- t 日收盘时参考净值尚未公布，模型假设它已知。实际可以用本项目的估算值代替，误差 1 到 3 个基点。",
            "- 样本只有约三年，高溢价体制主要集中在 2026 年，体制切换的次数很少。",
            "- 收盘价不是逐笔成交的瞬时价格，盘中溢价的日内动态没有建模。", ""]
    path.write_text("\n".join(out), encoding="utf-8")


# ------------------------------------------------------------------ HTML

def _html_table(head: list[str], rows: list[list[str]]) -> str:
    h = "".join(f"<th>{escape(c)}</th>" for c in head)
    b = "".join("<tr>" + "".join(f"<td>{escape(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table></div>'


def write_html(analyses: list[FundAnalysis], path: Path) -> None:
    payload = []
    sections = []
    for a in analyses:
        df = a.panel
        st = a.structural["states"]
        series = []
        for d in df.index:
            z = df.at[d, "z"]
            series.append({"d": d.isoformat(), "p": None if np.isnan(z) else round(math.expm1(z), 5),
                           "m": round(math.expm1(st.at[d, "anchor"]), 5)})
        payload.append({"id": a.fund.fund_id, "name": a.fund.name, "series": series,
                        "quint": [{k: (round(v, 6) if isinstance(v, float) else v) for k, v in q.items()} for q in a.quintiles]})
        k = key_numbers(a)
        reg_rows = []
        for x, label in PREDICTORS.items():
            for h in SHORT:
                reg_rows.append([label, f"{h} 日", *(_t(a.regressions[(x, t, h)]) for t in TARGETS)])
        oos_rows = []
        for h in SHORT:
            cells = []
            for t in TARGETS:
                r = a.structural["oos"].get(f"{t}{h}")
                cells.append(f"{r['slope']:+.2f}（t {r['t']:+.1f}，R² {r['r2']:.1%}）" if r else "—")
            oos_rows.append([f"{h} 日", *cells])
        p = a.structural["params"]
        sections.append(f"""
  <section class="card">
    <h2>{escape(a.fund.fund_id)} {escape(a.fund.name)}</h2>
    <p class="desc">{k['price_total']} 个价格预测回归中 {k['price_sig']} 个显著，最大 R² {k['price_max_r2']:.1%}；同样的变量预测溢价变化时 {k['prem_sig']}/{k['prem_total']} 个显著为负。
    错误定价半衰期 {a.structural['half_life']:.1f} 天，信息领先占溢价日变化的 {a.structural['dz_share']['信息领先']:.0%}，净值兑现比例 λ={p['lam']:.2f}。</p>
    <h3>溢价率与模型估计的锚</h3>
    <div class="legend"><span><i class="key" style="background:var(--series-1)"></i>溢价率</span><span><i class="key" style="background:var(--context)"></i>锚（实时滤波）</span></div>
    <div class="chart" id="line-{a.fund.fund_id}" tabindex="0" aria-label="{escape(a.fund.fund_id)} 溢价率与锚，可用左右方向键查看"></div>
    <h3>按溢价偏离 20 日均值分五组，之后 5 个交易日的平均变化</h3>
    <div class="legend"><span><i class="swatch" style="background:var(--series-1)"></i>价格</span><span><i class="swatch" style="background:var(--series-2)"></i>净值</span><span><i class="swatch" style="background:var(--series-3)"></i>溢价变化</span></div>
    <div class="chart" id="bars-{a.fund.fund_id}" tabindex="0" aria-label="{escape(a.fund.fund_id)} 分组柱状图，可用左右方向键查看"></div>
    {_html_table(['分组', '天数', '平均偏离', '价格', '净值', '溢价变化'], [[f"第 {q['q']} 组", q['n'], _pct(q['x']), _pct(q['rP']), _pct(q['rN']), _pct(q['dZ'])] for q in a.quintiles])}
    <h3>预测回归（斜率与 Newey-West t 值）</h3>
    {_html_table(['预测变量', '期限', '价格', '净值', '溢价变化'], reg_rows)}
    <h3>结构模型严格样本外检验（实际对预测回归，斜率接近 1 才有效）</h3>
    {_html_table(['期限', '价格', '净值', '溢价变化'], oos_rows)}
  </section>""")

    param_rows = []
    for label, fn in (
        ("锚的日波动", lambda a: f"{a.structural['params']['sigma_anchor']:.2%}"),
        ("错误定价日波动 / 半衰期", lambda a: f"{a.structural['params']['sigma_mispricing']:.2%} / {a.structural['half_life']:.1f} 天"),
        ("信息领先日波动 / λ", lambda a: f"{a.structural['params']['sigma_info']:.2%} / {a.structural['params']['lam']:.2f}"),
        ("溢价日变化方差：锚 / 错价 / 信息", lambda a: " / ".join(f"{v:.0%}" for v in a.structural["dz_share"].values())),
        ("价格可预测性理论上限（1/5/10/20 日）", lambda a: " / ".join(f"{a.structural['ceiling'][h]:.1%}" for h in SHORT)),
        ("高溢价体制：价格 / 净值日波动", lambda a: (f"{a.variance['high']['sd_price']:.2%} / {a.variance['high']['sd_nav']:.2%}" if 'high' in a.variance else "—")),
        ("当前锚 / 错误定价", lambda a: f"{math.expm1(a.structural['states']['anchor'].iloc[-1]):.1%} / {a.structural['states']['mispricing'].iloc[-1]:+.2%}"),
    ):
        param_rows.append([label, *(fn(a) for a in analyses)])

    html = (_HTML
            .replace("__TITLE__", "溢价率与后续价格走势")
            .replace("__CONCLUSIONS__", "".join(f"<li>{escape(c)}</li>" for c in CONCLUSIONS))
            .replace("__METHOD__", "".join(f"<li>{escape(m.replace('**', ''))}</li>" for m in method_block()))
            .replace("__PARAMS__", _html_table(["参数", *(a.fund.fund_id for a in analyses)], param_rows))
            .replace("__SECTIONS__", "".join(sections))
            .replace("__DATA__", json.dumps(payload, ensure_ascii=False)))
    path.write_text(html, encoding="utf-8")


def write_all(analyses: list[FundAnalysis], out_dir: Path) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md, html = out_dir / "premium_model.md", out_dir / "premium_model.html"
    write_markdown(analyses, md)
    write_html(analyses, html)
    return [md, html]


_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a; --context: #b3b2ab;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --context: #6b6a64;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --context: #6b6a64;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.6 system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
main { max-width: 1080px; margin: 0 auto; padding: 28px 16px 48px; }
h1 { font-size: 22px; font-weight: 600; margin: 0 0 12px; }
h2 { font-size: 17px; font-weight: 600; margin: 0 0 6px; }
h3 { font-size: 14px; font-weight: 600; margin: 18px 0 6px; color: var(--ink); }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; margin-bottom: 16px; }
.desc, .card li { color: var(--ink-2); }
ol, ul { padding-left: 20px; margin: 6px 0; }
.legend { display: flex; gap: 16px; flex-wrap: wrap; color: var(--ink-2); font-size: 13px; margin: 4px 0 6px; }
.key { display: inline-block; width: 16px; height: 2px; vertical-align: middle; margin-right: 6px; border-radius: 1px; }
.swatch { display: inline-block; width: 10px; height: 10px; vertical-align: middle; margin-right: 6px; border-radius: 2px; }
.chart { position: relative; width: 100%; outline: none; }
.chart:focus-visible { box-shadow: 0 0 0 2px var(--series-1); border-radius: 6px; }
.chart svg { display: block; width: 100%; overflow: visible; }
.chart text { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.tip { position: absolute; pointer-events: none; background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px 10px; font-size: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.12); min-width: 160px; display: none; z-index: 2; }
.tip .d { color: var(--ink-2); margin-bottom: 4px; }
.tip .row { display: flex; align-items: center; gap: 8px; justify-content: space-between; }
.tip .row b { font-weight: 600; color: var(--ink); font-variant-numeric: tabular-nums; }
.tip .row span { color: var(--ink-2); }
table { width: 100%; border-collapse: collapse; font-size: 13px; font-variant-numeric: tabular-nums; margin: 4px 0 8px; }
th, td { text-align: right; padding: 6px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { color: var(--ink-2); font-weight: 500; }
.scroll { overflow-x: auto; }
</style>
</head>
<body>
<main>
  <h1>__TITLE__</h1>
  <section class="card"><h2>结论</h2><ol>__CONCLUSIONS__</ol></section>
  <section class="card"><h2>模型参数对比</h2>__PARAMS__</section>
  __SECTIONS__
  <section class="card"><h2>方法</h2><ul>__METHOD__</ul></section>
</main>
<script>
const DATA = __DATA__;
const NS = 'http://www.w3.org/2000/svg';
const cssVar = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const pct = (v, nd = 2) => v == null ? '—' : (v * 100).toFixed(nd) + '%';
function el(tag, attrs, parent) { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; }
function text(parent, x, y, s, anchor) { const t = el('text', { x, y, 'text-anchor': anchor || 'middle' }, parent); t.textContent = s; return t; }
function niceStep(range, target) { const raw = range / target, mag = Math.pow(10, Math.floor(Math.log10(raw))), n = raw / mag; return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag; }

// 通用图表外壳：坐标轴、网格、悬停与键盘
function frame(host, n, height, domain, tick, render, rows, xLabel) {
  const tip = document.createElement('div'); tip.className = 'tip'; host.appendChild(tip);
  let svg = null, xs = [], active = -1, cross = null, W = 0;
  function draw() {
    if (svg) svg.remove();
    W = host.clientWidth; const H = height, m = { l: 46, r: 16, t: 10, b: 26 };
    const pw = W - m.l - m.r, ph = H - m.t - m.b;
    svg = el('svg', { viewBox: `0 0 ${W} ${H}`, height: H, role: 'img' }); host.insertBefore(svg, tip);
    const [lo, hi] = domain(); const step = niceStep(hi - lo || 1e-6, 5);
    const y0 = Math.floor(lo / step) * step, y1 = Math.ceil(hi / step) * step;
    const X = i => m.l + (i + 0.5) * pw / n, Y = v => m.t + ph - (v - y0) / (y1 - y0) * ph;
    xs = Array.from({ length: n }, (_, i) => X(i));
    for (let v = y0; v <= y1 + step / 2; v += step) {
      el('line', { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: cssVar(Math.abs(v) < 1e-12 ? '--axis' : '--grid'), 'stroke-width': 1 }, svg);
      text(svg, m.l - 6, Y(v) + 4, tick(v), 'end');
    }
    xLabel(svg, X, H, W);
    render(svg, X, Y, pw, ph, m);
    cross = el('line', { y1: m.t, y2: m.t + ph, stroke: cssVar('--axis'), 'stroke-width': 1, visibility: 'hidden' }, svg);
    const hit = el('rect', { x: m.l, y: 0, width: pw, height: H, fill: 'transparent' }, svg);
    hit.addEventListener('pointermove', e => {
      const r = svg.getBoundingClientRect(), x = (e.clientX - r.left) * (W / r.width);
      let best = 0, bd = Infinity; xs.forEach((v, i) => { const d = Math.abs(v - x); if (d < bd) { bd = d; best = i; } });
      show(best);
    });
    hit.addEventListener('pointerleave', hide);
    if (active >= 0) show(active);
  }
  function show(i) {
    active = i; cross.setAttribute('x1', xs[i]); cross.setAttribute('x2', xs[i]); cross.setAttribute('visibility', 'visible');
    tip.replaceChildren();
    const [title, items] = rows(i);
    const d = document.createElement('div'); d.className = 'd'; d.textContent = title; tip.appendChild(d);
    for (const [label, value, color, kind] of items) {
      const row = document.createElement('div'); row.className = 'row';
      const s = document.createElement('span');
      if (color) { const k = document.createElement('i'); k.className = kind === 'bar' ? 'swatch' : 'key'; k.style.background = color; s.appendChild(k); }
      s.appendChild(document.createTextNode(label));
      const b = document.createElement('b'); b.textContent = value; row.appendChild(s); row.appendChild(b); tip.appendChild(row);
    }
    tip.style.display = 'block';
    const scale = host.clientWidth / W, tw = tip.offsetWidth; let left = xs[i] * scale + 12;
    if (left + tw > host.clientWidth) left = xs[i] * scale - tw - 12;
    tip.style.left = Math.max(0, left) + 'px'; tip.style.top = '8px';
  }
  function hide() { active = -1; tip.style.display = 'none'; if (cross) cross.setAttribute('visibility', 'hidden'); }
  host.addEventListener('keydown', e => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return; e.preventDefault();
    show(active < 0 ? n - 1 : Math.max(0, Math.min(n - 1, active + (e.key === 'ArrowRight' ? 1 : -1))));
  });
  host.addEventListener('focus', () => { if (active < 0) show(n - 1); });
  host.addEventListener('blur', hide);
  new ResizeObserver(draw).observe(host);
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', draw);
  draw();
}

function path(points) { let s = '', pen = false; for (const p of points) { if (p == null) { pen = false; continue; } s += (pen ? 'L' : 'M') + p[0].toFixed(1) + ',' + p[1].toFixed(1); pen = true; } return s; }

for (const f of DATA) {
  const S = f.series;
  frame(document.getElementById('line-' + f.id), S.length, 260,
    () => { const v = S.flatMap(r => [r.p, r.m]).filter(x => x != null); return [Math.min(0, ...v), Math.max(...v)]; },
    v => (v * 100).toFixed(0) + '%',
    (svg, X, Y) => {
      el('path', { d: path(S.map((r, i) => [X(i), Y(r.m)])), fill: 'none', stroke: cssVar('--context'), 'stroke-width': 2, 'stroke-linejoin': 'round' }, svg);
      el('path', { d: path(S.map((r, i) => r.p == null ? null : [X(i), Y(r.p)])), fill: 'none', stroke: cssVar('--series-1'), 'stroke-width': 1.5, 'stroke-linejoin': 'round' }, svg);
    },
    i => [S[i].d, [['溢价率', pct(S[i].p), cssVar('--series-1')], ['锚', pct(S[i].m), cssVar('--context')]]],
    (svg, X, H) => { let last = '', lx = -1e9; S.forEach((r, i) => { const y = r.d.slice(0, 4); if (y !== last) { last = y; if (X(i) - lx > 40) { text(svg, X(i), H - 6, y); lx = X(i); } } }); });

  const Q = f.quint, keys = [['rP', '价格', '--series-1'], ['rN', '净值', '--series-2'], ['dZ', '溢价变化', '--series-3']];
  frame(document.getElementById('bars-' + f.id), Q.length, 240,
    () => { const v = Q.flatMap(q => keys.map(k => q[k[0]])); return [Math.min(0, ...v), Math.max(0, ...v)]; },
    v => (v * 100).toFixed(1) + '%',
    (svg, X, Y, pw) => {
      const slot = pw / Q.length, bw = Math.min(24, (slot * 0.7) / keys.length - 2);
      Q.forEach((q, i) => keys.forEach(([k, , c], j) => {
        const x = X(i) + (j - (keys.length - 1) / 2) * (bw + 2) - bw / 2, v = q[k];
        const y = Math.min(Y(0), Y(v)), h = Math.max(1, Math.abs(Y(v) - Y(0)));
        el('rect', { x, y, width: bw, height: h, fill: cssVar(c), rx: 2 }, svg);
      }));
    },
    i => [`第 ${Q[i].q} 组（平均偏离 ${pct(Q[i].x)}，${Q[i].n} 天）`, keys.map(([k, label, c]) => [label, pct(Q[i][k]), cssVar(c), 'bar'])],
    (svg, X, H) => Q.forEach((q, i) => text(svg, X(i), H - 6, i === 0 ? '偏离最低' : i === Q.length - 1 ? '偏离最高' : `第 ${q.q} 组`)));
}
</script>
</body>
</html>
"""
