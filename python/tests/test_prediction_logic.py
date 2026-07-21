from __future__ import annotations

import sqlite3
import statistics
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


ENGINE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_DIR))

from live_strike_monitor import (  # noqa: E402
    IntradayTrendRow,
    IntradaySignalResult,
    NumericToken,
    OCRWord,
    StrikeRow,
    analyze_market_window,
    apply_intraday_option_signal,
    build_timeframe_report,
    combine_timeframe_analyses,
    compare_snapshots,
    estimate_option_probabilities,
    estimate_support_resistance,
    extract_intraday_trend_rows,
    extract_latest_intraday_signals,
    find_previous_internal_snapshot,
    image_day_folder_name,
    infer_expiry,
    load_config,
    locate_option_chain_columns,
    parse_strike_rows,
    rank_timeframe_oi_movers,
    rank_strike_oi_movers,
    render_ascii_table,
    report_day_directory,
    selected_token_indexes_are_distinct,
    strike_oi_comparison_lines,
    telegram_report_chunks,
    telegram_image_destination,
    wilson_probability_interval,
)


def strike_row(
    strike: float,
    *,
    call_oi: float,
    put_oi: float,
    call_ltp: float,
    put_ltp: float,
    call_change_oi: float,
    put_change_oi: float,
    confidence: float = 90,
) -> StrikeRow:
    return StrikeRow(
        strike=strike,
        call_oi=call_oi,
        call_change_oi=call_change_oi,
        call_ltp=call_ltp,
        put_ltp=put_ltp,
        put_change_oi=put_change_oi,
        put_oi=put_oi,
        confidence=confidence,
        raw_text="test row",
        top=0,
    )


