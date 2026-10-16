"""核心逻辑测试。依赖 data/ 缓存的用例在缓存缺失时跳过。运行：uv run python -m unittest -v"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date

from etfprem import history, store
from etfprem.calendar import Calendar
from etfprem.config import DATA, get_fund
from etfprem.estimator import Estimator, snap_split
from etfprem.sources import szse_pcf

FUND = get_fund("159509")
PCF_FILE = DATA / "pcf" / "159509" / "20261009.txt"
HAS_DATA = PCF_FILE.exists() and (DATA / "us_prices" / "NVDA.json").exists()


class TestPCF(unittest.TestCase):
    @unittest.skipUnless(PCF_FILE.exists(), "缺少缓存的 PCF")
    def test_parse(self):
        p = szse_pcf.parse(PCF_FILE.read_bytes(), "159509")
        self.assertEqual(p.trade_date, date(2026, 10, 9))
        self.assertEqual(p.nav_date, date(2026, 9, 30))  # 清单里的净值是 T 的前两个交易日
        self.assertEqual(len(p.holdings), 47)
        self.assertEqual(p.holdings["AAPL"].qty, 139)
        self.assertAlmostEqual(p.holdings["AAPL"].sub_amount, 346784.05)
        self.assertAlmostEqual(p.nav, 2.38040466)
        self.assertEqual(p.nav4, 2.3804)
        self.assertAlmostEqual(p.cash_diff, 138.81)
        self.assertAlmostEqual(p.est_cash_diff, 32.68)
        self.assertAlmostEqual(p.cash_component, 2618409.13)


class TestSplit(unittest.TestCase):
    def test_snap(self):
        self.assertEqual(snap_split(1.004), 1.0)
        self.assertEqual(snap_split(3.9999), 4.0)
        self.assertEqual(snap_split(10.002), 10.0)
        self.assertEqual(snap_split(0.5001), 0.5)
        self.assertEqual(snap_split(1.5), 1.5)


class TestCalendar(unittest.TestCase):
    def test_holiday_alignment(self):
        cn = [date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 8)]
        us = [date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 8)]
        cal = Calendar(cn, us)
        self.assertEqual(cal.prev_cn(date(2026, 10, 8)), date(2026, 9, 30))
        self.assertEqual(cal.next_cn(date(2026, 9, 30)), date(2026, 10, 8))
        self.assertEqual(cal.us_on_or_before(date(2026, 10, 7)), date(2026, 10, 5))
        self.assertEqual(
            cal.us_between(date(2026, 9, 30), date(2026, 10, 5)),
            [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5)],
        )


@unittest.skipUnless(HAS_DATA, "缺少 data/ 缓存，先运行 etfprem update")
class TestHistory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.md = store.load(FUND)
        cls.rows = history.compute(cls.md, date(2025, 10, 12), date(2026, 10, 12))
        cls.summary = history.summarize(cls.rows)

    def test_accuracy(self):
        nav = self.summary["nav_err_bp"]
        self.assertLess(nav["mae"], 1.5)
        self.assertLess(nav["max"], 15)
        self.assertLess(abs(nav["mean"]), 0.5)

    def test_official_premium_matches_jisilu(self):
        self.assertGreater(self.summary["jisilu_days"], 0)
        self.assertLess(self.summary["jisilu_vs_official_pp"]["max"], 0.006)

    def test_no_lookahead(self):
        """删掉 T 日开盘后才知道的数据，估算结果必须完全相同。"""
        md, full = self.md, Estimator(self.md)
        for D in (date(2025, 12, 22), date(2026, 5, 6), date(2026, 9, 21), date(2026, 10, 8)):
            st = full.step(D)
            T = full.cal.next_cn(D)
            cut = replace(
                md,
                pcf={k: v for k, v in md.pcf.items() if k <= T},
                nav={k: v for k, v in md.nav.items() if k <= st.Dp},
                nav4={k: v for k, v in md.nav4.items() if k <= st.Dp},
                etf={k: v for k, v in md.etf.items() if k <= T},
                fx={k: v for k, v in md.fx.items() if k <= T},
                px={c: {k: v for k, v in s.items() if k <= st.s1} for c, s in md.px.items()},
                index={k: v for k, v in md.index.items() if k <= st.s1},
                jisilu={},
            )
            a, b = full.estimate(D), Estimator(cut).estimate(D)
            self.assertEqual(a.nav, b.nav, D)
            self.assertEqual(a.exposure, b.exposure, D)

    def test_split_days_handled(self):
        """KLAC 1 拆 10、CRWD 1 拆 4 前后，复权股数应连续。"""
        est = Estimator(self.md)
        for code, before, on, after in (
            ("KLAC", date(2026, 6, 11), date(2026, 6, 12), date(2026, 6, 15)),
            ("CRWD", date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3)),
        ):
            q0, q1, q2 = (est.holdings(d).get(code) for d in (before, on, after))
            self.assertIsNotNone(q1, f"{code} 拆股当天应沿用前一份清单")
            self.assertAlmostEqual(q0, q1)
            self.assertLess(abs(q2 / q0 - 1), 0.3, f"{code} 拆股后复权股数应与拆股前接近")


# ---------------------------------------------------------------- 指数法：跟踪指数或被投资 ETF 的日线

from etfprem.estimator import IndexEstimator  # noqa: E402

NDX_FUND = get_fund("513100")
HAS_NDX = (DATA / "index" / "NDX.json").exists() and (DATA / "nav" / "513100_eastmoney.json").exists()


@unittest.skipUnless(HAS_NDX, "缺少 513100 的缓存，先运行 etfprem update --fund 513100")
class TestIndexMethod(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.md = store.load(NDX_FUND)
        cls.rows = history.compute(cls.md, date(2025, 10, 15), date(2026, 10, 15))
        cls.summary = history.summarize(cls.rows)

    def test_accuracy(self):
        nav = self.summary["nav_err_bp"]
        self.assertLess(nav["mae"], 1.5)
        self.assertLess(nav["max"], 10)

    def test_official_premium_matches_jisilu(self):
        self.assertGreater(self.summary["jisilu_days"], 40)
        self.assertLess(self.summary["jisilu_vs_official_pp"]["max"], 0.006)

    def test_no_lookahead(self):
        """删掉 T 日开盘后才知道的数据，估算结果必须完全相同。"""
        md, full = self.md, IndexEstimator(self.md)
        for D in (date(2025, 12, 22), date(2026, 5, 6), date(2026, 9, 30), date(2026, 10, 9)):
            st = full.step(D)
            T = full.cal.next_cn(D)
            cut = replace(
                md,
                nav={k: v for k, v in md.nav.items() if k <= st.Dp},
                nav4={k: v for k, v in md.nav4.items() if k <= st.Dp},
                etf={k: v for k, v in md.etf.items() if k <= T},
                fx={k: v for k, v in md.fx.items() if k <= T},
                index={k: v for k, v in md.index.items() if k <= st.s1},
                jisilu={},
            )
            a, b = full.estimate(D), IndexEstimator(cut).estimate(D)
            self.assertEqual(a.nav, b.nav, D)
            self.assertEqual(a.exposure, b.exposure, D)

    def test_holiday_gap(self):
        """国庆后首个交易日，参考净值日之后美股又交易了 5 天，公允价值要把这几天补上。"""
        row = next(r for r in self.rows if r.date == date(2026, 10, 8))
        self.assertEqual(row.nav_date, date(2026, 9, 30))
        self.assertEqual(row.gap_sessions, 5)
        self.assertNotAlmostEqual(row.fair_value, row.nav_est, places=3)


class TestSourceParsers(unittest.TestCase):
    """新数据源的解析与取价规则（不联网）。"""

    def test_b3_parse(self):
        import io
        import zipfile

        from etfprem.sources import b3

        chars = [" "] * 245
        for start, text in ((0, "01"), (2, "20261015"), (12, "BOVB11".ljust(12)), (24, "010"),
                            (108, "0000000016404"), (147, "00229"), (210, "0000001")):  # 记录类型、日期、代码、市场、收盘、笔数、报价因子
            chars[start:start + len(text)] = text
        line = "".join(chars)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("COTAHIST.TXT", line + "\n")
        self.assertEqual(b3.parse(buf.getvalue(), {"BOVB11"}), {"BOVB11": {date(2026, 10, 15): 164.04}})

    def test_bcb_rules(self):
        from etfprem.sources import bcb

        day = [("10:07:20", 5.38), ("11:07:19", 5.36), ("12:07:18", 5.37), ("13:07:22", 5.39)]
        self.assertEqual(bcb.rate_at(day, "11:00"), 5.36)
        self.assertIsNone(bcb.rate_at(day[:1], "13:00"))  # 半日市取不到 13 点
        self.assertEqual(bcb.london_4pm_brt(date(2026, 7, 1)), "12:00")  # 英国夏令时
        self.assertEqual(bcb.london_4pm_brt(date(2026, 1, 15)), "13:00")

    def test_fx_pair_names(self):
        from etfprem.config import pair_currency

        self.assertEqual(pair_currency("USD/CNY@16"), ("USD", 1.0))
        self.assertEqual(pair_currency("BRL/CNY@BJ22"), ("BRL", 1.0))
        self.assertEqual(pair_currency("100JPY/CNY"), ("JPY", 100.0))
        self.assertEqual(store._fx_path("USD/CNY@16").name, "USD_CNY_16h.json")
        self.assertEqual(store._fx_path("BRL/CNY@LDN16").name, "BRL_CNY_LDN16.json")


ALL_FUNDS_OK = all((DATA / "nav" / f"{f}_eastmoney.json").exists() for f in ("513300", "159329", "159822", "159687"))


@unittest.skipUnless(ALL_FUNDS_OK, "缺少缓存，先运行 etfprem update --fund all")
class TestFundConventions(unittest.TestCase):
    """批量扩展时查出来的各基金估值口径。"""

    @staticmethod
    def _summary(fund_id: str) -> dict:
        md = store.load(get_fund(fund_id))
        return history.summarize(history.compute(md, date(2025, 10, 15), date(2026, 10, 15)))

    def test_huaxia_uses_4pm_reference_rate(self):
        """华夏用外汇交易中心 16:00 参考汇率，换回中间价误差会大十倍。"""
        self.assertLess(self._summary("513300")["nav_err_bp"]["mae"], 1.0)
        fund = replace(get_fund("513300"), fx_pairs=("USD/CNY",))
        md = store.load(fund)
        mae = history.summarize(history.compute(md, date(2025, 10, 15), date(2026, 10, 15)))["nav_err_bp"]["mae"]
        self.assertGreater(mae, 5)

    def test_feeder_uses_underlying_nav(self):
        """联接基金按被投资 ETF 的净值估值。"""
        self.assertLess(self._summary("159687")["nav_err_bp"]["mae"], 1.0)
        self.assertLess(self._summary("159329")["nav_err_bp"]["mae"], 1.0)

    def test_saudi_hk_holiday_lag(self):
        """2830 的净值下一个港股交易日才公布：重阳节前一天的涨跌进入 159329 第二天的净值。"""
        md = store.load(get_fund("159329"))
        est = IndexEstimator(md)
        self.assertEqual(est.proxy_day(date(2025, 10, 28)), date(2025, 10, 27))
        self.assertEqual(est.proxy_day(date(2025, 10, 29)), date(2025, 10, 28))
        self.assertEqual(est.proxy_day(date(2025, 10, 30)), date(2025, 10, 30))

    def test_missing_nav_days_are_chained(self):
        """159822 在美国、香港休市日不公布净值，后一天的估算从更早的公布净值接续，不缺行。"""
        s = self._summary("159822")
        self.assertEqual(s["days"], 243)
        self.assertLess(s["days_with_official"], s["days"])


class TestSplitCandidates(unittest.TestCase):
    """没有申购替代金额的清单，靠前后股数的整数比找拆股；小股数的取整跳变不算。"""

    def _pcf(self, d: date, qty: dict[str, int]) -> szse_pcf.PCF:
        holdings = {c: szse_pcf.Holding(c, q, "允许", 0.1, 0.0, "其他市场") for c, q in qty.items()}
        return szse_pcf.PCF("159518", d, None, None, None, None, None, 1_000_000, None, holdings)

    def test_candidates(self):
        days = [date(2026, 6, d) for d in (10, 11, 12, 15, 16)]
        qty = [
            {"APA": 115, "AR": 132, "TINY": 1, "REV": 40},
            {"APA": 116, "AR": 132, "TINY": 2, "REV": 40},  # TINY 1→2 是取整
            {"APA": 0, "AR": 132, "TINY": 2, "REV": 40},  # 拆股当天写成 0 股
            {"APA": 464, "AR": 133, "TINY": 2, "REV": 4},  # APA 1 拆 4，REV 10 合 1
            {"APA": 465, "AR": 133, "TINY": 2, "REV": 4},
        ]
        pcfs = {d: self._pcf(d, q) for d, q in zip(days, qty)}
        got = {(c, T, k) for c, _, T, k in store.split_candidates(pcfs)}
        self.assertEqual(got, {("APA", date(2026, 6, 15), 4.0), ("REV", date(2026, 6, 15), 0.1)})

    def test_rebalance_days(self):
        """超过三成成分股的股数同时变化的那天是调仓日，核对不了的拆股候选在这天按调仓处理。"""
        days = [date(2025, 12, d) for d in (19, 22, 23)]
        qty = [
            {"A": 100, "B": 50, "C": 30, "RNA": 60},
            {"A": 100, "B": 50, "C": 30, "RNA": 60},
            {"A": 140, "B": 35, "C": 30, "RNA": 31},  # 调仓：A、B 和 RNA 都变了
        ]
        pcfs = {d: self._pcf(d, q) for d, q in zip(days, qty)}
        self.assertEqual(store.rebalance_days(pcfs), {date(2025, 12, 23)})

    def test_ratio(self):
        self.assertEqual(store._is_split_ratio(4.003), 4.0)
        self.assertEqual(store._is_split_ratio(0.1001), 0.1)
        self.assertEqual(store._is_split_ratio(10.25), 10.0)  # NFLX 1 拆 10 与调仓同日，股数比 10.25
        self.assertEqual(store._is_split_ratio(1.5), 1.5)
        self.assertIsNone(store._is_split_ratio(1.2))
        self.assertIsNone(store._is_split_ratio(2.3))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 501225：定期报告持仓

from etfprem.estimator import ReportEstimator  # noqa: E402
from etfprem.sources import fund_reports  # noqa: E402

LOF = get_fund("501225")
Q2_PDF = DATA / "fund_reports" / "501225" / "AN202607201827154075.pdf"
HAS_LOF = (DATA / "fund_reports" / "501225" / "reports.json").exists() and (DATA / "cn_prices" / "159995.json").exists()


class TestFundReport(unittest.TestCase):
    def test_period_end(self):
        self.assertEqual(fund_reports.period_end("某基金2026年第2季度报告"), date(2026, 6, 30))
        self.assertEqual(fund_reports.period_end("某基金2026年中期报告"), date(2026, 6, 30))
        self.assertEqual(fund_reports.period_end("某基金2025年年度报告"), date(2025, 12, 31))
        self.assertIsNone(fund_reports.period_end("某基金招募说明书"))

    @unittest.skipUnless(Q2_PDF.exists(), "缺少 2026 年二季报 PDF")
    def test_parse_q2_holdings(self):
        pct, fv, unknown = fund_reports.parse_fund_holdings(fund_reports.pdf_text(Q2_PDF), LOF.assets)
        self.assertEqual(unknown, [])
        self.assertEqual(len(pct), 8)
        self.assertAlmostEqual(pct["PSI"], 0.1821)
        self.assertAlmostEqual(pct["159995"], 0.0774)
        self.assertAlmostEqual(pct["2644"], 0.0173)
        self.assertAlmostEqual(fv["SOXX"], 214581003.65)


@unittest.skipUnless(HAS_LOF, "缺少 501225 缓存，先运行 etfprem update --fund 501225")
class TestReportFund(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.md = store.load(LOF)
        cls.rows = history.compute(cls.md, date(2025, 10, 12), date(2026, 10, 12))
        cls.summary = history.summarize(cls.rows)

    def test_accuracy(self):
        nav = self.summary["nav_err_bp"]
        self.assertLess(nav["mae"], 4.0)
        self.assertLess(nav["max"], 25)

    def test_a_share_prices_adjusted(self):
        """三只 A 股 ETF 都做过 1 拆 2，复权后不应出现超过 ±21% 的单日跳变。"""
        for code in ("159995", "512760", "159560"):
            s = self.md.px[code]
            ks = sorted(s)
            worst = max(abs(s[b] / s[a] - 1) for a, b in zip(ks, ks[1:]))
            self.assertLess(worst, 0.21, code)

    def test_report_by_publish_date(self):
        est = ReportEstimator(self.md)
        self.assertEqual(est.report_for(date(2026, 7, 21)).period_end, date(2026, 3, 31))  # 二季报当天发布，当天不能用
        self.assertEqual(est.report_for(date(2026, 7, 22)).period_end, date(2026, 6, 30))

    def test_no_lookahead(self):
        md, full = self.md, ReportEstimator(self.md)
        for D in (date(2025, 11, 6), date(2026, 3, 27), date(2026, 7, 21), date(2026, 10, 8)):
            st = full.step(D)
            T = full.cal.next_cn(D)
            cut = replace(
                md,
                nav={k: v for k, v in md.nav.items() if k <= st.Dp},
                nav4={k: v for k, v in md.nav4.items() if k <= st.Dp},
                etf={k: v for k, v in md.etf.items() if k <= T},
                px={c: {k: v for k, v in s.items() if k <= D} for c, s in md.px.items()},
                fx_all={c: {k: v for k, v in s.items() if k <= T} for c, s in md.fx_all.items()},
                reports=[r for r in md.reports if r.publish_date < T],
                jisilu={},
            )
            a, b = full.estimate(D), ReportEstimator(cut).estimate(D)
            self.assertEqual(a.nav, b.nav, D)
            self.assertEqual(a.betas, b.betas, D)


# ---------------------------------------------------------------- 溢价率建模（需要 uv sync --extra analysis）

try:
    import numpy as np  # noqa: E402

    from etfprem import analysis  # noqa: E402

    HAS_ANALYSIS = (DATA / "index" / "NDXTMC.json").exists()
except ImportError:
    HAS_ANALYSIS = False


@unittest.skipUnless(HAS_ANALYSIS and HAS_DATA, "缺少可选依赖或长历史数据")
class TestPremiumModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.df = analysis.build_panel(FUND)

    def test_identity(self):
        """价格收益 = 净值收益 + 溢价变化，逐日精确成立。"""
        for h in (1, 5, 20):
            d = self.df[[f"rP{h}", f"rN{h}", f"dZ{h}"]].dropna()
            self.assertLess(float((d[f"rP{h}"] - d[f"rN{h}"] - d[f"dZ{h}"]).abs().max()), 1e-12)

    def test_excludes_halts_and_holiday_gaps(self):
        bad = self.df[(self.df["vol"] <= 0) | self.df["gap"]]
        self.assertTrue(bad["z"].isna().all())

    def test_structural_model(self):
        s = analysis.structural(self.df, oos_start=10_000)  # 只估参数，跳过样本外
        p = s["params"]
        self.assertTrue(0 < p["phi"] < 1)
        self.assertAlmostEqual(sum(s["dz_share"].values()), 1.0)
        f = {h: analysis.model_forecast(s["states"].T.to_numpy(), list(p.values()), h) for h in (1, 5)}
        for h, fc in f.items():  # 预测同样满足分解：价格 = 净值 + 溢价变化
            self.assertLess(float(np.nanmax(np.abs(fc["rP"] - fc["rN"] - fc["dZ"]))), 1e-12)