class PredictionLogicTests(unittest.TestCase):
    @staticmethod
    def ocr_word(
        text: str,
        center_x: float,
        center_y: float,
        width: int = 50,
        confidence: float = 95,
    ) -> OCRWord:
        return OCRWord(
            text=text,
            confidence=confidence,
            left=round(center_x - width / 2),
            top=round(center_y - 8),
            width=width,
            height=16,
        )

    @staticmethod
    def trend_row(
        time_text: str,
        call_value: float,
        put_value: float,
        price: float,
        vwap: float,
        option_signal: str = "SELL",
        vwap_signal: str = "BUY",
    ) -> IntradayTrendRow:
        return IntradayTrendRow(
            time_text=time_text,
            call_value=call_value,
            put_value=put_value,
            diff_value=put_value - call_value,
            pcr=put_value / call_value,
            option_signal=option_signal,
            price=price,
            vwap=vwap,
            vwap_signal=vwap_signal,
            confidence=95,
            math_valid=True,
            raw_text="synthetic validated row",
            top=0,
        )

    def test_latest_explicit_sell_signal_overrides_oi_prediction(self) -> None:
        signals = IntradaySignalResult(
            option_signal="SELL",
            option_confidence=96,
            vwap_signal="BUY",
            vwap_confidence=95,
        )

        direction, confidence, explanation = apply_intraday_option_signal(
            "BULLISH_BIAS",
            60,
            signals,
            {
                "prefer_intraday_option_signal": True,
                "minimum_intraday_signal_confidence": 70,
            },
        )

        self.assertEqual("BEARISH_BIAS", direction)
        self.assertEqual(90, confidence)
        self.assertIn("takes priority", explanation or "")

    def test_extracts_signals_from_latest_intraday_row(self) -> None:
        def word(
            text: str,
            left: int,
            top: int,
            width: int = 40,
            confidence: float = 95,
        ) -> OCRWord:
            return OCRWord(
                text=text,
                confidence=confidence,
                left=left,
                top=top,
                width=width,
                height=12,
            )

        words = [
            word("Time", 20, 100),
            word("Option", 600, 100),
            word("Signal", 650, 100),
            word("VWAP", 900, 100),
            word("Signal", 950, 100),
            word("11:00", 20, 140),
            word("SELL", 625, 140, confidence=96),
            word("BUY", 925, 140, confidence=94),
            word("10:45", 20, 180),
            word("BUY", 625, 180),
            word("SELL", 925, 180),
        ]

        signals = extract_latest_intraday_signals(words, image_width=1080)

        self.assertEqual("SELL", signals.option_signal)
        self.assertEqual(96, signals.option_confidence)
        self.assertEqual("BUY", signals.vwap_signal)

    def test_downside_premium_consensus_overrides_bullish_oi_noise(self) -> None:
        previous: dict[float, StrikeRow] = {}
        current: dict[float, StrikeRow] = {}

        for strike in (22000.0, 22100.0, 22200.0):
            previous[strike] = strike_row(
                strike,
                call_oi=10_000,
                put_oi=10_000,
                call_ltp=100,
                put_ltp=100,
                call_change_oi=1_000,
                put_change_oi=1_000,
            )
            current[strike] = strike_row(
                strike,
                call_oi=10_000,
                put_oi=10_500,
                call_ltp=90,
                put_ltp=110,
                call_change_oi=1_000,
                put_change_oi=1_500,
            )

        differences, direction, confidence, _ = compare_snapshots(
            previous,
            current,
        )

        self.assertEqual("BEARISH_BIAS", direction)
        self.assertEqual(90, confidence)
        self.assertTrue(all(item["premium_score"] < 0 for item in differences))
        self.assertTrue(all(item["oi_score"] > 0 for item in differences))
        self.assertTrue(all(item["row_score"] < 0 for item in differences))

    def test_low_confidence_rows_cannot_create_a_prediction(self) -> None:
        previous: dict[float, StrikeRow] = {}
        current: dict[float, StrikeRow] = {}

        for strike in (22000.0, 22100.0, 22200.0):
            previous[strike] = strike_row(
                strike,
                call_oi=10_000,
                put_oi=10_000,
                call_ltp=100,
                put_ltp=100,
                call_change_oi=1_000,
                put_change_oi=1_000,
                confidence=40,
            )
            current[strike] = strike_row(
                strike,
                call_oi=11_000,
                put_oi=9_000,
                call_ltp=80,
                put_ltp=120,
                call_change_oi=2_000,
                put_change_oi=0,
                confidence=40,
            )

        differences, direction, confidence, explanations = compare_snapshots(
            previous,
            current,
        )

        self.assertEqual([], differences)
        self.assertEqual("UNCONFIRMED", direction)
        self.assertEqual(0, confidence)
        self.assertTrue(any("low-confidence" in item for item in explanations))

    def test_two_tokens_cannot_fill_three_option_columns(self) -> None:
        tokens = [
            NumericToken(1, "1", 10, 90),
            NumericToken(2, "2", 20, 90),
        ]

        self.assertFalse(
            selected_token_indexes_are_distinct(tokens, [0, 1, -1])
        )

    def test_image_day_folder_uses_market_timezone(self) -> None:
        late_utc_post = datetime(
            2026,
            7,
            20,
            20,
            30,
            tzinfo=timezone.utc,
        )

        folder_name = image_day_folder_name(
            late_utc_post,
            {"snapshot_timezone_offset_minutes": 330},
        )

        self.assertEqual("2026-07-21", folder_name)

    def test_telegram_destination_keeps_utc_filename_in_market_day(self) -> None:
        late_utc_post = datetime(
            2026,
            7,
            20,
            20,
            30,
            tzinfo=timezone.utc,
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch(
                "live_strike_monitor.DOWNLOAD_DIR",
                Path(temporary_directory),
            ):
                destination = telegram_image_destination(
                    late_utc_post,
                    message_id=12345,
                    config={"snapshot_timezone_offset_minutes": 330},
                )

            self.assertTrue(destination.parent.is_dir())
            self.assertEqual("2026-07-21", destination.parent.name)
            self.assertEqual(
                "2026-07-20_20-30-00_message_12345",
                destination.name,
            )

    def test_option_chain_headers_map_values_to_correct_fields(self) -> None:
        words = [
            self.ocr_word("Change", 60, 100),
            self.ocr_word("LTP", 333, 100),
            self.ocr_word("OI(Lakhs)", 425, 100, width=75),
            self.ocr_word("Strike", 531, 100),
            self.ocr_word("OI(Lakhs)", 650, 100, width=75),
            self.ocr_word("LTP", 742, 100),
            self.ocr_word("Change", 982, 100),
        ]
        columns = locate_option_chain_columns(
            words,
            strike_x=531,
            header_y=92,
            image_width=1073,
        )
        data_row = [
            self.ocr_word("1755390", 60, 150),
            self.ocr_word("174.5", 333, 150),
            self.ocr_word("46.61", 425, 150),
            self.ocr_word("24100", 531, 150),
            self.ocr_word("167.1", 650, 150),
            self.ocr_word("32.19", 742, 150),
            self.ocr_word("6469190", 982, 150),
        ]

        parsed = parse_strike_rows(
            rows=[data_row],
            strike_x=531,
            header_y=100,
            image_width=1073,
            config=load_config(),
            column_positions=columns,
        )

        self.assertEqual(1, len(parsed))
        self.assertEqual(4_661_000, parsed[0].call_oi)
        self.assertEqual(1_755_390, parsed[0].call_change_oi)
        self.assertEqual(174.5, parsed[0].call_ltp)
        self.assertEqual(16_710_000, parsed[0].put_oi)
        self.assertEqual(32.19, parsed[0].put_ltp)
        self.assertEqual(6_469_190, parsed[0].put_change_oi)

    def test_intraday_parser_supports_signed_flow_and_displayed_pcr(self) -> None:
        words = [
            self.ocr_word("Intraday", 80, 50),
            self.ocr_word("Trend", 155, 50),
            self.ocr_word("Time", 50, 100),
            self.ocr_word("Call", 170, 100),
            self.ocr_word("Put", 300, 100),
            self.ocr_word("Diff", 430, 100),
            self.ocr_word("PCR", 550, 100),
            self.ocr_word("Option", 620, 100),
            self.ocr_word("Signal", 680, 100),
            self.ocr_word("Price", 780, 100),
            self.ocr_word("VWAP", 870, 100),
            self.ocr_word("Signal", 1020, 100),
            self.ocr_word("13:30", 50, 150),
            self.ocr_word("11,63,070", 170, 150, width=90),
            self.ocr_word("-6,39,210", 300, 150, width=90),
            self.ocr_word("0.35", 550, 150),
            self.ocr_word("SELL", 650, 150),
            self.ocr_word("24248", 780, 150),
            self.ocr_word("24188", 870, 150),
            self.ocr_word("BUY", 990, 150),
            self.ocr_word("13:15", 50, 190),
            self.ocr_word("6,08,75,100", 170, 190, width=90),
            self.ocr_word("3,71,94,365", 300, 190, width=90),
            self.ocr_word("0.81", 550, 190),
            self.ocr_word("SELL", 650, 190),
            self.ocr_word("24240", 780, 190),
            self.ocr_word("24190", 870, 190),
            self.ocr_word("BUY", 990, 190),
        ]

        rows = extract_intraday_trend_rows(
            words,
            image_width=1073,
            config=load_config(),
        )

        self.assertEqual(2, len(rows))
        self.assertAlmostEqual(
            -639_210 - 1_163_070,
            rows[0].diff_value,
        )
        self.assertEqual(0.35, rows[0].pcr)
        self.assertTrue(rows[0].math_valid)
        self.assertAlmostEqual(
            37_194_365 / 60_875_100,
            rows[1].pcr,
        )

    def test_bearish_balance_with_recovery_is_not_called_up(self) -> None:
        rows = [
            self.trend_row(
                "13:30", 60_875_100, 37_194_365, 24_248, 24_188
            ),
            self.trend_row(
                "13:15", 89_982_100, 30_370_210, 24_220, 24_190
            ),
            self.trend_row(
                "13:00", 92_679_015, 23_253_880, 24_190, 24_195
            ),
            self.trend_row(
                "12:45", 90_307_685, 38_366_445, 24_180, 24_190
            ),
        ]

        analysis = analyze_market_window(
            rows,
            strike_direction="BEARISH_BIAS",
            config=load_config(),
        )

        self.assertEqual("BEARISH", analysis.current_condition)
        self.assertEqual("BEARISH", analysis.latest_condition)
        self.assertEqual("BULLISH", analysis.momentum)
        self.assertEqual("BEARISH_WEAKENING", analysis.state)
        self.assertEqual("FLAT", analysis.predicted_label)
        self.assertAlmostEqual(
            statistics.mean(
                (90_307_685, 92_679_015, 89_982_100, 60_875_100)
            ),
            analysis.average_call or 0,
        )

    def test_incomplete_45_minute_window_is_unconfirmed(self) -> None:
        rows = [
            self.trend_row("13:30", 60_000, 40_000, 24_200, 24_190),
            self.trend_row("13:15", 65_000, 35_000, 24_190, 24_190),
            self.trend_row("13:00", 70_000, 30_000, 24_180, 24_190),
        ]

        analysis = analyze_market_window(
            rows,
            strike_direction="BEARISH_BIAS",
            config=load_config(),
        )

        self.assertEqual("UNCONFIRMED", analysis.state)
        self.assertEqual(0, analysis.confidence)

    def test_negative_diff_cannot_issue_up_forecast(self) -> None:
        rows = [
            self.trend_row(
                "13:45", 57_409_235, 47_546_330, 24_242, 24_190
            ),
            self.trend_row(
                "13:30", 60_875_100, 37_194_365, 24_248, 24_188
            ),
            self.trend_row(
                "13:15", 89_982_100, 30_370_210, 24_195, 24_183
            ),
            self.trend_row(
                "13:00", 92_679_015, 23_253_880, 24_170, 24_183
            ),
        ]

        analysis = analyze_market_window(
            rows,
            strike_direction="BEARISH_BIAS",
            config=load_config(),
        )

        self.assertLess(analysis.imbalance or 0, 0)
        self.assertEqual("BEARISH", analysis.current_condition)
        self.assertEqual("BEARISH", analysis.latest_condition)
        self.assertEqual("BULLISH", analysis.momentum)
        self.assertEqual("BEARISH_WEAKENING", analysis.state)
        self.assertEqual("FLAT", analysis.predicted_label)
        self.assertAlmostEqual(
            75_236_362.5,
            analysis.average_call or 0,
        )
        self.assertAlmostEqual(
            34_591_196.25,
            analysis.average_put or 0,
        )
        self.assertAlmostEqual(
            -40_645_166.25,
            analysis.average_diff or 0,
        )
        self.assertAlmostEqual(
            24_213.75,
            analysis.average_price or 0,
        )
        self.assertAlmostEqual(
            24_186,
            analysis.average_vwap or 0,
        )

    def test_one_bullish_outlier_does_not_override_bearish_average(self) -> None:
        rows = [
            self.trend_row("13:45", 10_000, 90_000, 24_250, 24_200),
            self.trend_row("13:30", 100_000, 10_000, 24_180, 24_200),
            self.trend_row("13:15", 100_000, 10_000, 24_170, 24_200),
            self.trend_row("13:00", 100_000, 10_000, 24_160, 24_200),
        ]

        analysis = analyze_market_window(
            rows,
            strike_direction="BULLISH_BIAS",
            config=load_config(),
        )

        self.assertEqual("BEARISH", analysis.current_condition)
        self.assertEqual("BULLISH", analysis.latest_condition)
        self.assertLess(analysis.average_diff or 0, 0)
        self.assertEqual("BEARISH_WEAKENING", analysis.state)
        self.assertEqual("FLAT", analysis.predicted_label)

    def test_15m_30m_45m_use_two_three_and_four_rows(self) -> None:
        rows = [
            self.trend_row("13:45", 57_000, 47_000, 24_242, 24_190),
            self.trend_row("13:30", 61_000, 37_000, 24_248, 24_188),
            self.trend_row("13:15", 90_000, 30_000, 24_195, 24_183),
            self.trend_row("13:00", 93_000, 23_000, 24_170, 24_183),
        ]
        config = load_config()

        analyses = {
            minutes: analyze_market_window(
                rows,
                strike_direction="BEARISH_BIAS",
                config=config,
                horizon_minutes=minutes,
            )
            for minutes in (15, 30, 45)
        }

        self.assertEqual(2, len(analyses[15].window_times))
        self.assertEqual(3, len(analyses[30].window_times))
        self.assertEqual(4, len(analyses[45].window_times))
        self.assertEqual(59_000, analyses[15].average_call)
        self.assertAlmostEqual(
            69_333.33333333333,
            analyses[30].average_call or 0,
        )
        self.assertEqual(75_250, analyses[45].average_call)

        combined = combine_timeframe_analyses(analyses, config)
        self.assertEqual("BEARISH", combined.current_condition)
        self.assertEqual("FLAT", combined.predicted_label)
        self.assertIn("Timeframe agreement", combined.reasons[0])

    def test_strike_movers_and_oi_levels_are_ranked(self) -> None:
        rows = {
            100.0: strike_row(
                100,
                call_oi=400,
                put_oi=1_000,
                call_ltp=10,
                put_ltp=20,
                call_change_oi=20,
                put_change_oi=-50,
            ),
            110.0: strike_row(
                110,
                call_oi=600,
                put_oi=800,
                call_ltp=15,
                put_ltp=15,
                call_change_oi=50,
                put_change_oi=200,
            ),
            120.0: strike_row(
                120,
                call_oi=1_500,
                put_oi=300,
                call_ltp=20,
                put_ltp=10,
                call_change_oi=300,
                put_change_oi=10,
            ),
        }
        differences = [
            {
                "strike": 120.0,
                "call_oi_delta": 250,
                "put_oi_delta": 10,
            },
            {
                "strike": 110.0,
                "call_oi_delta": 20,
                "put_oi_delta": 180,
            },
        ]

        movers = rank_strike_oi_movers(
            rows.values(),
            differences,
            maximum_rows=3,
        )
        levels = estimate_support_resistance(
            rows.values(),
            current_price=111,
        )

        self.assertEqual(120, movers[0]["strike"])
        self.assertIn("resistance", movers[0]["interpretation"])
        self.assertEqual(100, levels.primary_support)
        self.assertEqual(110, levels.developing_support)
        self.assertEqual(100, levels.weakening_support)
        self.assertEqual(120, levels.primary_resistance)
        self.assertEqual(120, levels.developing_resistance)
        self.assertGreater(levels.confidence, 0)

    def test_two_sided_oi_buildup_is_reported_as_comparison(self) -> None:
        rows = [
            strike_row(
                24_150,
                call_oi=50_000_000,
                put_oi=40_000_000,
                call_ltp=10,
                put_ltp=20,
                call_change_oi=32_300_000,
                put_change_oi=28_900_000,
            ),
        ]
        differences = [
            {
                "strike": 24_150.0,
                "call_oi_delta": 1_500_000,
                "put_oi_delta": 2_000_000,
            },
        ]

        movers = rank_strike_oi_movers(
            rows,
            differences,
            maximum_rows=15,
        )
        report_lines = strike_oi_comparison_lines(
            movers,
            15,
            "13:45 IST",
            "13:30 IST",
        )
        report = "\n".join(report_lines)

        self.assertEqual(61_200_000, movers[0]["oi_activity"])
        self.assertEqual(-3_400_000, movers[0]["net_put_minus_call"])
        self.assertEqual(
            "TWO-SIDED (CALL TILT)",
            movers[0]["dominance"],
        )
        self.assertIn("up to 15", report)
        self.assertIn("Two-sided OI buildup", report)
        self.assertIn("[13:45 IST] Strike 24150", report)
        self.assertIn("[13:30 IST -> 13:45 IST] Total OI change", report)

    def test_long_telegram_report_is_split_without_truncation(self) -> None:
        report = "\n".join(f"report line {index:03d}" for index in range(400))

        chunks = telegram_report_chunks(report, maximum_length=500)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 500 for chunk in chunks))
        self.assertEqual(report, "\n".join(chunks))

    def test_report_directory_uses_market_date(self) -> None:
        config = load_config()
        with tempfile.TemporaryDirectory() as temp_directory:
            with patch(
                "live_strike_monitor.REPORT_DIR",
                Path(temp_directory),
            ):
                directory = report_day_directory(
                    "2026-07-20T20:00:00+00:00",
                    config,
                )

        self.assertEqual("2026-07-21", directory.name)

    def test_detailed_timeframe_report_contains_full_analysis(self) -> None:
        rows = [
            self.trend_row("13:45", 57_000, 47_000, 24_242, 24_190),
            self.trend_row("13:30", 61_000, 37_000, 24_248, 24_188),
            self.trend_row("13:15", 90_000, 30_000, 24_195, 24_183),
            self.trend_row("13:00", 93_000, 23_000, 24_170, 24_183),
        ]
        config = load_config()
        analysis = analyze_market_window(
            rows,
            strike_direction="BEARISH_BIAS",
            config=config,
            horizon_minutes=30,
        )
        strikes = {
            24_000.0: strike_row(
                24_000,
                call_oi=400_000,
                put_oi=1_000_000,
                call_ltp=10,
                put_ltp=20,
                call_change_oi=20_000,
                put_change_oi=50_000,
            ),
            24_500.0: strike_row(
                24_500,
                call_oi=1_500_000,
                put_oi=300_000,
                call_ltp=20,
                put_ltp=10,
                call_change_oi=300_000,
                put_change_oi=10_000,
            ),
        }

        report = build_timeframe_report(
            snapshot_id=7,
            captured_at="2026-07-21T13:45:00+05:30",
            instrument="NIFTY",
            current_rows=strikes,
            differences=[],
            analysis=analysis,
            intraday_trend_rows=rows,
            comparison_status="BASELINE",
            previous_snapshot=None,
            comparison_seconds=None,
            config=config,
        )

        self.assertIn("NIFTY 30-MINUTE DETAILED ANALYSIS", report)
        self.assertIn("Validated rows: 3/3", report)
        self.assertIn("30-minute averages:", report)
        self.assertIn("Composite components:", report)
        self.assertIn("30-MINUTE STRIKE TOTAL-OI CHANGE RANKING", report)
        self.assertIn("30-MINUTE OPTION PREMIUM PROBABILITY", report)
        self.assertIn("+------", report)
        self.assertIn("Estimated OI support and resistance:", report)
        self.assertIn("Strike-table confirmation:", report)
        self.assertIn("Plain-language conclusion:", report)

    def test_ascii_table_has_borders_pipes_and_wraps_long_cells(self) -> None:
        table = render_ascii_table(
            ["Strike", "Interpretation"],
            [[24_150, "Call buildup with Put unwinding and weaker support"]],
            [8, 18],
        )

        self.assertTrue(table[0].startswith("+"))
        self.assertTrue(all(line.startswith(("+", "|")) for line in table))
        self.assertTrue(any("| Strike" in line for line in table))
        self.assertGreater(len(table), 5)

    def test_timeframe_oi_ranking_uses_endpoint_total_oi_deltas(self) -> None:
        previous = {
            24_150.0: strike_row(
                24_150,
                call_oi=100,
                put_oi=100,
                call_ltp=10,
                put_ltp=10,
                call_change_oi=999,
                put_change_oi=999,
            ),
            24_500.0: strike_row(
                24_500,
                call_oi=100,
                put_oi=100,
                call_ltp=10,
                put_ltp=10,
                call_change_oi=999,
                put_change_oi=999,
            ),
        }
        current = {
            24_150.0: replace(
                previous[24_150.0],
                call_oi=150,
                put_oi=150,
            ),
            24_500.0: replace(
                previous[24_500.0],
                call_oi=300,
                put_oi=50,
            ),
        }

        movers, common_count = rank_timeframe_oi_movers(
            current,
            previous,
            15,
        )

        self.assertEqual(2, common_count)
        self.assertEqual(24_500, movers[0]["strike"])
        self.assertEqual(200, movers[0]["call_change_oi"])
        self.assertEqual(-50, movers[0]["put_change_oi"])
        self.assertEqual("CALL BUILD / PUT UNWIND", movers[0]["dominance"])

    def test_option_probability_requires_history_and_uses_wilson_range(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE option_outcomes (
                instrument TEXT,
                horizon_minutes INTEGER,
                option_type TEXT,
                moneyness TEXT,
                market_condition TEXT,
                entry_time TEXT,
                strike REAL,
                buy_net_return_pct REAL,
                sell_net_return_pct REAL
            )
            """
        )
        history = [
            (
                "NIFTY", 15, option_type, "ATM", "BULLISH",
                f"2026-07-{day:02d}T10:00:00+05:30", 24_200,
                2.0 if day <= 48 else -2.0,
                2.0 if day <= 48 else -2.0,
            )
            for option_type in ("CALL", "PUT")
            for day in range(1, 61)
        ]
        connection.executemany(
            """
            INSERT INTO option_outcomes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            history,
        )
        trend_rows = [
            self.trend_row("10:00", 50_000, 70_000, 24_200, 24_190),
            self.trend_row("10:15", 45_000, 80_000, 24_210, 24_195),
        ]
        analysis = analyze_market_window(
            trend_rows,
            strike_direction="BULLISH_BIAS",
            config=load_config(),
            horizon_minutes=15,
        )
        analysis = replace(
            analysis,
            state="BULLISH",
            predicted_label="UP",
            current_condition="BULLISH",
            score=0.8,
            current_price=24_200,
        )
        current = {
            24_200.0: strike_row(
                24_200,
                call_oi=100,
                put_oi=100,
                call_ltp=100,
                put_ltp=100,
                call_change_oi=10,
                put_change_oi=10,
            )
        }

        results = estimate_option_probabilities(
            connection,
            "NIFTY",
            current,
            analysis,
            load_config(),
        )
        low, high = wilson_probability_interval(48, 60)

        self.assertEqual(2, len(results))
        self.assertTrue(all(result.sample_count == 60 for result in results))
        self.assertTrue(all(result.win_probability == 0.8 for result in results))
        self.assertLess(low, 0.8)
        self.assertGreater(high, 0.8)
        self.assertTrue(all("CANDIDATE" in result.model_signal for result in results))
        connection.close()

    def test_expiry_is_normalized(self) -> None:
        self.assertEqual("2026-07-23", infer_expiry("NIFTY EXP 23 JUL 2026"))
        self.assertEqual("2026-07-23", infer_expiry("NIFTY 23/07/2026"))

    def test_previous_snapshot_must_have_the_same_expiry(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE snapshots (
                id INTEGER PRIMARY KEY,
                instrument TEXT,
                expiry TEXT,
                captured_at TEXT,
                row_count INTEGER
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO snapshots (
                id, instrument, expiry, captured_at, row_count
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    "NIFTY",
                    "2026-07-23",
                    "2026-07-20T10:00:00+05:30",
                    5,
                ),
                (
                    2,
                    "NIFTY",
                    "2026-07-30",
                    "2026-07-20T10:15:00+05:30",
                    5,
                ),
            ],
        )

        selected, elapsed, status = find_previous_internal_snapshot(
            connection=connection,
            current_snapshot_id=3,
            instrument="NIFTY",
            expiry="2026-07-23",
            current_captured_at="2026-07-20T10:30:00+05:30",
        )

        self.assertIsNotNone(selected)
        self.assertEqual(1, selected["id"])
        self.assertEqual(30 * 60, elapsed)
        self.assertEqual("COMPARE", status)
        connection.close()


if __name__ == "__main__":
    unittest.main()
