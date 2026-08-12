from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import statistics
import textwrap
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import cv2
import pytesseract
from dotenv import load_dotenv
from pytesseract import Output
from telethon import TelegramClient, errors, events


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

load_dotenv()

PROJECT_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = PROJECT_DIR / "downloaded_images"
REPORT_DIR = PROJECT_DIR / "reports"
DEBUG_DIR = PROJECT_DIR / "debug_ocr"
DATABASE_PATH = PROJECT_DIR / "market_snapshots.sqlite3"
CONFIG_PATH = PROJECT_DIR / "monitor_config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "channel": "autotrendpcrr",
    "report_target": "me",
    "send_report_to_telegram": True,
    "minimum_ocr_confidence": 28,
    "ocr_scale": 2.5,
    "strike_min": 1000,
    "strike_max": 100000,
    "strike_x_ratio_fallback": 0.50,
    "strike_x_tolerance_ratio": 0.09,
    "row_vertical_tolerance_ratio": 0.65,
    "minimum_numbers_left": 4,
    "minimum_numbers_right": 4,
    "column_indexes": {
        "call_oi_left": -1,
        "call_change_oi_left": 0,
        "call_ltp_left": -2,
        "put_ltp_right": 1,
        "put_change_oi_right": -1,
        "put_oi_right": 0
    },
    "call_oi_multiplier": 100_000,
    "put_oi_multiplier": 100_000,
    "option_column_x_tolerance_ratio": 0.055,
    "max_report_rows": 10,
    "easy_summary_rows": 3,
    "minimum_summary_row_confidence": 50,
    "prediction_min_row_confidence": 55,
    "prediction_min_common_strikes": 3,
    "prediction_min_directional_rows": 2,
    "prediction_consensus_ratio": 0.67,
    "prediction_oi_change_ratio": 0.01,
    "prediction_ltp_change_ratio": 0.005,
    "prediction_ltp_change_absolute": 0.5,
    "prediction_premium_weight": 2,
    "prediction_oi_weight": 1,
    "prefer_intraday_option_signal": False,
    "minimum_intraday_signal_confidence": 70,
    "prediction_horizon_minutes": 45,
    "prediction_min_intraday_confidence": 60,
    "intraday_diff_tolerance_ratio": 0.01,
    "intraday_pcr_tolerance": 0.03,
    "prediction_balance_scale": 0.35,
    "prediction_pcr_momentum_scale": 0.35,
    "prediction_imbalance_momentum_scale": 0.40,
    "prediction_flow_momentum_scale": 1.0,
    "prediction_price_return_scale": 0.003,
    "prediction_vwap_spread_scale": 0.0015,
    "prediction_direction_threshold": 0.35,
    "prediction_momentum_threshold": 0.35,
    "prediction_neutral_move_bps": 10,
    "analysis_timeframes_minutes": [15, 30, 45],
    "timeframe_weights": {
        "15": 0.20,
        "30": 0.30,
        "45": 0.50,
    },
    "strike_movers_rows": 15,
    "option_probability_min_samples": 100,
    "option_probability_min_trading_days": 20,
    "option_probability_threshold": 0.60,
    "option_probability_lower_bound_threshold": 0.52,
    "option_probability_round_trip_cost_pct": 0.005,
    "option_probability_min_entry_ltp": 1.0,
    "option_candidate_min_entry_ltp": 10.0,
    "option_candidate_min_row_confidence": 70,
    "option_outcome_tolerance_seconds": 90,
    "option_probability_max_tail_loss_pct": 35.0,
    "option_probability_max_observed_loss_pct": 100.0,
    "option_max_risk_reward_ratio": 5.0,
    "option_ema_min_rows": 21,
    "option_require_volume_confirmation": True,
    "option_probability_max_rows": 8,
    "option_probability_score_threshold": 0.20,
    "composite_weights": {
        "flow_momentum": 0.35,
        "average_balance": 0.20,
        "price_vwap": 0.20,
        "option_signal": 0.10,
        "vwap_signal": 0.10,
        "strike_confirmation": 0.05,
    },
    "report_timezone_offset_minutes": 330,
    "report_timezone_label": "IST",
    "snapshot_timezone_offset_minutes": 330,
    "snapshot_market_start": "08:00",
    "snapshot_market_end": "16:30",
    "intraday_rows_to_read": 32,
    "intraday_expected_interval_minutes": 15,
    "require_snapshot_time": True,
    "process_existing_images_on_start": True,
    "supported_extensions": [".jpg", ".jpeg", ".png", ".webp", ".bmp"]
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
LOGGER = logging.getLogger("strike-monitor")


@dataclass
class OCRWord:
    text: str
    confidence: float
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def center_x(self) -> float:
        return self.left + self.width / 2

    @property
    def center_y(self) -> float:
        return self.top + self.height / 2


@dataclass
class NumericToken:
    value: float
    raw: str
    x: float
    confidence: float


@dataclass
class StrikeRow:
    strike: float
    call_oi: float | None
    call_change_oi: float | None
    call_ltp: float | None
    put_ltp: float | None
    put_change_oi: float | None
    put_oi: float | None
    confidence: float
    raw_text: str
    top: float


@dataclass
class SnapshotResult:
    snapshot_id: int
    source_key: str
    image_path: str
    captured_at: str
    instrument: str | None
    expiry: str | None
    strike_x: float
    rows: list[StrikeRow]
    report_text: str
    diff_csv_path: str | None


@dataclass
class IntradaySignalResult:
    option_signal: str | None
    option_confidence: float
    vwap_signal: str | None
    vwap_confidence: float


@dataclass
class IntradayTrendRow:
    time_text: str
    call_value: float
    put_value: float
    diff_value: float
    pcr: float
    option_signal: str | None
    price: float | None
    vwap: float | None
    vwap_signal: str | None
    confidence: float
    math_valid: bool
    raw_text: str
    top: float


@dataclass
class MarketAnalysis:
    state: str
    score: float
    confidence: int
    current_condition: str
    latest_condition: str
    momentum: str
    predicted_label: str
    horizon_minutes: int
    current_price: float | None
    current_pcr: float | None
    average_call: float | None
    average_put: float | None
    average_diff: float | None
    average_pcr: float | None
    average_price: float | None
    average_vwap: float | None
    pcr_change: float | None
    call_change_pct: float | None
    put_change_pct: float | None
    diff_change: float | None
    imbalance: float | None
    latest_imbalance: float | None
    imbalance_change: float | None
    reasons: list[str]
    component_scores: dict[str, float]
    window_times: list[str]


@dataclass
class SupportResistanceEstimate:
    primary_support: float | None
    developing_support: float | None
    weakening_support: float | None
    primary_resistance: float | None
    developing_resistance: float | None
    weakening_resistance: float | None
    confidence: int
    notes: list[str]


@dataclass
class TimeframeStrikeComparison:
    horizon_minutes: int
    current_time: str
    previous_time: str | None
    movers: list[dict[str, Any]]
    current_strike_count: int
    common_strike_count: int
    status: str


@dataclass
class OptionProbabilityResult:
    horizon_minutes: int
    strike: float
    option_type: str
    entry_ltp: float
    moneyness: str
    evaluated_action: str
    model_signal: str
    win_probability: float | None
    probability_low: float | None
    probability_high: float | None
    sample_count: int
    trading_day_count: int
    average_win_pct: float | None
    average_loss_pct: float | None
    expected_value_pct: float | None
    worst_loss_pct: float | None
    tail_loss_pct: float | None
    ema9: float | None
    ema21: float | None
    hedge_strike: float | None
    hedge_ltp: float | None
    spread_credit: float | None
    maximum_loss: float | None
    risk_reward_ratio: float | None
    data_quality: str
    gate_statuses: dict[str, str]
    gate_details: dict[str, str]
    reason: str


# -----------------------------------------------------------------------------
# Setup helpers
# -----------------------------------------------------------------------------


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(DEFAULT_CONFIG, indent=2),
            encoding="utf-8",
        )
        LOGGER.info("Created default configuration: %s", CONFIG_PATH)
        return DEFAULT_CONFIG.copy()

    user_config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    merged = DEFAULT_CONFIG.copy()
    merged.update(user_config)

    merged_indexes = DEFAULT_CONFIG["column_indexes"].copy()
    merged_indexes.update(user_config.get("column_indexes", {}))
    merged["column_indexes"] = merged_indexes

    merged_weights = DEFAULT_CONFIG["composite_weights"].copy()
    merged_weights.update(user_config.get("composite_weights", {}))
    merged["composite_weights"] = merged_weights

    merged_timeframe_weights = DEFAULT_CONFIG["timeframe_weights"].copy()
    merged_timeframe_weights.update(
        user_config.get("timeframe_weights", {})
    )
    merged["timeframe_weights"] = merged_timeframe_weights
    return merged


def configure_tesseract() -> None:
    detected = shutil.which("tesseract")
    if detected:
        pytesseract.pytesseract.tesseract_cmd = detected
        LOGGER.info("Tesseract: %s", detected)
        return

    bundled_path = PROJECT_DIR / "tesseract" / "tesseract.exe"
    if bundled_path.exists():
        pytesseract.pytesseract.tesseract_cmd = str(bundled_path)
        os.environ.setdefault("TESSDATA_PREFIX", str(bundled_path.parent / "tessdata"))
        LOGGER.info("Tesseract: %s", bundled_path)
        return

    default_path = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if default_path.exists():
        pytesseract.pytesseract.tesseract_cmd = str(default_path)
        LOGGER.info("Tesseract: %s", default_path)
        return

    raise RuntimeError("Tesseract executable was not found.")


def ensure_directories() -> None:
    for directory in (DOWNLOAD_DIR, REPORT_DIR, DEBUG_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def connect_database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_key TEXT NOT NULL UNIQUE,
            message_id INTEGER,
            channel TEXT,
            captured_at TEXT NOT NULL,
            image_path TEXT NOT NULL,
            instrument TEXT,
            expiry TEXT,
            strike_x REAL,
            row_count INTEGER NOT NULL,
            raw_ocr_text TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS strike_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            strike REAL NOT NULL,
            call_oi REAL,
            call_change_oi REAL,
            call_ltp REAL,
            put_ltp REAL,
            put_change_oi REAL,
            put_oi REAL,
            confidence REAL,
            raw_text TEXT,
            top_position REAL,
            UNIQUE(snapshot_id, strike),
            FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS intraday_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            row_time TEXT NOT NULL,
            call_value REAL NOT NULL,
            put_value REAL NOT NULL,
            diff_value REAL NOT NULL,
            pcr REAL NOT NULL,
            option_signal TEXT,
            price REAL,
            vwap REAL,
            vwap_signal TEXT,
            confidence REAL,
            math_valid INTEGER NOT NULL,
            raw_text TEXT,
            top_position REAL,
            UNIQUE(snapshot_id, row_time),
            FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL UNIQUE,
            instrument TEXT NOT NULL,
            prediction_time TEXT NOT NULL,
            target_time TEXT NOT NULL,
            horizon_minutes INTEGER NOT NULL,
            state TEXT NOT NULL,
            predicted_label TEXT NOT NULL,
            score REAL NOT NULL,
            confidence INTEGER NOT NULL,
            current_price REAL,
            actual_price REAL,
            actual_return_bps REAL,
            actual_label TEXT,
            is_correct INTEGER,
            features_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            evaluated_at TEXT,
            FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS option_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            instrument TEXT NOT NULL,
            expiry TEXT,
            strike REAL NOT NULL,
            option_type TEXT NOT NULL,
            horizon_minutes INTEGER NOT NULL,
            entry_time TEXT NOT NULL,
            target_time TEXT NOT NULL,
            entry_ltp REAL NOT NULL,
            spot_price REAL NOT NULL,
            moneyness TEXT NOT NULL,
            market_condition TEXT NOT NULL,
            predicted_label TEXT NOT NULL,
            analysis_score REAL NOT NULL,
            analysis_confidence INTEGER NOT NULL,
            row_confidence REAL NOT NULL,
            exit_ltp REAL,
            exit_time TEXT,
            premium_return_pct REAL,
            buy_net_return_pct REAL,
            sell_net_return_pct REAL,
            created_at TEXT NOT NULL,
            evaluated_at TEXT,
            UNIQUE(snapshot_id, strike, option_type, horizon_minutes),
            FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
        )
        """
    )
    option_outcome_columns = {
        str(row["name"])
        for row in connection.execute(
            "PRAGMA table_info(option_outcomes)"
        ).fetchall()
    }
    if "exit_time" not in option_outcome_columns:
        connection.execute(
            "ALTER TABLE option_outcomes ADD COLUMN exit_time TEXT"
        )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_option_outcomes_calibration
        ON option_outcomes (
            instrument, horizon_minutes, option_type, moneyness,
            market_condition, evaluated_at
        )
        """
    )
    connection.commit()
    return connection


# -----------------------------------------------------------------------------
# OCR and layout reconstruction
# -----------------------------------------------------------------------------


def preprocess_image(image: Any, scale: float) -> Any:
    enlarged = cv2.resize(
        image,
        None,
        fx=scale,
        fy=scale,
        interpolation=cv2.INTER_CUBIC,
    )
    gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    blurred = cv2.GaussianBlur(enhanced, (0, 0), sigmaX=1.0)
    sharpened = cv2.addWeighted(enhanced, 1.8, blurred, -0.8, 0)
    return sharpened


def extract_words(processed: Any, scale: float, minimum_confidence: float) -> list[OCRWord]:
    data = pytesseract.image_to_data(
        processed,
        lang="eng",
        config="--oem 3 --psm 11 -c preserve_interword_spaces=1",
        output_type=Output.DICT,
    )

    words: list[OCRWord] = []
    for index, raw_text in enumerate(data["text"]):
        text = str(raw_text).strip()
        if not text:
            continue

        try:
            confidence = float(data["conf"][index])
        except (TypeError, ValueError):
            confidence = -1

        if confidence < minimum_confidence:
            continue

        words.append(
            OCRWord(
                text=text,
                confidence=confidence,
                left=round(int(data["left"][index]) / scale),
                top=round(int(data["top"][index]) / scale),
                width=max(1, round(int(data["width"][index]) / scale)),
                height=max(1, round(int(data["height"][index]) / scale)),
            )
        )
    return words


def group_words_into_rows(words: list[OCRWord], tolerance_ratio: float) -> list[list[OCRWord]]:
    if not words:
        return []

    median_height = statistics.median(word.height for word in words)
    tolerance = max(4.0, median_height * tolerance_ratio)
    rows: list[list[OCRWord]] = []

    for word in sorted(words, key=lambda item: (item.center_y, item.left)):
        best_row: list[OCRWord] | None = None
        best_distance: float | None = None

        for row in rows:
            row_center = statistics.mean(item.center_y for item in row)
            distance = abs(word.center_y - row_center)
            if distance <= tolerance and (best_distance is None or distance < best_distance):
                best_row = row
                best_distance = distance

        if best_row is None:
            rows.append([word])
        else:
            best_row.append(word)

    for row in rows:
        row.sort(key=lambda item: item.left)
    rows.sort(key=lambda row: statistics.mean(item.center_y for item in row))
    return rows


def normalize_ocr_text(text: str) -> str:
    replacements = str.maketrans({
        "O": "0",
        "o": "0",
        "I": "1",
        "l": "1",
        "|": "1",
        "S": "5",
    })
    return text.translate(replacements)


def header_key(text: str) -> str:
    """Normalize an OCR header without changing O/I/L into digits."""
    return re.sub(r"[^A-Z]", "", text.upper())


def parse_market_number(raw_text: str) -> float | None:
    text = normalize_ocr_text(raw_text.strip())
    text = text.replace("₹", "").replace("$", "").replace(",", "")
    text = text.replace("%", "")
    text = text.replace("−", "-").replace("–", "-")

    multiplier = 1.0
    suffix_match = re.search(r"(?i)(CR|CRORE|L|LAC|LAKH|K|M|MN|B)$", text)
    if suffix_match:
        suffix = suffix_match.group(1).upper()
        text = text[: suffix_match.start()]
        multiplier = {
            "K": 1_000.0,
            "L": 100_000.0,
            "LAC": 100_000.0,
            "LAKH": 100_000.0,
            "M": 1_000_000.0,
            "MN": 1_000_000.0,
            "CR": 10_000_000.0,
            "CRORE": 10_000_000.0,
            "B": 1_000_000_000.0,
        }[suffix]

    text = re.sub(r"[^0-9+\-.]", "", text)
    if not text or text in {"+", "-", ".", "+.", "-."}:
        return None
    if text.count(".") > 1:
        return None

    try:
        return float(text) * multiplier
    except ValueError:
        return None


def locate_strike_column(words: list[OCRWord], image_width: int, fallback_ratio: float) -> tuple[float, float | None]:
    strike_words = [word for word in words if "STRIKE" in word.text.upper()]
    if strike_words:
        return statistics.median(word.center_x for word in strike_words), min(word.top for word in strike_words)

    # Also inspect adjacent words such as "STRIKE PRICE" reconstructed as separate tokens.
    price_words = [word for word in words if word.text.upper() == "PRICE"]
    if price_words:
        center_candidates = [word.center_x for word in price_words if image_width * 0.30 <= word.center_x <= image_width * 0.70]
        if center_candidates:
            return statistics.median(center_candidates), min(word.top for word in price_words)

    return image_width * fallback_ratio, None


def locate_option_chain_columns(
    words: list[OCRWord],
    strike_x: float,
    header_y: float | None,
    image_width: int,
) -> dict[str, float]:
    """
    Locate the six option fields by their visible headers.

    The feed layout is:
      Change OI | % | LTP | OI (Lakhs) | Strike |
      OI (Lakhs) | LTP | % | Change OI
    """
    if header_y is None:
        return {}

    strike_headers = [
        word for word in words if header_key(word.text) == "STRIKE"
    ]
    if strike_headers:
        selected_strike = min(
            strike_headers,
            key=lambda word: abs(word.center_x - strike_x),
        )
        header_center_y = selected_strike.center_y
    else:
        header_center_y = header_y

    header_band = [
        word
        for word in words
        if abs(word.center_y - header_center_y)
        <= max(30.0, image_width * 0.025)
    ]
    ltp_headers = [
        word for word in header_band if header_key(word.text) == "LTP"
    ]
    oi_lakh_headers = [
        word
        for word in header_band
        if "LAKH" in header_key(word.text)
    ]
    change_headers = [
        word for word in header_band if header_key(word.text) == "CHANGE"
    ]

    if (
        len(ltp_headers) < 2
        or len(oi_lakh_headers) < 2
        or len(change_headers) < 2
    ):
        return {}

    ltp_headers.sort(key=lambda word: word.center_x)
    oi_lakh_headers.sort(key=lambda word: word.center_x)
    change_headers.sort(key=lambda word: word.center_x)
    return {
        "call_change_oi": change_headers[0].center_x,
        "call_ltp": ltp_headers[0].center_x,
        "call_oi": oi_lakh_headers[0].center_x,
        "put_oi": oi_lakh_headers[-1].center_x,
        "put_ltp": ltp_headers[-1].center_x,
        "put_change_oi": change_headers[-1].center_x,
    }


def row_to_numeric_tokens(row: list[OCRWord]) -> list[NumericToken]:
    tokens: list[NumericToken] = []
    for word in row:
        value = parse_market_number(word.text)
        if value is None:
            continue
        tokens.append(
            NumericToken(
                value=value,
                raw=word.text,
                x=word.center_x,
                confidence=word.confidence,
            )
        )
    return sorted(tokens, key=lambda item: item.x)


def get_by_index(tokens: list[NumericToken], index: int) -> float | None:
    try:
        return tokens[index].value
    except IndexError:
        return None


def get_confidence_by_index(tokens: list[NumericToken], index: int) -> float | None:
    try:
        return tokens[index].confidence
    except IndexError:
        return None


def resolved_token_index(tokens: list[NumericToken], index: int) -> int | None:
    """Resolve a positive or negative list index without allowing duplicates."""
    resolved = index if index >= 0 else len(tokens) + index
    if 0 <= resolved < len(tokens):
        return resolved
    return None


def selected_token_indexes_are_distinct(
    tokens: list[NumericToken],
    indexes: Iterable[int],
) -> bool:
    resolved = [resolved_token_index(tokens, index) for index in indexes]
    return (
        all(index is not None for index in resolved)
        and len(set(resolved)) == len(resolved)
    )


def parse_strike_rows(
    rows: list[list[OCRWord]],
    strike_x: float,
    header_y: float | None,
    image_width: int,
    config: dict[str, Any],
    column_positions: dict[str, float] | None = None,
) -> list[StrikeRow]:
    tolerance = image_width * float(config["strike_x_tolerance_ratio"])
    strike_min = float(config["strike_min"])
    strike_max = float(config["strike_max"])
    indexes = config["column_indexes"]

    parsed: dict[float, StrikeRow] = {}

    for row in rows:
        row_top = statistics.mean(word.center_y for word in row)
        if header_y is not None and row_top <= header_y:
            continue

        numeric_tokens = row_to_numeric_tokens(row)
        if len(numeric_tokens) < 3:
            continue

        candidates = [
            token
            for token in numeric_tokens
            if strike_min <= token.value <= strike_max and abs(token.x - strike_x) <= tolerance
        ]
        if not candidates:
            continue

        strike_token = min(candidates, key=lambda token: abs(token.x - strike_x))
        strike = strike_token.value
        left = [token for token in numeric_tokens if token.x < strike_token.x]
        right = [token for token in numeric_tokens if token.x > strike_token.x]

        confidence_values = [strike_token.confidence]
        positions = column_positions or {}
        if positions:
            field_tolerance = image_width * float(
                config.get("option_column_x_tolerance_ratio", 0.055)
            )

            def field_token(field_name: str) -> NumericToken | None:
                target_x = positions[field_name]
                selected = min(
                    numeric_tokens,
                    key=lambda token: abs(token.x - target_x),
                )
                if abs(selected.x - target_x) > field_tolerance:
                    return None
                return selected

            selected_fields = {
                field_name: field_token(field_name)
                for field_name in (
                    "call_oi",
                    "call_change_oi",
                    "call_ltp",
                    "put_ltp",
                    "put_change_oi",
                    "put_oi",
                )
            }
            if any(token is None for token in selected_fields.values()):
                continue
            field_indexes = [
                numeric_tokens.index(token)
                for token in selected_fields.values()
                if token is not None
            ]
            if len(set(field_indexes)) != len(field_indexes):
                continue
            for token in selected_fields.values():
                if token is not None:
                    confidence_values.append(token.confidence)
            call_oi = selected_fields["call_oi"].value
            call_change_oi = selected_fields["call_change_oi"].value
            call_ltp = selected_fields["call_ltp"].value
            put_ltp = selected_fields["put_ltp"].value
            put_change_oi = selected_fields["put_change_oi"].value
            put_oi = selected_fields["put_oi"].value
        else:
            if len(left) < int(config["minimum_numbers_left"]):
                continue
            if len(right) < int(config["minimum_numbers_right"]):
                continue

            left_indexes = [
                int(indexes["call_oi_left"]),
                int(indexes["call_change_oi_left"]),
                int(indexes["call_ltp_left"]),
            ]
            right_indexes = [
                int(indexes["put_ltp_right"]),
                int(indexes["put_change_oi_right"]),
                int(indexes["put_oi_right"]),
            ]
            if not selected_token_indexes_are_distinct(left, left_indexes):
                continue
            if not selected_token_indexes_are_distinct(right, right_indexes):
                continue

            for token_list, index_name in (
                (left, "call_oi_left"),
                (left, "call_change_oi_left"),
                (left, "call_ltp_left"),
                (right, "put_ltp_right"),
                (right, "put_change_oi_right"),
                (right, "put_oi_right"),
            ):
                confidence = get_confidence_by_index(
                    token_list,
                    int(indexes[index_name]),
                )
                if confidence is not None:
                    confidence_values.append(confidence)

            call_oi = get_by_index(left, int(indexes["call_oi_left"]))
            call_change_oi = get_by_index(
                left,
                int(indexes["call_change_oi_left"]),
            )
            call_ltp = get_by_index(left, int(indexes["call_ltp_left"]))
            put_ltp = get_by_index(right, int(indexes["put_ltp_right"]))
            put_change_oi = get_by_index(
                right,
                int(indexes["put_change_oi_right"]),
            )
            put_oi = get_by_index(right, int(indexes["put_oi_right"]))

        if call_oi is not None:
            call_oi *= float(config.get("call_oi_multiplier", 100_000))
        if put_oi is not None:
            put_oi *= float(config.get("put_oi_multiplier", 100_000))

        # Total OI and option prices cannot be negative. A negative value in
        # one of these fields means OCR positions shifted and the row is unsafe.
        if any(
            value is not None and value < 0
            for value in (call_oi, call_ltp, put_ltp, put_oi)
        ):
            continue

        parsed_row = StrikeRow(
            strike=strike,
            call_oi=call_oi,
            call_change_oi=call_change_oi,
            call_ltp=call_ltp,
            put_ltp=put_ltp,
            put_change_oi=put_change_oi,
            put_oi=put_oi,
            confidence=statistics.mean(confidence_values),
            raw_text=" | ".join(word.text for word in row),
            top=row_top,
        )

        # If OCR produces the same strike twice, keep the richer/higher-confidence row.
        existing = parsed.get(strike)
        if existing is None or parsed_row.confidence > existing.confidence:
            parsed[strike] = parsed_row

    return sorted(parsed.values(), key=lambda item: item.strike)


def infer_instrument(text: str) -> str | None:
    upper = re.sub(r"\s+", " ", text.upper())
    patterns = (
        ("BANKNIFTY", r"\bBANK\s*NIFTY\b|\bBANKNIFTY\b"),
        ("FINNIFTY", r"\bFIN\s*NIFTY\b|\bFINNIFTY\b"),
        ("MIDCPNIFTY", r"\bMIDCAP\s*NIFTY\b|\bMIDCPNIFTY\b"),
        ("NIFTY", r"\bNIFTY(?:\s*50)?\b"),
        ("SENSEX", r"\bSENSEX\b"),
    )
    for name, pattern in patterns:
        if re.search(pattern, upper):
            return name
    return None


def infer_expiry(text: str) -> str | None:
    patterns = (
        r"\b\d{1,2}[-/ ](?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[-/ ]\d{2,4}\b",
        r"\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text.upper())
        if match:
            raw_expiry = re.sub(r"[-/ ]+", "-", match.group(0).strip())
            for date_format in (
                "%d-%b-%Y",
                "%d-%b-%y",
                "%d-%m-%Y",
                "%d-%m-%y",
            ):
                try:
                    return datetime.strptime(
                        raw_expiry.title(),
                        date_format,
                    ).date().isoformat()
                except ValueError:
                    continue
            return raw_expiry
    return None


def parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_clock_setting(value: str, default_hour: int, default_minute: int) -> tuple[int, int]:
    try:
        hour_text, minute_text = value.strip().split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (AttributeError, TypeError, ValueError):
        pass
    return default_hour, default_minute


def extract_snapshot_date(text: str) -> datetime | None:
    """Read a calendar date printed inside the snapshot when one is present."""
    normalized = re.sub(r"\s+", " ", text.upper())

    patterns_and_formats = (
        (r"\b(\d{1,2}[-/ ](?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[-/ ]\d{2,4})\b",
         ("%d-%b-%Y", "%d-%b-%y", "%d/%b/%Y", "%d/%b/%y", "%d %b %Y", "%d %b %y")),
        (r"\b(\d{4}[-/]\d{1,2}[-/]\d{1,2})\b",
         ("%Y-%m-%d", "%Y/%m/%d")),
        (r"\b(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})\b",
         ("%d-%m-%Y", "%d-%m-%y", "%d/%m/%Y", "%d/%m/%y")),
    )

    for pattern, formats in patterns_and_formats:
        for match in re.finditer(pattern, normalized):
            raw = re.sub(r"\s+", " ", match.group(1).strip())
            for date_format in formats:
                try:
                    return datetime.strptime(raw.title(), date_format)
                except ValueError:
                    continue
    return None


def clock_candidate_to_seconds(
    hour_text: str,
    minute_text: str,
    second_text: str | None,
    meridiem: str | None,
) -> tuple[int, int, int] | None:
    try:
        hour = int(hour_text)
        minute = int(minute_text)
        second = int(second_text or "0")
    except ValueError:
        return None

    if not 0 <= minute <= 59 or not 0 <= second <= 59:
        return None

    meridiem_upper = (meridiem or "").upper().replace(".", "")
    if meridiem_upper:
        if not 1 <= hour <= 12:
            return None
        if meridiem_upper == "PM" and hour != 12:
            hour += 12
        elif meridiem_upper == "AM" and hour == 12:
            hour = 0
    elif not 0 <= hour <= 23:
        return None

    return hour, minute, second


def normalize_clock_word(value: str) -> str:
    """Normalize common OCR mistakes in a clock value."""
    normalized = value.upper().strip()
    normalized = normalized.replace("O", "0")
    normalized = normalized.replace("D", "0")
    normalized = normalized.replace("I", "1")
    normalized = normalized.replace("L", "1")
    normalized = normalized.replace(";", ":")
    normalized = normalized.replace(".", ":")
    normalized = normalized.replace("°", ":")
    normalized = normalized.replace("º", ":")
    return normalized


def parse_clock_word(value: str) -> tuple[int, int, int] | None:
    """Parse a single OCR word such as 11:00 or 10.45."""
    normalized = normalize_clock_word(value)
    match = re.search(
        r"(?<!\d)([0-2]?\d)\s*:\s*([0-5]\d)"
        r"(?:\s*:\s*([0-5]\d))?(?!\d)",
        normalized,
    )
    if not match:
        return None
    return clock_candidate_to_seconds(
        match.group(1),
        match.group(2),
        match.group(3),
        None,
    )


def intraday_time_candidates(
    words: list[OCRWord],
    image_width: int,
    config: dict[str, Any],
) -> list[tuple[int, int, int, str, float]]:
    """
    Read times only from the Intraday Trend -> Time column.

    The topmost time below the Time header is the latest market-data time.
    This deliberately ignores Telegram upload time, Windows file time, the
    computer clock, and unrelated clock values elsewhere in the snapshot.
    """
    if not words:
        return []

    def word_key(word: OCRWord) -> str:
        return re.sub(r"[^A-Z]", "", word.text.upper())

    intraday_words = [word for word in words if word_key(word) == "INTRADAY"]
    trend_words = [word for word in words if word_key(word) == "TREND"]

    trend_bottom: float | None = None
    for intraday_word in intraday_words:
        nearby = [
            trend_word
            for trend_word in trend_words
            if abs(trend_word.center_y - intraday_word.center_y)
            <= max(intraday_word.height, trend_word.height) * 1.5
        ]
        if nearby:
            selected_trend = min(
                nearby,
                key=lambda item: abs(item.center_y - intraday_word.center_y),
            )
            trend_bottom = max(intraday_word.bottom, selected_trend.bottom)
            break

    time_headers = [
        word
        for word in words
        if word_key(word) == "TIME"
        and word.center_x <= image_width * 0.45
    ]
    if trend_bottom is not None:
        preferred_headers = [
            word
            for word in time_headers
            if trend_bottom - 8 <= word.top <= trend_bottom + 150
        ]
        if preferred_headers:
            time_headers = preferred_headers

    if not time_headers:
        return []

    time_header = min(time_headers, key=lambda item: item.top)
    x_tolerance = max(36.0, image_width * 0.10)

    raw_candidates: list[tuple[int, int, int, str, float, float]] = []
    for word in words:
        if word.top <= time_header.bottom:
            continue
        if abs(word.center_x - time_header.center_x) > x_tolerance:
            continue

        parsed = parse_clock_word(word.text)
        if parsed is None:
            continue

        hour, minute, second = parsed
        raw_candidates.append(
            (hour, minute, second, word.text.strip(), word.confidence, word.top)
        )

    raw_candidates.sort(key=lambda item: item[5])

    # Keep one clock value per visual row.
    deduplicated: list[tuple[int, int, int, str, float, float]] = []
    for candidate in raw_candidates:
        if deduplicated and abs(candidate[5] - deduplicated[-1][5]) < 6:
            if candidate[4] > deduplicated[-1][4]:
                deduplicated[-1] = candidate
            continue
        deduplicated.append(candidate)

    maximum_rows = max(1, int(config.get("intraday_rows_to_read", 7)))
    deduplicated = deduplicated[:maximum_rows]
    if not deduplicated:
        return []

    expected_interval = max(
        1,
        int(config.get("intraday_expected_interval_minutes", 15)),
    ) * 60

    repaired: list[tuple[int, int, int, str, float]] = []
    previous_seconds: int | None = None

    for hour, minute, second, raw_value, confidence, _top in deduplicated:
        seconds = hour * 3600 + minute * 60 + second

        if previous_seconds is not None:
            expected_seconds = previous_seconds - expected_interval

            # Repair obvious OCR mistakes such as 09:30 being read as 03:30.
            expected_hour = expected_seconds // 3600
            expected_minute = (expected_seconds % 3600) // 60
            candidate_gap = previous_seconds - seconds

            if (
                minute == expected_minute
                and (
                    seconds >= previous_seconds
                    or abs(candidate_gap - expected_interval) > 5 * 60
                )
            ):
                hour = expected_hour
                minute = expected_minute
                second = 0
                seconds = expected_seconds
                raw_value = f"{raw_value} [OCR corrected]"

        repaired.append((hour, minute, second, raw_value, confidence))
        previous_seconds = seconds

    return repaired


def extract_latest_intraday_signals(
    words: list[OCRWord],
    image_width: int,
) -> IntradaySignalResult:
    """Read Option Signal and VWAP Signal from the latest Intraday Trend row."""

    empty = IntradaySignalResult(None, 0, None, 0)
    if not words:
        return empty

    def key(word: OCRWord) -> str:
        return re.sub(r"[^A-Z]", "", word.text.upper())

    signal_headers = [word for word in words if key(word) == "SIGNAL"]
    option_headers = [word for word in words if key(word) == "OPTION"]
    vwap_headers = [word for word in words if key(word) == "VWAP"]
    time_headers = [
        word
        for word in words
        if key(word) == "TIME" and word.center_x <= image_width * 0.45
    ]

    def paired_column_center(
        left_headers: list[OCRWord],
    ) -> tuple[float, float] | None:
        pairs: list[tuple[float, float, float]] = []
        for left_header in left_headers:
            for signal_header in signal_headers:
                vertical_tolerance = max(
                    left_header.height,
                    signal_header.height,
                ) * 1.5
                if (
                    signal_header.center_x > left_header.center_x
                    and abs(signal_header.center_y - left_header.center_y)
                    <= vertical_tolerance
                ):
                    distance = signal_header.center_x - left_header.center_x
                    pairs.append(
                        (
                            distance,
                            statistics.mean(
                                (
                                    left_header.center_x,
                                    signal_header.center_x,
                                )
                            ),
                            max(left_header.bottom, signal_header.bottom),
                        )
                    )
        if not pairs:
            return None
        _distance, center_x, header_bottom = min(
            pairs,
            key=lambda item: item[0],
        )
        return center_x, header_bottom

    option_column = paired_column_center(option_headers)
    vwap_column = paired_column_center(vwap_headers)
    if option_column is None or not time_headers:
        return empty

    option_x, option_header_bottom = option_column
    time_header = min(
        time_headers,
        key=lambda word: abs(word.center_y - option_header_bottom),
    )
    time_candidates = [
        word
        for word in words
        if word.top > time_header.bottom
        and abs(word.center_x - time_header.center_x)
        <= max(36.0, image_width * 0.10)
        and parse_clock_word(word.text) is not None
    ]
    if not time_candidates:
        return empty

    latest_time_word = min(time_candidates, key=lambda word: word.top)
    signal_values = [
        word
        for word in words
        if key(word) in {"BUY", "SELL"}
        and abs(word.center_y - latest_time_word.center_y)
        <= max(10.0, latest_time_word.height * 1.5)
    ]

    def read_column(
        column: tuple[float, float] | None,
    ) -> tuple[str | None, float]:
        if column is None:
            return None, 0
        center_x, _header_bottom = column
        candidates = [
            word
            for word in signal_values
            if abs(word.center_x - center_x)
            <= max(35.0, image_width * 0.06)
        ]
        if not candidates:
            return None, 0
        selected = min(
            candidates,
            key=lambda word: (
                abs(word.center_x - center_x),
                -word.confidence,
            ),
        )
        return key(selected), selected.confidence

    option_signal, option_confidence = read_column(option_column)
    vwap_signal, vwap_confidence = read_column(vwap_column)
    return IntradaySignalResult(
        option_signal=option_signal,
        option_confidence=option_confidence,
        vwap_signal=vwap_signal,
        vwap_confidence=vwap_confidence,
    )


def locate_intraday_columns(
    words: list[OCRWord],
    image_width: int,
) -> dict[str, float]:
    """Locate every Intraday Trend column from its visible header."""

    intraday_words = [
        word for word in words if header_key(word.text) == "INTRADAY"
    ]
    trend_words = [
        word for word in words if header_key(word.text) == "TREND"
    ]
    trend_bottom = 0.0
    for intraday_word in intraday_words:
        nearby = [
            word
            for word in trend_words
            if abs(word.center_y - intraday_word.center_y)
            <= max(word.height, intraday_word.height) * 1.5
        ]
        if nearby:
            trend_bottom = max(
                intraday_word.bottom,
                min(nearby, key=lambda word: word.left).bottom,
            )
            break

    time_headers = [
        word
        for word in words
        if header_key(word.text) == "TIME"
        and word.center_x <= image_width * 0.20
        and word.top >= trend_bottom
    ]
    if not time_headers:
        return {}
    time_header = min(time_headers, key=lambda word: word.top)
    header_center_y = time_header.center_y
    header_band = [
        word
        for word in words
        if abs(word.center_y - header_center_y)
        <= max(20.0, time_header.height * 1.8)
    ]

    def one_header(name: str) -> OCRWord | None:
        candidates = [
            word for word in header_band if header_key(word.text) == name
        ]
        return min(candidates, key=lambda word: word.left) if candidates else None

    call_header = one_header("CALL")
    put_header = one_header("PUT")
    diff_candidates = [
        word
        for word in header_band
        if header_key(word.text) in {"DIFF", "DIF", "DITF"}
    ]
    diff_header = (
        min(diff_candidates, key=lambda word: word.left)
        if diff_candidates
        else None
    )
    pcr_header = one_header("PCR")
    price_header = one_header("PRICE")
    if any(
        header is None
        for header in (
            call_header,
            put_header,
            diff_header,
            pcr_header,
            price_header,
        )
    ):
        return {}

    signal_headers = [
        word for word in header_band if header_key(word.text) == "SIGNAL"
    ]
    option_headers = [
        word for word in header_band if header_key(word.text) == "OPTION"
    ]
    vwap_headers = [
        word
        for word in header_band
        if (
            header_key(word.text) in {"VWAP", "WAP"}
            or (
                header_key(word.text).endswith("VAP")
                and len(header_key(word.text)) <= 6
            )
        )
    ]

    def nearest_right_pair(
        left_headers: list[OCRWord],
    ) -> tuple[OCRWord, OCRWord] | None:
        pairs = [
            (right.center_x - left.center_x, left, right)
            for left in left_headers
            for right in signal_headers
            if right.center_x > left.center_x
        ]
        if not pairs:
            return None
        _distance, left, right = min(pairs, key=lambda item: item[0])
        return left, right

    option_pair = nearest_right_pair(option_headers)
    if option_pair is None:
        return {}

    right_signal_headers = [
        word
        for word in signal_headers
        if word is not option_pair[1]
        and word.center_x > price_header.center_x
    ]
    if not right_signal_headers:
        return {}
    right_signal = max(
        right_signal_headers,
        key=lambda word: word.center_x,
    )
    vwap_headers_before_signal = sorted(
        (
            word
            for word in vwap_headers
            if price_header.center_x < word.center_x < right_signal.center_x
        ),
        key=lambda word: word.center_x,
    )
    inferred_vwap_x = (
        price_header.center_x
        + (right_signal.center_x - price_header.center_x) * 0.45
    )
    if len(vwap_headers_before_signal) >= 2:
        vwap_x = vwap_headers_before_signal[0].center_x
        vwap_signal_x = statistics.mean(
            (
                vwap_headers_before_signal[-1].center_x,
                right_signal.center_x,
            )
        )
    elif len(vwap_headers_before_signal) == 1:
        only_vwap = vwap_headers_before_signal[0]
        midpoint = statistics.mean(
            (price_header.center_x, right_signal.center_x)
        )
        if only_vwap.center_x < midpoint:
            vwap_x = only_vwap.center_x
            vwap_signal_x = right_signal.center_x
        else:
            vwap_x = inferred_vwap_x
            vwap_signal_x = statistics.mean(
                (only_vwap.center_x, right_signal.center_x)
            )
    else:
        vwap_x = inferred_vwap_x
        vwap_signal_x = right_signal.center_x

    return {
        "time": time_header.center_x,
        "call": call_header.center_x,
        "put": put_header.center_x,
        "diff": diff_header.center_x,
        "pcr": pcr_header.center_x,
        "option_signal": statistics.mean(
            (option_pair[0].center_x, option_pair[1].center_x)
        ),
        "price": price_header.center_x,
        "vwap": vwap_x,
        "vwap_signal": vwap_signal_x,
        "header_bottom": max(word.bottom for word in header_band),
    }


def extract_intraday_trend_rows(
    words: list[OCRWord],
    image_width: int,
    config: dict[str, Any],
    latest_time_text: str | None = None,
) -> list[IntradayTrendRow]:
    """Reconstruct and validate the complete Intraday Trend table."""

    columns = locate_intraday_columns(words, image_width)
    if not columns:
        return []

    time_words = [
        word
        for word in words
        if word.top > columns["header_bottom"]
        and abs(word.center_x - columns["time"])
        <= max(36.0, image_width * 0.08)
        and parse_clock_word(word.text) is not None
    ]
    time_words.sort(key=lambda word: word.top)

    deduplicated: list[OCRWord] = []
    for word in time_words:
        if deduplicated and abs(word.center_y - deduplicated[-1].center_y) < 6:
            if word.confidence > deduplicated[-1].confidence:
                deduplicated[-1] = word
            continue
        deduplicated.append(word)

    maximum_rows = max(4, int(config.get("intraday_rows_to_read", 7)))
    deduplicated = deduplicated[:maximum_rows]
    x_tolerance = max(36.0, image_width * 0.065)
    expected_interval_seconds = max(
        1,
        int(config.get("intraday_expected_interval_minutes", 15)),
    ) * 60

    # The rows are in visual newest-to-oldest order. Repair obvious OCR hour
    # errors (for example 10:30 read as 19:30) before any time-based sorting.
    repaired_clocks: dict[int, tuple[int, int, int]] = {}
    previous_seconds: int | None = None
    forced_latest_clock = (
        parse_clock_word(latest_time_text)
        if latest_time_text
        else None
    )
    for index, time_word in enumerate(deduplicated):
        parsed_clock = (
            forced_latest_clock
            if index == 0 and forced_latest_clock is not None
            else parse_clock_word(time_word.text)
        )
        if parsed_clock is None:
            continue
        hour, minute, second = parsed_clock
        seconds = hour * 3600 + minute * 60 + second
        if previous_seconds is not None:
            expected_seconds = previous_seconds - expected_interval_seconds
            expected_hour = expected_seconds // 3600
            expected_minute = (expected_seconds % 3600) // 60
            actual_gap = previous_seconds - seconds
            gap_remainder = (
                actual_gap % expected_interval_seconds
                if actual_gap > 0
                else expected_interval_seconds
            )
            gap_is_interval_multiple = (
                actual_gap > 0
                and min(
                    gap_remainder,
                    expected_interval_seconds - gap_remainder,
                ) <= 60
            )
            needs_repair = (
                seconds >= previous_seconds
                or not gap_is_interval_multiple
            )
            if needs_repair:
                if (
                    expected_seconds >= 0
                    and minute == expected_minute
                ):
                    hour = expected_hour
                    minute = expected_minute
                    second = 0
                    seconds = expected_seconds
                else:
                    # Do not let one malformed time reset the sequence and
                    # cascade into duplicate repaired clock rows.
                    continue
        repaired_clocks[id(time_word)] = (hour, minute, second)
        previous_seconds = seconds

    def word_for_column(
        row_words: list[OCRWord],
        name: str,
    ) -> OCRWord | None:
        candidates = [
            word
            for word in row_words
            if abs(word.center_x - columns[name]) <= x_tolerance
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda word: (
                abs(word.center_x - columns[name]),
                -word.confidence,
            ),
        )

    rows: list[IntradayTrendRow] = []
    diff_tolerance_ratio = float(
        config.get("intraday_diff_tolerance_ratio", 0.01)
    )
    pcr_tolerance = float(config.get("intraday_pcr_tolerance", 0.03))

    for time_word in deduplicated:
        y_tolerance = max(10.0, time_word.height * 1.6)
        row_words = [
            word
            for word in words
            if abs(word.center_y - time_word.center_y) <= y_tolerance
        ]
        selected = {
            name: word_for_column(row_words, name)
            for name in (
                "call",
                "put",
                "diff",
                "pcr",
                "option_signal",
                "price",
                "vwap",
                "vwap_signal",
            )
        }

        call_value = (
            parse_market_number(selected["call"].text)
            if selected["call"]
            else None
        )
        put_value = (
            parse_market_number(selected["put"].text)
            if selected["put"]
            else None
        )
        diff_value = (
            parse_market_number(selected["diff"].text)
            if selected["diff"]
            else None
        )
        displayed_pcr = (
            parse_market_number(selected["pcr"].text)
            if selected["pcr"]
            else None
        )
        if (
            call_value is None
            or put_value is None
            or (call_value == 0 and put_value == 0)
        ):
            continue

        calculated_pcr = (
            abs(put_value) / abs(call_value)
            if call_value
            else None
        )
        expected_diff = put_value - call_value
        diff_tolerance = max(
            1_000.0,
            abs(expected_diff) * diff_tolerance_ratio,
        )
        diff_valid = (
            diff_value is not None
            and abs(diff_value - expected_diff) <= diff_tolerance
        )
        displayed_pcr_valid = (
            displayed_pcr is not None
            and 0 <= displayed_pcr <= 5
        )
        ratio_comparable = call_value > 0 and put_value >= 0
        pcr_valid = (
            displayed_pcr_valid
            and (
                not ratio_comparable
                or calculated_pcr is None
                or abs(displayed_pcr - calculated_pcr) <= pcr_tolerance
            )
        )
        effective_pcr = (
            calculated_pcr
            if ratio_comparable and calculated_pcr is not None
            else (
                displayed_pcr
                if displayed_pcr_valid
                else calculated_pcr
            )
        )
        if effective_pcr is None:
            continue

        option_signal = (
            header_key(selected["option_signal"].text)
            if selected["option_signal"]
            and header_key(selected["option_signal"].text) in {"BUY", "SELL"}
            else None
        )
        vwap_signal = (
            header_key(selected["vwap_signal"].text)
            if selected["vwap_signal"]
            and header_key(selected["vwap_signal"].text) in {"BUY", "SELL"}
            else None
        )
        price = (
            parse_market_number(selected["price"].text)
            if selected["price"]
            else None
        )
        vwap = (
            parse_market_number(selected["vwap"].text)
            if selected["vwap"]
            else None
        )

        confidence_words = [
            time_word,
            *(
                word
                for word in selected.values()
                if word is not None
            ),
        ]
        parsed_clock = repaired_clocks.get(id(time_word))
        if parsed_clock is None:
            continue
        hour, minute, second = parsed_clock
        time_text = (
            f"{hour:02d}:{minute:02d}:{second:02d}"
            if second
            else f"{hour:02d}:{minute:02d}"
        )
        rows.append(
            IntradayTrendRow(
                time_text=time_text,
                call_value=call_value,
                put_value=put_value,
                # Diff is deterministic, so it can be recovered when its OCR
                # cell is missing. Positive rows also use recomputed Put/Call
                # PCR; signed flow rows use the feed's displayed PCR because
                # a signed Put/Call ratio is not a valid PCR.
                diff_value=expected_diff,
                pcr=effective_pcr,
                option_signal=option_signal,
                price=price,
                vwap=vwap,
                vwap_signal=vwap_signal,
                confidence=max(
                    0,
                    statistics.mean(
                        word.confidence for word in confidence_words
                    )
                    - (8 if not diff_valid else 0)
                    - (5 if not pcr_valid else 0),
                ),
                math_valid=True,
                raw_text=" | ".join(
                    word.text
                    for word in sorted(row_words, key=lambda word: word.left)
                ),
                top=time_word.center_y,
            )
        )

    unique_rows: list[IntradayTrendRow] = []
    seen_times: set[str] = set()
    for row in sorted(rows, key=lambda item: item.top):
        if row.time_text in seen_times:
            continue
        seen_times.add(row.time_text)
        unique_rows.append(row)
    return unique_rows


def clamp(value: float, minimum: float = -1.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def percentage_change(current: float, previous: float) -> float | None:
    if previous == 0:
        return None
    return (current - previous) / abs(previous)


def trend_row_seconds(row: IntradayTrendRow) -> int:
    parsed = parse_clock_word(row.time_text)
    if parsed is None:
        return -1
    return parsed[0] * 3600 + parsed[1] * 60 + parsed[2]


def select_intraday_window(
    rows: list[IntradayTrendRow],
    horizon_minutes: int,
    expected_interval_minutes: int,
    minimum_confidence: float,
) -> list[IntradayTrendRow]:
    reliable = [
        row
        for row in rows
        if row.math_valid and row.confidence >= minimum_confidence
    ]
    if not reliable:
        return []

    reliable.sort(key=trend_row_seconds, reverse=True)
    latest_seconds = trend_row_seconds(reliable[0])
    target_seconds = latest_seconds - horizon_minutes * 60
    start_candidates = [
        row
        for row in reliable
        if abs(trend_row_seconds(row) - target_seconds) <= 60
    ]
    if not start_candidates:
        return []
    start = min(
        start_candidates,
        key=lambda row: abs(trend_row_seconds(row) - target_seconds),
    )
    start_seconds = trend_row_seconds(start)
    window = [
        row
        for row in reliable
        if start_seconds <= trend_row_seconds(row) <= latest_seconds
    ]
    window.sort(key=trend_row_seconds)

    expected_rows = horizon_minutes // expected_interval_minutes + 1
    if len(window) < expected_rows:
        return []
    intervals = [
        trend_row_seconds(current) - trend_row_seconds(previous)
        for previous, current in zip(window, window[1:])
    ]
    expected_seconds = expected_interval_minutes * 60
    if any(abs(interval - expected_seconds) > 60 for interval in intervals):
        return []
    return window[-expected_rows:]


def analyze_market_window(
    rows: list[IntradayTrendRow],
    strike_direction: str,
    config: dict[str, Any],
    horizon_minutes: int | None = None,
) -> MarketAnalysis:
    """Combine one timeframe's averages, momentum, and confirmations."""

    if horizon_minutes is None:
        horizon_minutes = int(
            config.get("prediction_horizon_minutes", 45)
        )
    expected_interval = int(
        config.get("intraday_expected_interval_minutes", 15)
    )
    expected_rows = horizon_minutes // expected_interval + 1
    minimum_confidence = float(
        config.get("prediction_min_intraday_confidence", 60)
    )
    window = select_intraday_window(
        rows,
        horizon_minutes,
        expected_interval,
        minimum_confidence,
    )
    if not window:
        return MarketAnalysis(
            state="UNCONFIRMED",
            score=0,
            confidence=0,
            current_condition="UNKNOWN",
            latest_condition="UNKNOWN",
            momentum="UNKNOWN",
            predicted_label="FLAT",
            horizon_minutes=horizon_minutes,
            current_price=rows[0].price if rows else None,
            current_pcr=rows[0].pcr if rows else None,
            average_call=None,
            average_put=None,
            average_diff=None,
            average_pcr=None,
            average_price=None,
            average_vwap=None,
            pcr_change=None,
            call_change_pct=None,
            put_change_pct=None,
            diff_change=None,
            imbalance=None,
            latest_imbalance=None,
            imbalance_change=None,
            reasons=[
                f"A complete, validated {horizon_minutes}-minute "
                "Intraday Trend window was not available."
            ],
            component_scores={},
            window_times=[row.time_text for row in rows[:expected_rows]],
        )

    start = window[0]
    current = window[-1]

    def imbalance(row: IntradayTrendRow) -> float:
        total = abs(row.put_value) + abs(row.call_value)
        return row.diff_value / total if total else 0

    def optional_average(values: Iterable[float | None]) -> float | None:
        available = [value for value in values if value is not None]
        return statistics.mean(available) if available else None

    average_call = statistics.mean(row.call_value for row in window)
    average_put = statistics.mean(row.put_value for row in window)
    average_diff = statistics.mean(row.diff_value for row in window)
    average_pcr = statistics.mean(row.pcr for row in window)
    average_price = optional_average(row.price for row in window)
    average_vwap = optional_average(row.vwap for row in window)

    start_imbalance = imbalance(start)
    current_imbalance = imbalance(current)
    average_total = abs(average_put) + abs(average_call)
    average_imbalance = (
        average_diff / average_total if average_total else 0
    )
    pcr_change = current.pcr - start.pcr
    imbalance_change = current_imbalance - start_imbalance
    call_change_pct = percentage_change(current.call_value, start.call_value)
    put_change_pct = percentage_change(current.put_value, start.put_value)
    diff_change = current.diff_value - start.diff_value

    pcr_score = clamp(
        pcr_change
        / float(config.get("prediction_pcr_momentum_scale", 0.35))
    )
    imbalance_momentum_score = clamp(
        imbalance_change
        / float(config.get("prediction_imbalance_momentum_scale", 0.40))
    )
    flow_difference = (
        (put_change_pct or 0) - (call_change_pct or 0)
    )
    flow_score = clamp(
        flow_difference
        / float(config.get("prediction_flow_momentum_scale", 1.0))
    )
    flow_momentum_score = (
        pcr_score * 0.40
        + imbalance_momentum_score * 0.40
        + flow_score * 0.20
    )
    balance_score = clamp(
        average_imbalance
        / float(config.get("prediction_balance_scale", 0.35))
    )

    component_scores: dict[str, float] = {
        "flow_momentum": flow_momentum_score,
        "average_balance": balance_score,
    }

    price_step_returns = [
        (current_row.price - previous_row.price) / previous_row.price
        for previous_row, current_row in zip(window, window[1:])
        if (
            previous_row.price is not None
            and current_row.price is not None
            and previous_row.price > 0
        )
    ]
    if price_step_returns:
        # Average every 15-minute price move, then scale it to the complete
        # 45-minute window. This prevents the first/last pair from being the
        # only price observation used by the composite.
        price_return = (
            statistics.mean(price_step_returns)
            * (len(window) - 1)
        )
        price_return_score = clamp(
            price_return
            / float(config.get("prediction_price_return_scale", 0.003))
        )
        if (
            average_price is not None
            and average_vwap is not None
            and average_vwap > 0
        ):
            vwap_spread = (
                (average_price - average_vwap) / average_vwap
            )
            vwap_spread_score = clamp(
                vwap_spread
                / float(config.get("prediction_vwap_spread_scale", 0.0015))
            )
            component_scores["price_vwap"] = (
                price_return_score * 0.60 + vwap_spread_score * 0.40
            )
        else:
            component_scores["price_vwap"] = price_return_score

    if current.option_signal in {"BUY", "SELL"}:
        component_scores["option_signal"] = (
            1.0 if current.option_signal == "BUY" else -1.0
        )
    if current.vwap_signal in {"BUY", "SELL"}:
        component_scores["vwap_signal"] = (
            1.0 if current.vwap_signal == "BUY" else -1.0
        )
    if strike_direction in {"BULLISH_BIAS", "BEARISH_BIAS"}:
        component_scores["strike_confirmation"] = (
            1.0 if strike_direction == "BULLISH_BIAS" else -1.0
        )

    weights = config.get("composite_weights", DEFAULT_CONFIG["composite_weights"])
    available_weight = sum(
        float(weights.get(name, 0))
        for name in component_scores
    )
    score = (
        sum(
            component_scores[name] * float(weights.get(name, 0))
            for name in component_scores
        )
        / available_weight
        if available_weight
        else 0
    )
    score = clamp(score)

    neutral_band = 0.08
    if average_imbalance <= -neutral_band:
        current_condition = "BEARISH"
    elif average_imbalance >= neutral_band:
        current_condition = "BULLISH"
    else:
        current_condition = "BALANCED"
    if current_imbalance <= -neutral_band:
        latest_condition = "BEARISH"
    elif current_imbalance >= neutral_band:
        latest_condition = "BULLISH"
    else:
        latest_condition = "BALANCED"

    momentum_threshold = float(
        config.get("prediction_momentum_threshold", 0.35)
    )
    if flow_momentum_score >= momentum_threshold:
        momentum = "BULLISH"
    elif flow_momentum_score <= -momentum_threshold:
        momentum = "BEARISH"
    else:
        momentum = "MIXED"

    direction_threshold = float(
        config.get("prediction_direction_threshold", 0.35)
    )
    bullish_confirmations = sum(
        component_scores.get(name, 0) > 0.20
        for name in ("price_vwap", "option_signal", "vwap_signal")
    )
    bearish_confirmations = sum(
        component_scores.get(name, 0) < -0.20
        for name in ("price_vwap", "option_signal", "vwap_signal")
    )
    crossed_up = start_imbalance <= 0 < current_imbalance
    crossed_down = start_imbalance >= 0 > current_imbalance

    if (
        crossed_up
        and average_imbalance >= 0
        and momentum == "BULLISH"
        and score >= direction_threshold
        and bullish_confirmations >= 2
    ):
        state = "BULLISH_CONFIRMED"
    elif (
        crossed_down
        and average_imbalance <= 0
        and momentum == "BEARISH"
        and score <= -direction_threshold
        and bearish_confirmations >= 2
    ):
        state = "BEARISH_CONFIRMED"
    elif current_condition == "BEARISH":
        if momentum == "BULLISH":
            state = "BEARISH_WEAKENING"
        elif momentum == "BEARISH" and score <= -direction_threshold:
            state = "BEARISH_STRENGTHENING"
        else:
            state = "BEARISH_WEAKENING"
    elif current_condition == "BULLISH":
        if momentum == "BEARISH":
            state = "BULLISH_WEAKENING"
        elif momentum == "BULLISH" and score >= direction_threshold:
            state = "BULLISH_STRENGTHENING"
        else:
            state = "BULLISH_WEAKENING"
    elif momentum == "BULLISH" and score >= direction_threshold:
        state = "BULLISH_DEVELOPING"
    elif momentum == "BEARISH" and score <= -direction_threshold:
        state = "BEARISH_DEVELOPING"
    elif abs(flow_momentum_score) >= momentum_threshold:
        state = "REVERSAL_WATCH"
    else:
        state = "NEUTRAL_TRANSITION"

    if (
        score >= direction_threshold
        and momentum == "BULLISH"
        and bullish_confirmations >= 2
        and average_imbalance >= 0
        and current_imbalance >= 0
    ):
        predicted_label = "UP"
    elif (
        score <= -direction_threshold
        and momentum == "BEARISH"
        and bearish_confirmations >= 2
        and average_imbalance <= 0
        and current_imbalance <= 0
    ):
        predicted_label = "DOWN"
    else:
        predicted_label = "FLAT"

    final_sign = 1 if score > 0 else -1 if score < 0 else 0
    agreeing_weight = sum(
        float(weights.get(name, 0))
        for name, value in component_scores.items()
        if final_sign == 0 or value * final_sign >= 0
    )
    agreement = agreeing_weight / available_weight if available_weight else 0
    completeness = min(
        1.0,
        available_weight
        / sum(float(value) for value in weights.values()),
    )
    average_ocr = statistics.mean(row.confidence for row in window)
    confidence = round(
        average_ocr * 0.55 + agreement * 25 + completeness * 10
    )
    if abs(score) < direction_threshold:
        confidence -= 10
    confidence = max(0, min(90, confidence))

    pcr_directions = [
        current_row.pcr - previous_row.pcr
        for previous_row, current_row in zip(window, window[1:])
    ]
    rising_steps = sum(change > 0 for change in pcr_directions)
    falling_steps = sum(change < 0 for change in pcr_directions)
    reasons = [
        (
            f"{len(window)}-row {horizon_minutes}-minute averages: "
            f"Call={format_number(average_call)}, "
            f"Put={format_number(average_put)}, "
            f"Diff={format_number(average_diff)}, "
            f"PCR={average_pcr:.3f}."
        ),
        (
            f"{horizon_minutes}-minute PCR: "
            f"{start.pcr:.3f} → {current.pcr:.3f} "
            f"({pcr_change:+.3f}); rising steps={rising_steps}, "
            f"falling steps={falling_steps}."
        ),
        (
            f"Call changed {format_percent(call_change_pct)} and Put changed "
            f"{format_percent(put_change_pct)} over {horizon_minutes} minutes."
        ),
        (
            f"Diff moved from {format_number(start.diff_value)} to "
            f"{format_number(current.diff_value)} "
            f"({format_number(diff_change)} change)."
        ),
        (
            f"Average normalized imbalance is {average_imbalance:+.3f}; "
            f"latest-row imbalance is {current_imbalance:+.3f}."
        ),
    ]
    if average_price is not None and average_vwap is not None:
        reasons.append(
            f"Average Price {average_price:g} versus average VWAP "
            f"{average_vwap:g}; latest signals: "
            f"Option={current.option_signal or 'n/a'}, "
            f"VWAP={current.vwap_signal or 'n/a'}."
        )

    return MarketAnalysis(
        state=state,
        score=score,
        confidence=confidence,
        current_condition=current_condition,
        latest_condition=latest_condition,
        momentum=momentum,
        predicted_label=predicted_label,
        horizon_minutes=horizon_minutes,
        current_price=current.price,
        current_pcr=current.pcr,
        average_call=average_call,
        average_put=average_put,
        average_diff=average_diff,
        average_pcr=average_pcr,
        average_price=average_price,
        average_vwap=average_vwap,
        pcr_change=pcr_change,
        call_change_pct=call_change_pct,
        put_change_pct=put_change_pct,
        diff_change=diff_change,
        imbalance=average_imbalance,
        latest_imbalance=current_imbalance,
        imbalance_change=imbalance_change,
        reasons=reasons,
        component_scores=component_scores,
        window_times=[row.time_text for row in window],
    )


def combine_timeframe_analyses(
    analyses: dict[int, MarketAnalysis],
    config: dict[str, Any],
) -> MarketAnalysis:
    """Build the final 45-minute forecast from 15m/30m/45m agreement."""

    primary_minutes = int(config.get("prediction_horizon_minutes", 45))
    primary = analyses.get(primary_minutes)
    if primary is None:
        primary = analyses[max(analyses)]

    available = {
        minutes: analysis
        for minutes, analysis in analyses.items()
        if analysis.state != "UNCONFIRMED"
    }
    if primary.state == "UNCONFIRMED" or len(available) < 2:
        return replace(
            primary,
            state="UNCONFIRMED",
            predicted_label="FLAT",
            confidence=0,
            reasons=[
                "At least two complete timeframes, including 45 minutes, "
                "are required for a combined forecast.",
                *primary.reasons,
            ],
        )

    configured_weights = config.get(
        "timeframe_weights",
        DEFAULT_CONFIG["timeframe_weights"],
    )
    raw_weights = {
        minutes: float(configured_weights.get(str(minutes), 0))
        for minutes in available
    }
    total_weight = sum(raw_weights.values())
    if total_weight <= 0:
        raw_weights = {minutes: 1.0 for minutes in available}
        total_weight = float(len(raw_weights))
    weights = {
        minutes: weight / total_weight
        for minutes, weight in raw_weights.items()
    }

    combined_score = clamp(
        sum(
            analysis.score * weights[minutes]
            for minutes, analysis in available.items()
        )
    )

    condition_values = {
        "BULLISH": 1.0,
        "BEARISH": -1.0,
        "BALANCED": 0.0,
        "UNKNOWN": 0.0,
    }
    momentum_values = {
        "BULLISH": 1.0,
        "BEARISH": -1.0,
        "MIXED": 0.0,
        "UNKNOWN": 0.0,
    }
    condition_score = sum(
        condition_values.get(analysis.current_condition, 0)
        * weights[minutes]
        for minutes, analysis in available.items()
    )
    momentum_score = sum(
        momentum_values.get(analysis.momentum, 0)
        * weights[minutes]
        for minutes, analysis in available.items()
    )
    if condition_score >= 0.20:
        condition = "BULLISH"
    elif condition_score <= -0.20:
        condition = "BEARISH"
    else:
        condition = "BALANCED"
    if momentum_score >= 0.20:
        momentum = "BULLISH"
    elif momentum_score <= -0.20:
        momentum = "BEARISH"
    else:
        momentum = "MIXED"

    up_timeframes = [
        minutes
        for minutes, analysis in available.items()
        if analysis.predicted_label == "UP"
    ]
    down_timeframes = [
        minutes
        for minutes, analysis in available.items()
        if analysis.predicted_label == "DOWN"
    ]
    if (
        primary.predicted_label == "UP"
        and len(up_timeframes) >= 2
        and not down_timeframes
    ):
        predicted_label = "UP"
    elif (
        primary.predicted_label == "DOWN"
        and len(down_timeframes) >= 2
        and not up_timeframes
    ):
        predicted_label = "DOWN"
    else:
        predicted_label = "FLAT"

    if condition == "BEARISH":
        state = (
            "BEARISH_STRENGTHENING"
            if momentum == "BEARISH" and predicted_label == "DOWN"
            else "BEARISH_WEAKENING"
        )
    elif condition == "BULLISH":
        state = (
            "BULLISH_STRENGTHENING"
            if momentum == "BULLISH" and predicted_label == "UP"
            else "BULLISH_WEAKENING"
        )
    elif predicted_label == "UP":
        state = "BULLISH_DEVELOPING"
    elif predicted_label == "DOWN":
        state = "BEARISH_DEVELOPING"
    elif momentum != "MIXED":
        state = "REVERSAL_WATCH"
    else:
        state = "NEUTRAL_TRANSITION"

    condition_agreement = sum(
        weights[minutes]
        for minutes, analysis in available.items()
        if analysis.current_condition == condition
    )
    momentum_agreement = sum(
        weights[minutes]
        for minutes, analysis in available.items()
        if analysis.momentum == momentum
    )
    agreement = (condition_agreement + momentum_agreement) / 2
    weighted_confidence = sum(
        analysis.confidence * weights[minutes]
        for minutes, analysis in available.items()
    )
    confidence = round(
        weighted_confidence * (0.70 + agreement * 0.30)
    )
    if predicted_label == "FLAT":
        confidence -= 5
    confidence = max(0, min(90, confidence))

    timeframe_text = "; ".join(
        (
            f"{minutes}m={analysis.current_condition}/"
            f"{analysis.momentum}/{analysis.predicted_label}"
        )
        for minutes, analysis in sorted(available.items())
    )
    reasons = [
        f"Timeframe agreement: {timeframe_text}.",
        (
            f"Combined timeframe score is {combined_score:+.3f}; "
            f"condition agreement={condition_agreement:.0%}, "
            f"momentum agreement={momentum_agreement:.0%}."
        ),
        *primary.reasons,
    ]
    return replace(
        primary,
        state=state,
        score=combined_score,
        confidence=confidence,
        current_condition=condition,
        momentum=momentum,
        predicted_label=predicted_label,
        reasons=reasons,
    )


def apply_intraday_option_signal(
    direction: str,
    confidence: int,
    signals: IntradaySignalResult,
    config: dict[str, Any],
) -> tuple[str, int, str | None]:
    """Prefer the source's explicit latest Option Signal when it is reliable."""

    if not bool(config.get("prefer_intraday_option_signal", True)):
        return direction, confidence, None

    minimum_confidence = float(
        config.get("minimum_intraday_signal_confidence", 70)
    )
    if (
        signals.option_signal not in {"BUY", "SELL"}
        or signals.option_confidence < minimum_confidence
    ):
        return direction, confidence, None

    explicit_direction = (
        "BULLISH_BIAS"
        if signals.option_signal == "BUY"
        else "BEARISH_BIAS"
    )
    explanation = (
        f"Latest Intraday Option Signal is {signals.option_signal} "
        f"(OCR {signals.option_confidence:.0f}%); this explicit source signal "
        "takes priority over the derived OI vote."
    )
    return (
        explicit_direction,
        min(90, round(signals.option_confidence)),
        explanation,
    )


def clock_list_strings(
    candidates: list[tuple[int, int, int, str, float]],
) -> list[str]:
    result: list[str] = []
    for hour, minute, second, _raw, _confidence in candidates:
        if second:
            result.append(f"{hour:02d}:{minute:02d}:{second:02d}")
        else:
            result.append(f"{hour:02d}:{minute:02d}")
    return result


def extract_latest_snapshot_datetime(
    words: list[OCRWord],
    raw_ocr_text: str,
    reference_at: str,
    image_width: int,
    config: dict[str, Any],
) -> tuple[str, str, list[str]] | None:
    """
    Read the topmost time in the Intraday Trend table.

    The date is used only as an internal day anchor. The report clock and all
    comparison clocks come solely from the Intraday Trend Time column.
    """
    candidates = intraday_time_candidates(words, image_width, config)
    if not candidates:
        return None

    hour, minute, second, raw_value, _confidence = candidates[0]

    offset_minutes = int(
        config.get(
            "snapshot_timezone_offset_minutes",
            config.get("report_timezone_offset_minutes", 330),
        )
    )
    snapshot_timezone = timezone(timedelta(minutes=offset_minutes))
    reference_local = parse_iso_datetime(reference_at).astimezone(
        snapshot_timezone
    )

    printed_date = extract_snapshot_date(raw_ocr_text)
    if printed_date is not None:
        local_date = printed_date.date()
    else:
        # This date anchor prevents cross-day comparisons. It does not affect
        # the displayed report time, which is read only from the image.
        local_date = reference_local.date()

    snapshot_datetime = datetime(
        local_date.year,
        local_date.month,
        local_date.day,
        hour,
        minute,
        second,
        tzinfo=snapshot_timezone,
    )
    market_start = parse_clock_setting(
        str(config.get("snapshot_market_start", "08:00")),
        8,
        0,
    )
    market_end = parse_clock_setting(
        str(config.get("snapshot_market_end", "16:30")),
        16,
        30,
    )
    market_start_seconds = market_start[0] * 3600 + market_start[1] * 60
    market_end_seconds = market_end[0] * 3600 + market_end[1] * 60
    candidate_seconds = hour * 3600 + minute * 60 + second
    outside_market_hours = not (
        market_start_seconds <= candidate_seconds <= market_end_seconds
    )
    implausibly_after_upload = (
        snapshot_datetime > reference_local + timedelta(minutes=5)
    )
    if outside_market_hours or implausibly_after_upload:
        interval_minutes = max(
            1,
            int(config.get("intraday_expected_interval_minutes", 15)),
        )
        repaired_minute = (
            reference_local.minute // interval_minutes
        ) * interval_minutes
        repaired = datetime(
            local_date.year,
            local_date.month,
            local_date.day,
            reference_local.hour,
            repaired_minute,
            tzinfo=snapshot_timezone,
        )
        repaired_seconds = (
            repaired.hour * 3600 + repaired.minute * 60
        )
        if market_start_seconds <= repaired_seconds <= market_end_seconds:
            LOGGER.info(
                "Corrected implausible latest table time %s to %s "
                "using the Telegram post time.",
                snapshot_datetime.strftime("%H:%M"),
                repaired.strftime("%H:%M"),
            )
            snapshot_datetime = repaired
            raw_value = f"{raw_value} [OCR corrected]"
    return (
        snapshot_datetime.isoformat(),
        raw_value,
        clock_list_strings(candidates),
    )

def format_snapshot_clock(value: str, config: dict[str, Any]) -> str:
    """Display only the time printed in the snapshot."""
    try:
        parsed = parse_iso_datetime(value)
        offset = timezone(
            timedelta(
                minutes=int(
                    config.get(
                        "snapshot_timezone_offset_minutes",
                        config.get("report_timezone_offset_minutes", 330),
                    )
                )
            )
        )
        label = str(config.get("report_timezone_label", "")).strip()
        local_value = parsed.astimezone(offset)
        formatted = (
            local_value.strftime("%H:%M:%S")
            if local_value.second
            else local_value.strftime("%H:%M")
        )
        return f"{formatted} {label}".strip()
    except (ValueError, TypeError, KeyError):
        return value


def elapsed_text(seconds: int) -> str:
    minutes, remaining_seconds = divmod(max(0, seconds), 60)
    if remaining_seconds:
        return f"{minutes}m {remaining_seconds}s"
    return f"{minutes} minutes"


def save_debug_files(
    image: Any,
    processed: Any,
    words: list[OCRWord],
    rows: list[StrikeRow],
    strike_x: float,
    source_key: str,
) -> None:
    short_key = hashlib.sha1(source_key.encode("utf-8")).hexdigest()[:12]
    overlay = image.copy()
    image_height, _ = overlay.shape[:2]
    cv2.line(overlay, (int(strike_x), 0), (int(strike_x), image_height), (255, 0, 255), 2)

    for word in words:
        cv2.rectangle(overlay, (word.left, word.top), (word.right, word.bottom), (0, 255, 0), 1)

    for row in rows:
        y = int(row.top)
        cv2.line(overlay, (0, y), (overlay.shape[1] - 1, y), (0, 0, 255), 1)
        cv2.putText(
            overlay,
            f"Strike {row.strike:g}",
            (5, max(15, y - 3)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (0, 0, 255),
            1,
            cv2.LINE_AA,
        )

    cv2.imwrite(str(DEBUG_DIR / f"{short_key}_overlay.png"), overlay)
    cv2.imwrite(str(DEBUG_DIR / f"{short_key}_processed.png"), processed)
    (DEBUG_DIR / f"{short_key}_rows.json").write_text(
        json.dumps([asdict(row) for row in rows], indent=2),
        encoding="utf-8",
    )


# -----------------------------------------------------------------------------
# Persistence and comparison
# -----------------------------------------------------------------------------


def source_already_processed(
    connection: sqlite3.Connection,
    source_key: str,
    image_path: Path | None = None,
) -> bool:
    normalized_image_path = (
        str(image_path.resolve())
        if image_path is not None
        else None
    )
    row = connection.execute(
        """
        SELECT 1
        FROM snapshots
        WHERE source_key = ?
           OR (? IS NOT NULL AND image_path = ?)
        LIMIT 1
        """,
        (
            source_key,
            normalized_image_path,
            normalized_image_path,
        ),
    ).fetchone()
    return row is not None


def insert_snapshot(
    connection: sqlite3.Connection,
    source_key: str,
    message_id: int | None,
    channel: str,
    captured_at: str,
    image_path: Path,
    instrument: str | None,
    expiry: str | None,
    strike_x: float,
    raw_ocr_text: str,
    rows: list[StrikeRow],
    intraday_rows: list[IntradayTrendRow],
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO snapshots (
            source_key, message_id, channel, captured_at, image_path,
            instrument, expiry, strike_x, row_count, raw_ocr_text, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_key,
            message_id,
            channel,
            captured_at,
            str(image_path),
            instrument,
            expiry,
            strike_x,
            len(rows),
            raw_ocr_text,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    snapshot_id = int(cursor.lastrowid)

    connection.executemany(
        """
        INSERT INTO strike_rows (
            snapshot_id, strike, call_oi, call_change_oi, call_ltp,
            put_ltp, put_change_oi, put_oi, confidence, raw_text, top_position
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                snapshot_id,
                row.strike,
                row.call_oi,
                row.call_change_oi,
                row.call_ltp,
                row.put_ltp,
                row.put_change_oi,
                row.put_oi,
                row.confidence,
                row.raw_text,
                row.top,
            )
            for row in rows
        ],
    )
    connection.executemany(
        """
        INSERT OR IGNORE INTO intraday_rows (
            snapshot_id, row_time, call_value, put_value, diff_value, pcr,
            option_signal, price, vwap, vwap_signal, confidence, math_valid,
            raw_text, top_position
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                snapshot_id,
                row.time_text,
                row.call_value,
                row.put_value,
                row.diff_value,
                row.pcr,
                row.option_signal,
                row.price,
                row.vwap,
                row.vwap_signal,
                row.confidence,
                int(row.math_valid),
                row.raw_text,
                row.top,
            )
            for row in intraday_rows
        ],
    )
    connection.commit()
    return snapshot_id


def load_rows(connection: sqlite3.Connection, snapshot_id: int) -> dict[float, StrikeRow]:
    database_rows = connection.execute(
        "SELECT * FROM strike_rows WHERE snapshot_id = ? ORDER BY strike",
        (snapshot_id,),
    ).fetchall()
    return {
        float(row["strike"]): StrikeRow(
            strike=float(row["strike"]),
            call_oi=row["call_oi"],
            call_change_oi=row["call_change_oi"],
            call_ltp=row["call_ltp"],
            put_ltp=row["put_ltp"],
            put_change_oi=row["put_change_oi"],
            put_oi=row["put_oi"],
            confidence=row["confidence"] or 0,
            raw_text=row["raw_text"] or "",
            top=row["top_position"] or 0,
        )
        for row in database_rows
    }


def load_intraday_rows(
    connection: sqlite3.Connection,
    snapshot_id: int,
) -> list[IntradayTrendRow]:
    """Load the OCR-validated Intraday Trend rows stored for one image."""

    database_rows = connection.execute(
        """
        SELECT *
        FROM intraday_rows
        WHERE snapshot_id = ?
        ORDER BY top_position
        """,
        (snapshot_id,),
    ).fetchall()
    return [
        IntradayTrendRow(
            time_text=str(row["row_time"]),
            call_value=float(row["call_value"]),
            put_value=float(row["put_value"]),
            diff_value=float(row["diff_value"]),
            pcr=float(row["pcr"]),
            option_signal=row["option_signal"],
            price=row["price"],
            vwap=row["vwap"],
            vwap_signal=row["vwap_signal"],
            confidence=float(row["confidence"] or 0),
            math_valid=bool(row["math_valid"]),
            raw_text=str(row["raw_text"] or ""),
            top=float(row["top_position"] or 0),
        )
        for row in database_rows
    ]


def load_recent_intraday_rows_from_images(
    connection: sqlite3.Connection,
    instrument: str,
    market_day: str,
    latest_captured_at: str,
    minutes: int,
) -> tuple[list[IntradayTrendRow], int]:
    """Build a clean recent series from the closest matching stored images."""

    latest_datetime = parse_iso_datetime(latest_captured_at)
    earliest_datetime = latest_datetime - timedelta(minutes=minutes + 30)
    candidates = connection.execute(
        """
        SELECT ir.*, s.id AS source_snapshot_id, s.captured_at AS source_time
        FROM intraday_rows AS ir
        JOIN snapshots AS s ON s.id = ir.snapshot_id
        WHERE s.instrument = ?
          AND substr(s.captured_at, 1, 10) = ?
          AND s.captured_at <= ?
        ORDER BY s.id DESC, ir.top_position
        """,
        (instrument, market_day, latest_captured_at),
    ).fetchall()

    selected: dict[str, tuple[tuple[float, float, int], sqlite3.Row]] = {}
    for row in candidates:
        clock = parse_clock_word(str(row["row_time"]))
        if clock is None:
            continue
        row_datetime = latest_datetime.replace(
            hour=clock[0],
            minute=clock[1],
            second=clock[2],
            microsecond=0,
        )
        if row_datetime < earliest_datetime or row_datetime > latest_datetime:
            continue
        source_datetime = parse_iso_datetime(str(row["source_time"]))
        distance_seconds = abs(
            (source_datetime - row_datetime).total_seconds()
        )
        # Prefer the image posted for this exact row time, then stronger OCR.
        rank = (
            distance_seconds,
            -float(row["confidence"] or 0),
            -int(row["source_snapshot_id"]),
        )
        existing = selected.get(str(row["row_time"]))
        if existing is None or rank < existing[0]:
            selected[str(row["row_time"])] = (rank, row)

    rows: list[IntradayTrendRow] = []
    source_snapshot_ids: set[int] = set()
    for _, row in selected.values():
        source_snapshot_ids.add(int(row["source_snapshot_id"]))
        rows.append(IntradayTrendRow(
            time_text=str(row["row_time"]),
            call_value=float(row["call_value"]),
            put_value=float(row["put_value"]),
            diff_value=float(row["diff_value"]),
            pcr=float(row["pcr"]),
            option_signal=row["option_signal"],
            price=row["price"],
            vwap=row["vwap"],
            vwap_signal=row["vwap_signal"],
            confidence=float(row["confidence"] or 0),
            math_valid=bool(row["math_valid"]),
            raw_text=str(row["raw_text"] or ""),
            top=float(row["top_position"] or 0),
        ))
    rows.sort(key=trend_row_seconds, reverse=True)
    return rows, len(source_snapshot_ids)


def inferred_strike_interval(rows: Iterable[StrikeRow]) -> float:
    strikes = sorted({row.strike for row in rows})
    intervals = [
        current - previous
        for previous, current in zip(strikes, strikes[1:])
        if current > previous
    ]
    return float(statistics.median(intervals)) if intervals else 1.0


def option_moneyness_bucket(
    strike: float,
    spot_price: float,
    option_type: str,
    strike_interval: float,
) -> str:
    """Classify a strike without tying calibration to one absolute index level."""

    interval = max(strike_interval, 1.0)
    distance_steps = (strike - spot_price) / interval
    if abs(distance_steps) <= 0.75:
        return "ATM"
    if option_type == "CALL":
        return "ITM" if distance_steps < 0 else "OTM"
    return "ITM" if distance_steps > 0 else "OTM"


def evaluate_pending_option_outcomes(
    connection: sqlite3.Connection,
    instrument: str | None,
    expiry: str | None,
    captured_at: str,
    current_rows: dict[float, StrikeRow],
    config: dict[str, Any],
) -> int:
    """Attach future same-strike premiums only after each target time arrives."""

    if not instrument or not current_rows:
        return 0
    current_datetime = parse_iso_datetime(captured_at)
    allowed_lateness = int(
        config.get("option_outcome_tolerance_seconds", 90)
    )
    round_trip_cost = float(
        config.get("option_probability_round_trip_cost_pct", 0.005)
    )
    pending = connection.execute(
        """
        SELECT *
        FROM option_outcomes
        WHERE instrument = ?
          AND exit_ltp IS NULL
        ORDER BY target_time, id
        """,
        (instrument,),
    ).fetchall()

    evaluated = 0
    for outcome in pending:
        stored_expiry = outcome["expiry"]
        if str(stored_expiry or "") != str(expiry or ""):
            continue
        target_datetime = parse_iso_datetime(str(outcome["target_time"]))
        elapsed_after_target = (
            current_datetime - target_datetime
        ).total_seconds()
        if elapsed_after_target < 0 or elapsed_after_target > allowed_lateness:
            continue

        current_row = current_rows.get(float(outcome["strike"]))
        if current_row is None:
            continue
        exit_ltp = (
            current_row.call_ltp
            if str(outcome["option_type"]) == "CALL"
            else current_row.put_ltp
        )
        entry_ltp = float(outcome["entry_ltp"])
        if exit_ltp is None or exit_ltp <= 0 or entry_ltp <= 0:
            continue

        premium_return = (float(exit_ltp) - entry_ltp) / entry_ltp
        buy_net_return = premium_return - round_trip_cost
        sell_net_return = -premium_return - round_trip_cost
        connection.execute(
            """
            UPDATE option_outcomes
            SET exit_ltp = ?, exit_time = ?, premium_return_pct = ?,
                buy_net_return_pct = ?, sell_net_return_pct = ?,
                evaluated_at = ?
            WHERE id = ?
            """,
            (
                float(exit_ltp),
                current_datetime.isoformat(),
                premium_return * 100,
                buy_net_return * 100,
                sell_net_return * 100,
                datetime.now(timezone.utc).isoformat(),
                int(outcome["id"]),
            ),
        )
        evaluated += 1
    connection.commit()
    return evaluated


def insert_option_outcomes(
    connection: sqlite3.Connection,
    snapshot_id: int,
    instrument: str | None,
    expiry: str | None,
    captured_at: str,
    current_rows: dict[float, StrikeRow],
    timeframe_analyses: dict[int, MarketAnalysis],
    config: dict[str, Any],
) -> int:
    """Store premium entries for later walk-forward evaluation."""

    if not instrument or not current_rows:
        return 0
    minimum_ltp = float(config.get("option_probability_min_entry_ltp", 1.0))
    minimum_confidence = float(config.get("prediction_min_row_confidence", 55))
    entry_datetime = parse_iso_datetime(captured_at)
    interval = inferred_strike_interval(current_rows.values())
    created_at = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for horizon_minutes, analysis in timeframe_analyses.items():
        if analysis.current_price is None or analysis.current_price <= 0:
            continue
        target_time = entry_datetime + timedelta(minutes=horizon_minutes)
        for row in current_rows.values():
            if row.confidence < minimum_confidence:
                continue
            for option_type, entry_ltp in (
                ("CALL", row.call_ltp),
                ("PUT", row.put_ltp),
            ):
                if entry_ltp is None or entry_ltp < minimum_ltp:
                    continue
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO option_outcomes (
                        snapshot_id, instrument, expiry, strike, option_type,
                        horizon_minutes, entry_time, target_time, entry_ltp,
                        spot_price, moneyness, market_condition,
                        predicted_label, analysis_score,
                        analysis_confidence, row_confidence, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        instrument,
                        expiry,
                        row.strike,
                        option_type,
                        horizon_minutes,
                        entry_datetime.isoformat(),
                        target_time.isoformat(),
                        float(entry_ltp),
                        analysis.current_price,
                        option_moneyness_bucket(
                            row.strike,
                            analysis.current_price,
                            option_type,
                            interval,
                        ),
                        analysis.current_condition,
                        analysis.predicted_label,
                        analysis.score,
                        analysis.confidence,
                        row.confidence,
                        created_at,
                    ),
                )
                inserted += int(cursor.rowcount > 0)
    connection.commit()
    return inserted


def wilson_probability_interval(
    wins: int,
    sample_count: int,
    z_score: float = 1.96,
) -> tuple[float, float]:
    if sample_count <= 0:
        return 0.0, 0.0
    probability = wins / sample_count
    denominator = 1 + z_score * z_score / sample_count
    center = (
        probability + z_score * z_score / (2 * sample_count)
    ) / denominator
    margin = (
        z_score
        * math.sqrt(
            probability * (1 - probability) / sample_count
            + z_score * z_score / (4 * sample_count * sample_count)
        )
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def exponential_moving_average(
    values: list[float],
    period: int,
) -> float | None:
    """Return an EMA seeded by the period's simple moving average."""

    if period <= 0 or len(values) < period:
        return None
    current = statistics.mean(values[:period])
    multiplier = 2 / (period + 1)
    for value in values[period:]:
        current = value * multiplier + current * (1 - multiplier)
    return current


def ema_vwap_sell_confirmation(
    intraday_rows: list[IntradayTrendRow],
    option_type: str,
    minimum_rows: int,
) -> tuple[bool | None, float | None, float | None, str]:
    """Confirm a bearish Call sell or bullish Put sell with price/VWAP/EMAs."""

    reliable = [
        row
        for row in intraday_rows
        if row.math_valid
        and row.price is not None
        and row.price > 0
    ]
    reliable.sort(key=trend_row_seconds)
    prices = [float(row.price) for row in reliable]
    required_rows = max(21, minimum_rows)
    if len(prices) < required_rows:
        return (
            None,
            None,
            None,
            f"EMA unavailable: {len(prices)}/{required_rows} price rows.",
        )
    latest = reliable[-1]
    if latest.vwap is None or latest.vwap <= 0:
        return None, None, None, "VWAP unavailable in the latest row."
    ema9 = exponential_moving_average(prices, 9)
    ema21 = exponential_moving_average(prices, 21)
    if ema9 is None or ema21 is None:
        return None, ema9, ema21, "EMA calculation was unavailable."

    price = float(latest.price)
    vwap = float(latest.vwap)
    if option_type == "CALL":
        confirmed = price < vwap and ema9 < ema21
        requirement = "Price<VWAP and EMA9<EMA21"
    else:
        confirmed = price > vwap and ema9 > ema21
        requirement = "Price>VWAP and EMA9>EMA21"
    return (
        confirmed,
        ema9,
        ema21,
        (
            f"{requirement}; price={price:.2f}, VWAP={vwap:.2f}, "
            f"EMA9={ema9:.2f}, EMA21={ema21:.2f}."
        ),
    )


def defined_risk_hedge(
    current_rows: dict[float, StrikeRow],
    short_row: StrikeRow,
    option_type: str,
    minimum_confidence: float,
) -> tuple[StrikeRow | None, float | None, float | None, float | None]:
    """Find the nearest usable long leg for a vertical credit spread."""

    short_ltp = (
        short_row.call_ltp if option_type == "CALL" else short_row.put_ltp
    )
    if short_ltp is None or short_ltp <= 0:
        return None, None, None, None
    if option_type == "CALL":
        candidates = sorted(
            (
                row for row in current_rows.values()
                if row.strike > short_row.strike
                and row.call_ltp is not None
                and row.call_ltp > 0
                and row.confidence >= minimum_confidence
            ),
            key=lambda row: row.strike,
        )
        premium_field = "call_ltp"
    else:
        candidates = sorted(
            (
                row for row in current_rows.values()
                if row.strike < short_row.strike
                and row.put_ltp is not None
                and row.put_ltp > 0
                and row.confidence >= minimum_confidence
            ),
            key=lambda row: row.strike,
            reverse=True,
        )
        premium_field = "put_ltp"

    for hedge in candidates:
        hedge_ltp = float(getattr(hedge, premium_field))
        credit = float(short_ltp) - hedge_ltp
        width = abs(hedge.strike - short_row.strike)
        if credit <= 0 or width <= credit:
            continue
        maximum_loss = width - credit
        risk_reward_ratio = maximum_loss / credit
        return hedge, credit, maximum_loss, risk_reward_ratio
    return None, None, None, None


def grouped_independent_returns(
    rows: Iterable[sqlite3.Row],
) -> tuple[list[float], int]:
    """Collapse correlated same-timestamp strikes into one median observation."""

    values_by_time: dict[str, list[float]] = {}
    trading_days: set[str] = set()
    for row in rows:
        entry_time = str(row["entry_time"])
        value = row["net_return"]
        if value is None:
            continue
        values_by_time.setdefault(entry_time, []).append(float(value))
        try:
            trading_days.add(parse_iso_datetime(entry_time).date().isoformat())
        except (TypeError, ValueError):
            pass
    independent_returns = [
        statistics.median(values_by_time[entry_time])
        for entry_time in sorted(values_by_time)
    ]
    return independent_returns, len(trading_days)


def estimate_option_probabilities(
    connection: sqlite3.Connection,
    instrument: str | None,
    current_rows: dict[float, StrikeRow],
    analysis: MarketAnalysis,
    config: dict[str, Any],
    intraday_trend_rows: list[IntradayTrendRow] | None = None,
    volume_confirmation: bool | None = None,
) -> list[OptionProbabilityResult]:
    """Estimate independent outcomes and enforce every strict sell gate."""

    if not instrument or analysis.current_price is None or not current_rows:
        return []
    minimum_samples = int(config.get("option_probability_min_samples", 100))
    minimum_days = int(config.get("option_probability_min_trading_days", 20))
    probability_threshold = float(
        config.get("option_probability_threshold", 0.60)
    )
    lower_bound_threshold = float(
        config.get("option_probability_lower_bound_threshold", 0.52)
    )
    score_threshold = float(
        config.get("option_probability_score_threshold", 0.20)
    )
    minimum_candidate_ltp = float(
        config.get("option_candidate_min_entry_ltp", 10.0)
    )
    minimum_row_confidence = float(
        config.get("option_candidate_min_row_confidence", 70)
    )
    maximum_tail_loss = float(
        config.get("option_probability_max_tail_loss_pct", 35.0)
    )
    maximum_observed_loss = float(
        config.get("option_probability_max_observed_loss_pct", 100.0)
    )
    maximum_risk_reward = float(
        config.get("option_max_risk_reward_ratio", 5.0)
    )
    require_volume = bool(
        config.get("option_require_volume_confirmation", True)
    )
    ema_minimum_rows = int(config.get("option_ema_min_rows", 21))
    maximum_rows = int(config.get("option_probability_max_rows", 8))
    interval = inferred_strike_interval(current_rows.values())
    results: list[OptionProbabilityResult] = []
    trend_rows = intraday_trend_rows or []

    for row in current_rows.values():
        for option_type, entry_ltp in (
            ("CALL", row.call_ltp),
            ("PUT", row.put_ltp),
        ):
            if entry_ltp is None or entry_ltp <= 0:
                continue
            action = "NO TRADE"
            if analysis.score <= -score_threshold and option_type == "CALL":
                action = "SELL"
            elif analysis.score >= score_threshold and option_type == "PUT":
                action = "SELL"

            moneyness = option_moneyness_bucket(
                row.strike,
                analysis.current_price,
                option_type,
                interval,
            )
            return_column = "sell_net_return_pct"
            historical_returns: list[float] = []
            trading_day_count = 0
            if action != "NO TRADE" and analysis.current_condition != "UNKNOWN":
                historical_rows = connection.execute(
                        f"""
                        SELECT entry_time, {return_column} AS net_return
                        FROM option_outcomes
                        WHERE instrument = ?
                          AND horizon_minutes = ?
                          AND option_type = ?
                          AND moneyness = ?
                          AND market_condition = ?
                          AND entry_ltp >= ?
                          AND exit_time IS NOT NULL
                          AND {return_column} IS NOT NULL
                        ORDER BY entry_time, strike
                        """,
                        (
                            instrument,
                            analysis.horizon_minutes,
                            option_type,
                            moneyness,
                            analysis.current_condition,
                            minimum_candidate_ltp,
                        ),
                    ).fetchall()
                historical_returns, trading_day_count = (
                    grouped_independent_returns(historical_rows)
                )
            sample_count = len(historical_returns)
            winning_returns = [value for value in historical_returns if value > 0]
            losing_returns = [value for value in historical_returns if value <= 0]
            probability = (
                len(winning_returns) / sample_count if sample_count else None
            )
            low: float | None = None
            high: float | None = None
            if probability is not None:
                low, high = wilson_probability_interval(
                    len(winning_returns),
                    sample_count,
                )
            expected_value = (
                statistics.mean(historical_returns)
                if historical_returns
                else None
            )
            average_win = (
                statistics.mean(winning_returns) if winning_returns else None
            )
            average_loss = (
                statistics.mean(losing_returns) if losing_returns else None
            )
            worst_loss = min(historical_returns) if historical_returns else None
            tail_count = max(1, math.ceil(sample_count * 0.05))
            tail_loss = (
                statistics.mean(sorted(historical_returns)[:tail_count])
                if historical_returns
                else None
            )
            data_quality_label = (
                "High" if row.confidence >= 85
                else "Medium" if row.confidence >= 65
                else "Low"
            )

            direction_aligned = action == "SELL" and (
                (option_type == "CALL" and analysis.predicted_label == "DOWN")
                or (option_type == "PUT" and analysis.predicted_label == "UP")
            )
            ema_confirmed, ema9, ema21, ema_detail = (
                ema_vwap_sell_confirmation(
                    trend_rows,
                    option_type,
                    ema_minimum_rows,
                )
                if action == "SELL"
                else (None, None, None, "Not the directional sell side.")
            )
            oi_change = (
                row.call_change_oi
                if option_type == "CALL"
                else row.put_change_oi
            )
            total_oi = row.call_oi if option_type == "CALL" else row.put_oi
            oi_confirmed = (
                action == "SELL"
                and oi_change is not None
                and oi_change > 0
                and total_oi is not None
                and total_oi > 0
            )
            premium_confirmed = (
                action == "SELL"
                and entry_ltp >= minimum_candidate_ltp
                and row.confidence >= minimum_row_confidence
                and total_oi is not None
                and total_oi > 0
                and moneyness in {"ATM", "OTM"}
            )
            hedge, spread_credit, maximum_loss, risk_reward_ratio = (
                defined_risk_hedge(
                    current_rows,
                    row,
                    option_type,
                    minimum_row_confidence,
                )
                if action == "SELL"
                else (None, None, None, None)
            )
            hedge_ltp = None
            if hedge is not None:
                hedge_ltp = (
                    hedge.call_ltp
                    if option_type == "CALL"
                    else hedge.put_ltp
                )
            historical_risk_ok = (
                worst_loss is not None
                and tail_loss is not None
                and worst_loss >= -maximum_observed_loss
                and tail_loss >= -maximum_tail_loss
            )
            spread_risk_ok = (
                risk_reward_ratio is not None
                and risk_reward_ratio <= maximum_risk_reward
            )

            def gate_status(value: bool | None) -> str:
                if value is None:
                    return "N/A"
                return "PASS" if value else "FAIL"

            sample_ok = (
                sample_count >= minimum_samples
                and trading_day_count >= minimum_days
            )
            probability_ok = (
                probability is not None
                and probability >= probability_threshold
            )
            lower_bound_ok = (
                low is not None and low >= lower_bound_threshold
            )
            expected_value_ok = (
                expected_value is not None and expected_value > 0
            )
            if require_volume:
                volume_ok: bool | None = volume_confirmation
            else:
                volume_ok = True

            gate_statuses = {
                "FORECAST": gate_status(direction_aligned),
                "INDEPENDENT SAMPLES": gate_status(sample_ok),
                "PROBABILITY": gate_status(probability_ok),
                "LOWER CONFIDENCE": gate_status(lower_bound_ok),
                "EXPECTED VALUE": gate_status(expected_value_ok),
                "WORST-CASE RISK": gate_status(
                    historical_risk_ok and spread_risk_ok
                ),
                "VWAP + EMA": gate_status(ema_confirmed),
                "VOLUME": gate_status(volume_ok),
                "OI SUPPORT": gate_status(oi_confirmed),
                "PREMIUM + LIQUIDITY": gate_status(premium_confirmed),
                "DEFINED-RISK HEDGE": gate_status(hedge is not None),
            }
            gate_details = {
                "FORECAST": (
                    f"Forecast={analysis.predicted_label}; required "
                    f"{'DOWN' if option_type == 'CALL' else 'UP'}."
                ),
                "INDEPENDENT SAMPLES": (
                    f"{sample_count}/{minimum_samples} timestamps; "
                    f"{trading_day_count}/{minimum_days} trading days."
                ),
                "PROBABILITY": (
                    f"{100 * probability:.1f}% vs {100 * probability_threshold:.1f}%."
                    if probability is not None else "Probability unavailable."
                ),
                "LOWER CONFIDENCE": (
                    f"{100 * low:.1f}% vs {100 * lower_bound_threshold:.1f}%."
                    if low is not None else "Lower confidence bound unavailable."
                ),
                "EXPECTED VALUE": (
                    f"{expected_value:+.2f}% after costs."
                    if expected_value is not None else "Expected value unavailable."
                ),
                "WORST-CASE RISK": (
                    f"Worst={format_decimal(worst_loss)}%; tail5="
                    f"{format_decimal(tail_loss)}%; spread R:R="
                    f"{format_decimal(risk_reward_ratio)}."
                ),
                "VWAP + EMA": ema_detail,
                "VOLUME": (
                    "Verified traded volume confirmed."
                    if volume_confirmation is True
                    else "Verified traded volume did not confirm."
                    if volume_confirmation is False
                    else "Verified traded volume is unavailable."
                ),
                "OI SUPPORT": (
                    f"Selected-side ChgOI={format_number(oi_change)}; "
                    f"OI={format_number(total_oi)}."
                ),
                "PREMIUM + LIQUIDITY": (
                    f"LTP={entry_ltp:.2f} (minimum {minimum_candidate_ltp:.2f}); "
                    f"OCR={row.confidence:.0f}%; moneyness={moneyness}."
                ),
                "DEFINED-RISK HEDGE": (
                    f"Buy {hedge.strike:g} {option_type}; credit="
                    f"{format_decimal(spread_credit)}; max loss="
                    f"{format_decimal(maximum_loss)}."
                    if hedge is not None
                    else "No usable farther OTM long leg was available."
                ),
            }
            failed_gates = [
                name
                for name, status in gate_statuses.items()
                if status != "PASS"
            ]
            all_gates_passed = action == "SELL" and not failed_gates
            if all_gates_passed and hedge is not None:
                model_signal = (
                    f"SELL {row.strike:g} {option_type}; "
                    f"BUY {hedge.strike:g} {option_type} HEDGE"
                )
                reason = "All strict probability and risk gates passed."
            elif action == "NO TRADE":
                model_signal = "NO TRADE"
                reason = "This is not the directional option-selling side."
            else:
                model_signal = "NO TRADE"
                reason = "Failed gates: " + ", ".join(failed_gates) + "."

            results.append(OptionProbabilityResult(
                horizon_minutes=analysis.horizon_minutes,
                strike=row.strike,
                option_type=option_type,
                entry_ltp=float(entry_ltp),
                moneyness=moneyness,
                evaluated_action=action,
                model_signal=model_signal,
                win_probability=probability,
                probability_low=low,
                probability_high=high,
                sample_count=sample_count,
                trading_day_count=trading_day_count,
                average_win_pct=average_win,
                average_loss_pct=average_loss,
                expected_value_pct=expected_value,
                worst_loss_pct=worst_loss,
                tail_loss_pct=tail_loss,
                ema9=ema9,
                ema21=ema21,
                hedge_strike=hedge.strike if hedge is not None else None,
                hedge_ltp=float(hedge_ltp) if hedge_ltp is not None else None,
                spread_credit=spread_credit,
                maximum_loss=maximum_loss,
                risk_reward_ratio=risk_reward_ratio,
                data_quality=data_quality_label,
                gate_statuses=gate_statuses,
                gate_details=gate_details,
                reason=reason,
            ))

    results.sort(key=lambda result: (
        result.model_signal == "NO TRADE",
        result.moneyness != "ATM",
        -(result.win_probability or -1),
        -abs(result.expected_value_pct or 0),
        result.strike,
        result.option_type,
    ))
    return results[:maximum_rows]


def write_option_backtest_report(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT instrument, expiry, strike, option_type, horizon_minutes,
               entry_time, target_time, entry_ltp, spot_price, moneyness,
               market_condition, predicted_label, analysis_score,
               analysis_confidence, row_confidence, exit_ltp, exit_time,
               premium_return_pct, buy_net_return_pct,
               sell_net_return_pct, evaluated_at
        FROM option_outcomes
        ORDER BY entry_time, instrument, strike, option_type, horizon_minutes
        """
    ).fetchall()
    if not rows:
        return
    output_path = REPORT_DIR / "option_probability_backtest.csv"
    with output_path.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(dict(row) for row in rows)


def evaluate_pending_predictions(
    connection: sqlite3.Connection,
    instrument: str | None,
    captured_at: str,
    actual_price: float | None,
    config: dict[str, Any],
) -> int:
    """Evaluate earlier predictions only when their future horizon has arrived."""
    if not instrument or actual_price is None or actual_price <= 0:
        return 0

    current_datetime = parse_iso_datetime(captured_at)
    expected_interval_seconds = (
        int(config.get("intraday_expected_interval_minutes", 15)) * 60
    )
    neutral_bps = float(config.get("prediction_neutral_move_bps", 10))
    pending = connection.execute(
        """
        SELECT *
        FROM predictions
        WHERE instrument = ?
          AND actual_price IS NULL
        ORDER BY prediction_time
        """,
        (instrument,),
    ).fetchall()

    evaluated = 0
    for prediction in pending:
        target_datetime = parse_iso_datetime(str(prediction["target_time"]))
        elapsed_after_target = (
            current_datetime - target_datetime
        ).total_seconds()
        if elapsed_after_target < 0:
            continue
        if elapsed_after_target > expected_interval_seconds + 60:
            # Do not attach a much later price to a missed horizon.
            continue

        starting_price = prediction["current_price"]
        if starting_price is None or float(starting_price) <= 0:
            continue
        return_bps = (
            (actual_price - float(starting_price))
            / float(starting_price)
            * 10_000
        )
        if return_bps > neutral_bps:
            actual_label = "UP"
        elif return_bps < -neutral_bps:
            actual_label = "DOWN"
        else:
            actual_label = "FLAT"

        is_correct: int | None
        if str(prediction["state"]) == "UNCONFIRMED":
            is_correct = None
        else:
            is_correct = int(
                str(prediction["predicted_label"]) == actual_label
            )

        connection.execute(
            """
            UPDATE predictions
            SET actual_price = ?,
                actual_return_bps = ?,
                actual_label = ?,
                is_correct = ?,
                evaluated_at = ?
            WHERE id = ?
            """,
            (
                actual_price,
                return_bps,
                actual_label,
                is_correct,
                datetime.now(timezone.utc).isoformat(),
                int(prediction["id"]),
            ),
        )
        evaluated += 1

    connection.commit()
    return evaluated


def insert_prediction(
    connection: sqlite3.Connection,
    snapshot_id: int,
    instrument: str | None,
    captured_at: str,
    analysis: MarketAnalysis,
) -> None:
    if not instrument or analysis.current_price is None:
        return
    prediction_datetime = parse_iso_datetime(captured_at)
    target_datetime = prediction_datetime + timedelta(
        minutes=analysis.horizon_minutes
    )
    connection.execute(
        """
        INSERT OR REPLACE INTO predictions (
            snapshot_id, instrument, prediction_time, target_time,
            horizon_minutes, state, predicted_label, score, confidence,
            current_price, features_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            instrument,
            prediction_datetime.isoformat(),
            target_datetime.isoformat(),
            analysis.horizon_minutes,
            analysis.state,
            analysis.predicted_label,
            analysis.score,
            analysis.confidence,
            analysis.current_price,
            json.dumps(asdict(analysis), ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    connection.commit()


def write_backtest_reports(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT instrument, prediction_time, target_time, state,
               predicted_label, score, confidence, current_price,
               actual_price, actual_return_bps, actual_label, is_correct
        FROM predictions
        ORDER BY prediction_time, instrument
        """
    ).fetchall()
    if not rows:
        return

    csv_path = REPORT_DIR / "prediction_backtest.csv"
    fieldnames = list(rows[0].keys())
    with csv_path.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(dict(row) for row in rows)

    instruments = sorted({str(row["instrument"]) for row in rows})
    summary_lines = [
        "45-MINUTE WALK-FORWARD BACKTEST",
        "Predictions are evaluated only with later snapshots; no future row is used at prediction time.",
        "",
    ]
    for instrument in instruments:
        instrument_rows = [
            row for row in rows if str(row["instrument"]) == instrument
        ]
        evaluated = [
            row
            for row in instrument_rows
            if row["actual_label"] is not None
        ]
        actionable = [
            row for row in evaluated if row["is_correct"] is not None
        ]
        correct = sum(int(row["is_correct"]) for row in actionable)
        accuracy = (
            100 * correct / len(actionable) if actionable else 0
        )
        coverage = (
            100
            * sum(str(row["state"]) != "UNCONFIRMED" for row in instrument_rows)
            / len(instrument_rows)
            if instrument_rows
            else 0
        )
        summary_lines.extend(
            [
                instrument,
                f"  Stored predictions: {len(instrument_rows)}",
                f"  Evaluated horizons: {len(evaluated)}",
                f"  Actionable evaluated: {len(actionable)}",
                f"  Correct: {correct}",
                f"  Accuracy: {accuracy:.1f}%",
                f"  Prediction coverage: {coverage:.1f}%",
                "",
            ]
        )

    (REPORT_DIR / "prediction_backtest_summary.txt").write_text(
        "\n".join(summary_lines),
        encoding="utf-8",
    )


def seconds_from_iso_clock(value: str) -> int:
    parsed = parse_iso_datetime(value)
    return parsed.hour * 3600 + parsed.minute * 60 + parsed.second


def find_previous_internal_snapshot(
    connection: sqlite3.Connection,
    current_snapshot_id: int,
    instrument: str | None,
    expiry: str | None,
    current_captured_at: str,
) -> tuple[sqlite3.Row | None, int | None, str]:
    """
    Compare using snapshot-internal market times only.

    Returns one of these statuses:
      STALE    - previous image has the same latest Intraday Trend time.
      COMPARE  - nearest earlier distinct internal time was found.
      BASELINE - no earlier distinct internal time exists for the same day.
    """
    if not instrument:
        return None, None, "BASELINE"

    current_datetime = parse_iso_datetime(current_captured_at)
    current_date = current_datetime.date()
    current_seconds = seconds_from_iso_clock(current_captured_at)

    candidates = connection.execute(
        """
        SELECT * FROM snapshots
        WHERE id < ?
          AND instrument = ?
          AND COALESCE(expiry, '') = ?
          AND row_count > 0
        ORDER BY id DESC
        LIMIT 250
        """,
        (current_snapshot_id, instrument, expiry or ""),
    ).fetchall()

    nearest_previous: tuple[int, sqlite3.Row] | None = None

    for candidate in candidates:
        try:
            candidate_datetime = parse_iso_datetime(
                str(candidate["captured_at"])
            )
        except (TypeError, ValueError):
            continue

        # Never compare one trading date with another.
        if candidate_datetime.date() != current_date:
            continue

        candidate_seconds = seconds_from_iso_clock(
            str(candidate["captured_at"])
        )

        if candidate_seconds == current_seconds:
            return candidate, 0, "STALE"

        elapsed_seconds = current_seconds - candidate_seconds
        if elapsed_seconds <= 0:
            continue

        if nearest_previous is None or elapsed_seconds < nearest_previous[0]:
            nearest_previous = (elapsed_seconds, candidate)

    if nearest_previous is None:
        return None, None, "BASELINE"

    elapsed_seconds, selected = nearest_previous
    return selected, elapsed_seconds, "COMPARE"

def safe_delta(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    return current - previous


def max_oi_strike(rows: Iterable[StrikeRow], field_name: str) -> float | None:
    usable = [row for row in rows if getattr(row, field_name) is not None]
    if not usable:
        return None
    return max(usable, key=lambda row: float(getattr(row, field_name))).strike


def calculate_row_score(
    diff: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> tuple[int, int, int, list[str]]:
    settings = config or DEFAULT_CONFIG
    premium_score = 0
    oi_score = 0
    reasons: list[str] = []

    call_oi_delta = diff["call_oi_delta"]
    put_oi_delta = diff["put_oi_delta"]
    call_ltp_delta = diff["call_ltp_delta"]
    put_ltp_delta = diff["put_ltp_delta"]

    previous_oi_total = abs(diff.get("previous_call_oi") or 0) + abs(diff.get("previous_put_oi") or 0)
    oi_threshold = max(
        1.0,
        previous_oi_total
        * float(settings.get("prediction_oi_change_ratio", 0.01)),
    )

    if call_oi_delta is not None and put_oi_delta is not None:
        pressure = put_oi_delta - call_oi_delta
        change_pressure: float | None = None
        if (
            diff.get("call_change_oi_delta") is not None
            and diff.get("put_change_oi_delta") is not None
        ):
            change_pressure = (
                diff["put_change_oi_delta"]
                - diff["call_change_oi_delta"]
            )

        oi_columns_disagree = (
            change_pressure is not None
            and abs(change_pressure) > oi_threshold
            and pressure * change_pressure < 0
        )
        if oi_columns_disagree:
            reasons.append("OI columns disagreed, so the OI vote was ignored")
        elif pressure > oi_threshold:
            oi_score = int(settings.get("prediction_oi_weight", 1))
            reasons.append("put OI strengthened relative to call OI")
        elif pressure < -oi_threshold:
            oi_score = -int(settings.get("prediction_oi_weight", 1))
            reasons.append("call OI strengthened relative to put OI")

    previous_ltp = max(abs(diff.get("previous_call_ltp") or 0), abs(diff.get("previous_put_ltp") or 0))
    ltp_threshold = max(
        float(settings.get("prediction_ltp_change_absolute", 0.5)),
        previous_ltp
        * float(settings.get("prediction_ltp_change_ratio", 0.005)),
    )
    premium_weight = int(settings.get("prediction_premium_weight", 2))

    if call_ltp_delta is not None and put_ltp_delta is not None:
        if call_ltp_delta > ltp_threshold and put_ltp_delta < -ltp_threshold:
            premium_score = premium_weight
            reasons.append("call premium rose while put premium fell")
        elif put_ltp_delta > ltp_threshold and call_ltp_delta < -ltp_threshold:
            premium_score = -premium_weight
            reasons.append("put premium rose while call premium fell")

    return premium_score + oi_score, premium_score, oi_score, reasons


def compare_snapshots(
    previous_rows: dict[float, StrikeRow],
    current_rows: dict[float, StrikeRow],
    config: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str, int, list[str]]:
    settings = config or DEFAULT_CONFIG
    all_common_strikes = sorted(set(previous_rows) & set(current_rows))
    minimum_row_confidence = float(
        settings.get("prediction_min_row_confidence", 55)
    )
    common_strikes = [
        strike
        for strike in all_common_strikes
        if min(
            previous_rows[strike].confidence,
            current_rows[strike].confidence,
        )
        >= minimum_row_confidence
    ]
    differences: list[dict[str, Any]] = []
    row_scores: list[int] = []
    premium_scores: list[int] = []
    oi_scores: list[int] = []
    explanations: list[str] = []

    excluded_count = len(all_common_strikes) - len(common_strikes)
    if excluded_count:
        explanations.append(
            f"Excluded {excluded_count} low-confidence matching strike row(s)."
        )

    for strike in common_strikes:
        previous = previous_rows[strike]
        current = current_rows[strike]
        diff: dict[str, Any] = {
            "strike": strike,
            "previous_call_oi": previous.call_oi,
            "current_call_oi": current.call_oi,
            "call_oi_delta": safe_delta(current.call_oi, previous.call_oi),
            "previous_call_change_oi": previous.call_change_oi,
            "current_call_change_oi": current.call_change_oi,
            "call_change_oi_delta": safe_delta(current.call_change_oi, previous.call_change_oi),
            "previous_call_ltp": previous.call_ltp,
            "current_call_ltp": current.call_ltp,
            "call_ltp_delta": safe_delta(current.call_ltp, previous.call_ltp),
            "previous_put_ltp": previous.put_ltp,
            "current_put_ltp": current.put_ltp,
            "put_ltp_delta": safe_delta(current.put_ltp, previous.put_ltp),
            "previous_put_change_oi": previous.put_change_oi,
            "current_put_change_oi": current.put_change_oi,
            "put_change_oi_delta": safe_delta(current.put_change_oi, previous.put_change_oi),
            "previous_put_oi": previous.put_oi,
            "current_put_oi": current.put_oi,
            "put_oi_delta": safe_delta(current.put_oi, previous.put_oi),
        }
        diff["pressure_delta"] = (
            diff["put_oi_delta"] - diff["call_oi_delta"]
            if diff["put_oi_delta"] is not None and diff["call_oi_delta"] is not None
            else None
        )
        score, premium_score, oi_score, reasons = calculate_row_score(
            diff,
            settings,
        )
        diff["row_score"] = score
        diff["premium_score"] = premium_score
        diff["oi_score"] = oi_score
        diff["interpretation"] = "; ".join(reasons) if reasons else "small or mixed change"
        differences.append(diff)
        if score:
            row_scores.append(score)
        if premium_score:
            premium_scores.append(premium_score)
        if oi_score:
            oi_scores.append(oi_score)

    minimum_common_strikes = int(
        settings.get("prediction_min_common_strikes", 3)
    )
    if len(common_strikes) < minimum_common_strikes:
        return differences, "UNCONFIRMED", 0, [
            *explanations,
            (
                f"Only {len(common_strikes)} reliable common strikes were "
                "detected; layout calibration may be required."
            ),
        ]

    previous_put_wall = max_oi_strike(previous_rows.values(), "put_oi")
    current_put_wall = max_oi_strike(current_rows.values(), "put_oi")
    previous_call_wall = max_oi_strike(previous_rows.values(), "call_oi")
    current_call_wall = max_oi_strike(current_rows.values(), "call_oi")

    if previous_put_wall is not None and current_put_wall is not None:
        shift = current_put_wall - previous_put_wall
        explanations.append(f"Put-OI concentration proxy: {previous_put_wall:g} -> {current_put_wall:g} ({shift:+g}).")

    if previous_call_wall is not None and current_call_wall is not None:
        shift = current_call_wall - previous_call_wall
        explanations.append(f"Call-OI concentration proxy: {previous_call_wall:g} -> {current_call_wall:g} ({shift:+g}).")

    minimum_directional_rows = int(
        settings.get("prediction_min_directional_rows", 2)
    )
    required_consensus = float(
        settings.get("prediction_consensus_ratio", 0.67)
    )

    # Premium movement is the closest available proxy for actual underlying
    # direction in these screenshots. OI is positioning data and may oppose
    # the immediate move, so it confirms or weakens a call but cannot overrule
    # a multi-row premium consensus.
    if len(premium_scores) >= minimum_directional_rows:
        bullish_count = sum(score > 0 for score in premium_scores)
        bearish_count = sum(score < 0 for score in premium_scores)
        dominant_count = max(bullish_count, bearish_count)
        consensus = dominant_count / len(premium_scores)
        if consensus >= required_consensus:
            direction = (
                "BULLISH_BIAS"
                if bullish_count > bearish_count
                else "BEARISH_BIAS"
            )
            confidence = round(consensus * 100)
        else:
            direction = "NEUTRAL"
            confidence = 0
        explanations.append(
            "Premium-direction rows: "
            f"bullish={bullish_count}, bearish={bearish_count}."
        )
    else:
        # With no reliable premium consensus, require stronger OI-only
        # agreement. This prevents positioning noise from being presented as
        # an immediate up/down prediction.
        bullish_count = sum(score > 0 for score in oi_scores)
        bearish_count = sum(score < 0 for score in oi_scores)
        oi_directional_count = bullish_count + bearish_count
        oi_required_rows = max(3, minimum_directional_rows)
        oi_required_consensus = max(0.75, required_consensus)
        consensus = (
            max(bullish_count, bearish_count) / oi_directional_count
            if oi_directional_count
            else 0
        )
        if (
            oi_directional_count >= oi_required_rows
            and consensus >= oi_required_consensus
        ):
            direction = (
                "BULLISH_BIAS"
                if bullish_count > bearish_count
                else "BEARISH_BIAS"
            )
            confidence = round(consensus * 100)
        else:
            direction = "NEUTRAL"
            confidence = 0
        explanations.append(
            "OI-direction rows: "
            f"bullish={bullish_count}, bearish={bearish_count}."
        )

    confidence = min(90, confidence)

    explanations.append(
        f"Directional strike rows: bullish={sum(score > 0 for score in row_scores)}, "
        f"bearish={sum(score < 0 for score in row_scores)}, mixed={len(common_strikes) - len(row_scores)}."
    )
    return differences, direction, confidence, explanations


def format_number(value: float | None) -> str:
    if value is None:
        return "n/a"
    if abs(value) >= 10_000_000:
        return f"{value / 10_000_000:.2f}Cr"
    if abs(value) >= 100_000:
        return f"{value / 100_000:.2f}L"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}K"
    if float(value).is_integer():
        return f"{value:.0f}"
    return f"{value:.2f}"


def format_exact_number(value: float | None) -> str:
    """Render a price or strike without K/L/Cr abbreviation or rounding."""

    if value is None:
        return "n/a"
    if float(value).is_integer():
        return f"{value:.0f}"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def format_decimal(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def render_ascii_table(
    headers: list[str],
    rows: list[list[Any]],
    maximum_widths: list[int] | None = None,
) -> list[str]:
    """Render a wrapped fixed-width table that remains aligned in TXT files."""

    if not headers:
        return []
    column_count = len(headers)
    normalized_rows = [
        [
            "n/a" if value is None or str(value).strip() == "" else str(value)
            for value in row[:column_count]
        ]
        + ["n/a"] * max(0, column_count - len(row))
        for row in rows
    ]
    widths: list[int] = []
    for index, header in enumerate(headers):
        natural_width = max(
            [len(str(header))]
            + [len(row[index]) for row in normalized_rows],
        )
        if maximum_widths and index < len(maximum_widths):
            natural_width = min(natural_width, maximum_widths[index])
        widths.append(max(3, natural_width))

    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"

    def render_row(values: list[str]) -> list[str]:
        wrapped_columns: list[list[str]] = []
        for value, width in zip(values, widths):
            wrapped = textwrap.wrap(
                value,
                width=width,
                break_long_words=True,
                break_on_hyphens=False,
            ) or [""]
            wrapped_columns.append(wrapped)
        height = max(len(column) for column in wrapped_columns)
        output: list[str] = []
        for line_index in range(height):
            cells = [
                (
                    column[line_index]
                    if line_index < len(column)
                    else ""
                ).ljust(width)
                for column, width in zip(wrapped_columns, widths)
            ]
            output.append("| " + " | ".join(cells) + " |")
        return output

    output = [border]
    output.extend(render_row([str(header) for header in headers]))
    output.append(border)
    if normalized_rows:
        for row in normalized_rows:
            output.extend(render_row(row))
            output.append(border)
    else:
        output.extend(render_row(["n/a"] + [""] * (column_count - 1)))
        output.append(border)
    return output


def format_percent(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.1f}%"


def report_day_folder_name(
    captured_at: str | datetime,
    config: dict[str, Any],
) -> str:
    """Return the report trading-day folder in configured market time."""

    parsed = (
        parse_iso_datetime(captured_at)
        if isinstance(captured_at, str)
        else captured_at
    )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    offset = timezone(
        timedelta(
            minutes=int(
                config.get(
                    "snapshot_timezone_offset_minutes",
                    config.get("report_timezone_offset_minutes", 330),
                )
            )
        )
    )
    return parsed.astimezone(offset).strftime("%Y-%m-%d")


def report_day_directory(
    captured_at: str | datetime,
    config: dict[str, Any],
) -> Path:
    """Create and return the market-day directory for snapshot reports."""

    directory = REPORT_DIR / report_day_folder_name(captured_at, config)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def write_diff_csv(
    snapshot_id: int,
    differences: list[dict[str, Any]],
    output_directory: Path | None = None,
) -> Path | None:
    if not differences:
        return None
    output_directory = output_directory or REPORT_DIR
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / f"snapshot_{snapshot_id}_strike_diff.csv"
    with output_path.open("w", newline="", encoding="utf-8-sig") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=list(differences[0].keys()))
        writer.writeheader()
        writer.writerows(differences)
    return output_path


def format_easy_time(value: str, config: dict[str, Any]) -> str:
    """Convert Telegram timestamps to the configured market timezone."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        offset = timezone(timedelta(minutes=int(config["report_timezone_offset_minutes"])))
        label = str(config.get("report_timezone_label", "")).strip()
        formatted = parsed.astimezone(offset).strftime("%d-%b-%Y %H:%M:%S")
        return f"{formatted} {label}".strip()
    except (ValueError, TypeError, KeyError):
        return value


def confidence_label(confidence: int) -> str:
    if confidence >= 75:
        return "High"
    if confidence >= 50:
        return "Medium"
    if confidence > 0:
        return "Low"
    return "Not available"


def data_quality(current_rows: dict[float, StrikeRow], minimum_confidence: float) -> tuple[str, int]:
    if not current_rows:
        return "Low", 0

    average = round(statistics.mean(row.confidence for row in current_rows.values()))
    if len(current_rows) >= 7 and average >= max(65, minimum_confidence):
        return "Good", average
    if len(current_rows) >= 3 and average >= minimum_confidence:
        return "Medium", average
    return "Low", average


def direction_title(direction: str) -> tuple[str, str]:
    labels = {
        "BULLISH_BIAS": ("MARKET IS LEANING UP", "🟢"),
        "BEARISH_BIAS": ("MARKET IS LEANING DOWN", "🔴"),
        "NEUTRAL": ("MARKET IS SIDEWAYS / UNCLEAR", "🟡"),
        "UNCONFIRMED": ("DIRECTION COULD NOT BE CONFIRMED", "⚪"),
        "BASELINE": ("FIRST IMAGE SAVED", "🔵"),
        "STALE": ("NO FRESH MARKET DATA", "⏸️"),
        "BULLISH_CONFIRMED": ("UPSIDE CONFIRMED", "[UP]"),
        "BEARISH_CONFIRMED": ("DOWNSIDE CONFIRMED", "[DOWN]"),
        "BULLISH_STRENGTHENING": ("UPSIDE IS STRENGTHENING", "[UP]"),
        "BEARISH_STRENGTHENING": ("DOWNSIDE IS STRENGTHENING", "[DOWN]"),
        "BULLISH_WEAKENING": ("CURRENTLY UP, UPSIDE IS WEAKENING", "[UP]"),
        "BEARISH_WEAKENING": ("CURRENTLY DOWN, DOWNSIDE IS WEAKENING", "[DOWN]"),
        "BULLISH_DEVELOPING": ("POSSIBLE UPSIDE IS DEVELOPING", "[WATCH]"),
        "BEARISH_DEVELOPING": ("POSSIBLE DOWNSIDE IS DEVELOPING", "[WATCH]"),
        "REVERSAL_WATCH": ("REVERSAL CONDITIONS ARE DEVELOPING", "[WATCH]"),
        "NEUTRAL_TRANSITION": ("NO CONFIRMED 45-MINUTE DIRECTION", "[FLAT]"),
    }
    return labels.get(direction, (direction.replace("_", " "), "⚪"))


def movement_sentence(name: str, previous_value: float | None, current_value: float | None) -> str | None:
    if current_value is None:
        return None
    if previous_value is None:
        return f"{name}: {current_value:g}"

    shift = current_value - previous_value
    if shift > 0:
        return f"{name} moved UP: {previous_value:g} → {current_value:g}"
    if shift < 0:
        return f"{name} moved DOWN: {previous_value:g} → {current_value:g}"
    return f"{name} stayed at {current_value:g}"


def simple_meaning(
    direction: str,
    support: float | None,
    resistance: float | None,
    horizon_minutes: int = 45,
) -> list[str]:
    support_text = f"{support:g}" if support is not None else "the support area"
    resistance_text = f"{resistance:g}" if resistance is not None else "the resistance area"

    if direction == "BULLISH_BIAS":
        return [
            "Buyers currently look stronger than sellers.",
            f"This upward view becomes weaker if price falls below {support_text}.",
        ]
    if direction == "BEARISH_BIAS":
        return [
            "Sellers currently look stronger than buyers.",
            f"This downward view becomes weaker if price rises above {resistance_text}.",
        ]
    if direction == "NEUTRAL":
        return [
            "Bullish and bearish changes are mixed.",
            "There is no clear direction yet; waiting for another confirmed update is safer.",
        ]
    if direction == "UNCONFIRMED":
        return [
            f"A complete validated {horizon_minutes}-minute window was not available.",
            "Do not use this update for a market decision; wait for more rows or check the OCR overlay.",
        ]
    meanings = {
        "BULLISH_CONFIRMED": [
            "The current Call/Put balance and confirming signals support an upward view.",
            f"The view weakens if price loses {support_text}.",
        ],
        "BEARISH_CONFIRMED": [
            "The current Call/Put balance and confirming signals support a downward view.",
            f"The view weakens if price clears {resistance_text}.",
        ],
        "BULLISH_STRENGTHENING": [
            "The market is currently bullish and the 45-minute flow is reinforcing it.",
        ],
        "BEARISH_STRENGTHENING": [
            "The market is currently bearish and the 45-minute flow is reinforcing it.",
        ],
        "BULLISH_WEAKENING": [
            f"The market is still bullish, but the last {horizon_minutes} minutes show weakening upside pressure.",
        ],
        "BEARISH_WEAKENING": [
            f"The market is still bearish, but the last {horizon_minutes} minutes show easing downside pressure.",
            "Weakening downside is not the same as a confirmed upward reversal.",
        ],
        "BULLISH_DEVELOPING": [
            "Upward conditions are developing, but confirmation is not complete yet.",
        ],
        "BEARISH_DEVELOPING": [
            "Downward conditions are developing, but confirmation is not complete yet.",
        ],
        "REVERSAL_WATCH": [
            "Momentum opposes the current Call/Put balance; wait for confirmation before calling a reversal.",
        ],
        "NEUTRAL_TRANSITION": [
            "The combined evidence does not clear the directional threshold.",
        ],
    }
    if direction in meanings:
        return meanings[direction]
    return ["The next image will be compared with this one."]


def strongest_change_lines(
    differences: list[dict[str, Any]],
    maximum_rows: int,
) -> list[str]:
    meaningful = [item for item in differences if item.get("row_score")]
    ranked = sorted(
        meaningful,
        key=lambda item: abs(item.get("pressure_delta") or 0),
        reverse=True,
    )[:maximum_rows]

    output: list[str] = []
    for item in ranked:
        label = "bullish change" if item["row_score"] > 0 else "bearish change"
        output.append(
            f"Strike {item['strike']:g}: {label} "
            f"(Call OI {format_number(item['call_oi_delta'])}, "
            f"Put OI {format_number(item['put_oi_delta'])})"
        )
    return output


def classify_oi_change_pair(
    call_change: float,
    put_change: float,
) -> tuple[str, str]:
    """Describe two signed Call/Put OI changes without hiding two-sided flow."""

    larger = max(abs(call_change), abs(put_change))
    smaller = min(abs(call_change), abs(put_change))
    two_sided_ratio = smaller / larger if larger else 0
    if call_change > 0 and put_change > 0:
        if two_sided_ratio >= 0.60:
            if call_change > put_change:
                return (
                    "TWO-SIDED (CALL TILT)",
                    "Two-sided OI buildup; Call is slightly stronger",
                )
            if put_change > call_change:
                return (
                    "TWO-SIDED (PUT TILT)",
                    "Two-sided OI buildup; Put is slightly stronger",
                )
            return "BALANCED TWO-SIDED", "Balanced two-sided OI buildup"
        if call_change > put_change:
            return "CALL", "Call-dominant OI buildup / resistance pressure"
        return "PUT", "Put-dominant OI buildup / support pressure"
    if call_change > 0 and put_change < 0:
        return (
            "CALL BUILD / PUT UNWIND",
            "Call buildup with Put unwinding / resistance stronger, support weaker",
        )
    if put_change > 0 and call_change < 0:
        return (
            "PUT BUILD / CALL UNWIND",
            "Put buildup with Call unwinding / support stronger, resistance weaker",
        )
    if call_change < 0 and put_change < 0:
        return "TWO-SIDED UNWIND", "Both Call and Put OI are unwinding"
    if call_change > 0:
        return "CALL", "Call OI buildup / resistance pressure"
    if put_change > 0:
        return "PUT", "Put OI buildup / support pressure"
    if call_change < 0:
        return "CALL UNWIND", "Call OI unwinding / resistance may weaken"
    if put_change < 0:
        return "PUT UNWIND", "Put OI unwinding / support may weaken"
    return "MIXED", "Mixed Call/Put OI activity"


def rank_strike_oi_movers(
    current_rows: Iterable[StrikeRow],
    differences: list[dict[str, Any]],
    maximum_rows: int,
) -> list[dict[str, Any]]:
    """Rank current Change-in-OI activity; this is not traded volume."""

    difference_by_strike = {
        float(item["strike"]): item for item in differences
    }
    movers: list[dict[str, Any]] = []
    for row in current_rows:
        call_change = row.call_change_oi or 0
        put_change = row.put_change_oi or 0
        activity = abs(call_change) + abs(put_change)
        difference = difference_by_strike.get(row.strike, {})
        total_oi_delta = sum(
            abs(value)
            for value in (
                difference.get("call_oi_delta"),
                difference.get("put_oi_delta"),
            )
            if value is not None
        )
        if activity <= 0 and total_oi_delta <= 0:
            continue

        dominance, interpretation = classify_oi_change_pair(
            call_change,
            put_change,
        )

        movers.append({
            "strike": row.strike,
            "call_change_oi": row.call_change_oi,
            "put_change_oi": row.put_change_oi,
            "oi_activity": activity,
            "net_put_minus_call": put_change - call_change,
            "dominance": dominance,
            "call_oi_delta": difference.get("call_oi_delta"),
            "put_oi_delta": difference.get("put_oi_delta"),
            "total_oi_delta": total_oi_delta,
            "interpretation": interpretation,
        })

    return sorted(
        movers,
        key=lambda item: (
            item["oi_activity"],
            item["total_oi_delta"],
        ),
        reverse=True,
    )[:maximum_rows]


def strike_oi_comparison_lines(
    strike_movers: list[dict[str, Any]],
    maximum_rows: int,
    current_snapshot_time: str,
    previous_snapshot_time: str | None = None,
) -> list[str]:
    """Format ranked Call/Put Change-in-OI comparisons for reports."""

    lines = [
        "",
        f"Strike Change-in-OI comparison (up to {maximum_rows}; "
        "not traded volume):",
    ]
    if not strike_movers:
        lines.append(
            "- No reliable strike Change-in-OI activity was available."
        )
        return lines

    lines.append(
        f"- Reliable strikes shown: {len(strike_movers)}; "
        "ranked by |Call ChgOI| + |Put ChgOI|."
    )
    for index, mover in enumerate(strike_movers, start=1):
        lines.append(
            f"- {index}. [{current_snapshot_time}] "
            f"Strike {mover['strike']:g}: "
            f"Call ChgOI={format_number(mover['call_change_oi'])}; "
            f"Put ChgOI={format_number(mover['put_change_oi'])}; "
            f"activity={format_number(mover['oi_activity'])}; "
            f"net Put-Call={format_number(mover['net_put_minus_call'])}; "
            f"dominance={mover['dominance']}; "
            f"{mover['interpretation']}."
        )
        if (
            mover.get("call_oi_delta") is not None
            or mover.get("put_oi_delta") is not None
        ):
            comparison_time = (
                f"{previous_snapshot_time} -> {current_snapshot_time}"
                if previous_snapshot_time
                else f"prior snapshot -> {current_snapshot_time}"
            )
            lines.append(
                f"  [{comparison_time}] Total OI change: "
                f"Call delta={format_number(mover.get('call_oi_delta'))}; "
                f"Put delta={format_number(mover.get('put_oi_delta'))}."
            )
    return lines


def load_rows_for_market_time(
    connection: sqlite3.Connection,
    current_snapshot_id: int,
    instrument: str | None,
    expiry: str | None,
    target_at: str,
    tolerance_seconds: int = 60,
) -> tuple[str | None, dict[float, StrikeRow]]:
    """Merge same-time option-chain images for one instrument and expiry."""

    if not instrument:
        return None, {}
    target_datetime = parse_iso_datetime(target_at)
    candidates = connection.execute(
        """
        SELECT id, captured_at
        FROM snapshots
        WHERE id <= ?
          AND instrument = ?
          AND COALESCE(expiry, '') = ?
          AND row_count > 0
        ORDER BY id DESC
        LIMIT 500
        """,
        (current_snapshot_id, instrument, expiry or ""),
    ).fetchall()

    timed_candidates: list[tuple[float, sqlite3.Row]] = []
    for candidate in candidates:
        try:
            candidate_datetime = parse_iso_datetime(
                str(candidate["captured_at"])
            )
        except (TypeError, ValueError):
            continue
        if candidate_datetime.date() != target_datetime.date():
            continue
        distance = abs(
            (candidate_datetime - target_datetime).total_seconds()
        )
        if distance <= tolerance_seconds:
            timed_candidates.append((distance, candidate))

    if not timed_candidates:
        return None, {}
    nearest_distance = min(item[0] for item in timed_candidates)
    selected = [
        item[1]
        for item in timed_candidates
        if item[0] == nearest_distance
    ]
    selected_time = str(selected[0]["captured_at"])
    merged: dict[float, StrikeRow] = {}
    for candidate in reversed(selected):
        for strike, row in load_rows(
            connection,
            int(candidate["id"]),
        ).items():
            existing = merged.get(strike)
            if existing is None or row.confidence >= existing.confidence:
                merged[strike] = row
    return selected_time, merged


def rank_timeframe_oi_movers(
    current_rows: dict[float, StrikeRow],
    previous_rows: dict[float, StrikeRow],
    maximum_rows: int,
) -> tuple[list[dict[str, Any]], int]:
    """Rank actual total-OI movement between the two timeframe endpoints."""

    common_strikes = sorted(set(current_rows) & set(previous_rows))
    movers: list[dict[str, Any]] = []
    for strike in common_strikes:
        current = current_rows[strike]
        previous = previous_rows[strike]
        call_delta = safe_delta(current.call_oi, previous.call_oi)
        put_delta = safe_delta(current.put_oi, previous.put_oi)
        if call_delta is None and put_delta is None:
            continue
        call_value = call_delta or 0.0
        put_value = put_delta or 0.0
        activity = abs(call_value) + abs(put_value)
        if activity <= 0:
            continue
        dominance, interpretation = classify_oi_change_pair(
            call_value,
            put_value,
        )
        movers.append({
            "strike": strike,
            "call_change_oi": call_delta,
            "put_change_oi": put_delta,
            "oi_activity": activity,
            "net_put_minus_call": put_value - call_value,
            "dominance": dominance,
            "interpretation": interpretation,
        })

    movers.sort(
        key=lambda item: (
            item["oi_activity"],
            abs(item["net_put_minus_call"]),
        ),
        reverse=True,
    )
    return movers[:maximum_rows], len(common_strikes)


def build_timeframe_strike_comparisons(
    connection: sqlite3.Connection,
    current_snapshot_id: int,
    instrument: str | None,
    expiry: str | None,
    captured_at: str,
    timeframe_minutes: Iterable[int],
    current_rows: dict[float, StrikeRow],
    config: dict[str, Any],
) -> dict[int, TimeframeStrikeComparison]:
    """Build independent 15m/30m/45m OI comparisons at exact endpoints."""

    current_time, merged_current = load_rows_for_market_time(
        connection,
        current_snapshot_id,
        instrument,
        expiry,
        captured_at,
    )
    if not merged_current:
        merged_current = current_rows
        current_time = captured_at
    maximum_rows = int(config.get("strike_movers_rows", 15))
    current_datetime = parse_iso_datetime(captured_at)
    results: dict[int, TimeframeStrikeComparison] = {}
    for horizon_minutes in timeframe_minutes:
        target_at = (
            current_datetime - timedelta(minutes=horizon_minutes)
        ).isoformat()
        previous_time, previous_rows = load_rows_for_market_time(
            connection,
            current_snapshot_id,
            instrument,
            expiry,
            target_at,
        )
        if not previous_rows:
            movers: list[dict[str, Any]] = []
            common_count = 0
            status = "NO COMPARABLE SNAPSHOT"
        else:
            movers, common_count = rank_timeframe_oi_movers(
                merged_current,
                previous_rows,
                maximum_rows,
            )
            status = "COMPLETE" if common_count else "NO COMMON STRIKES"
        results[horizon_minutes] = TimeframeStrikeComparison(
            horizon_minutes=horizon_minutes,
            current_time=format_snapshot_clock(
                current_time or captured_at,
                config,
            ),
            previous_time=(
                format_snapshot_clock(previous_time, config)
                if previous_time
                else None
            ),
            movers=movers,
            current_strike_count=len(merged_current),
            common_strike_count=common_count,
            status=status,
        )
    return results


def timeframe_strike_comparison_lines(
    comparison: TimeframeStrikeComparison,
    maximum_rows: int,
) -> list[str]:
    """Format one timeframe's independently ranked OI changes as TXT tables."""

    previous_time = comparison.previous_time or "n/a"
    lines = [
        "",
        (
            f"{comparison.horizon_minutes}-MINUTE STRIKE TOTAL-OI CHANGE "
            f"RANKING (up to {maximum_rows}; not traded volume)"
        ),
        (
            f"Window: {previous_time} -> {comparison.current_time}; "
            f"status={comparison.status}; current strikes="
            f"{comparison.current_strike_count}; common strikes="
            f"{comparison.common_strike_count}."
        ),
    ]
    timing = f"{previous_time} -> {comparison.current_time}"
    table_rows = [
        [
            index,
            timing,
            f"{mover['strike']:g}",
            format_number(mover.get("call_change_oi")),
            format_number(mover.get("put_change_oi")),
            format_number(mover.get("oi_activity")),
            format_number(mover.get("net_put_minus_call")),
            mover.get("dominance") or "n/a",
        ]
        for index, mover in enumerate(comparison.movers, start=1)
    ]
    if not table_rows:
        table_rows = [["n/a", timing, "n/a", "n/a", "n/a", "n/a", "n/a", "n/a"]]
    lines.extend(render_ascii_table(
        [
            "Rank", "From -> To", "Strike", "Call OI d", "Put OI d",
            "Activity", "Net P-C", "Dominance",
        ],
        table_rows,
        [4, 25, 8, 11, 11, 10, 10, 23],
    ))
    interpretation_rows = [
        [index, f"{mover['strike']:g}", mover.get("interpretation") or "n/a"]
        for index, mover in enumerate(comparison.movers, start=1)
    ]
    if interpretation_rows:
        lines.extend(["", "OI interpretation:"])
        lines.extend(render_ascii_table(
            ["Rank", "Strike", "Interpretation"],
            interpretation_rows,
            [4, 8, 72],
        ))
    return lines


def option_probability_lines(
    results: list[OptionProbabilityResult],
    horizon_minutes: int,
    config: dict[str, Any],
    gate_audit_limit: int | None = None,
    compact: bool = False,
) -> list[str]:
    """Render calibrated premium-P&L results without presenting certainty."""

    minimum_samples = int(config.get("option_probability_min_samples", 100))
    minimum_days = int(config.get("option_probability_min_trading_days", 20))
    cost_pct = 100 * float(
        config.get("option_probability_round_trip_cost_pct", 0.005)
    )
    lines = [
        "",
        f"{horizon_minutes}-MINUTE OPTION PREMIUM PROBABILITY (HISTORICAL)",
        (
            "Win means net premium P&L > 0 after an assumed "
            f"{cost_pct:.2f}% round-trip cost. Probability is shown only at "
            f"N >= {minimum_samples} independent timestamps across at least "
            f"{minimum_days} trading days; it is not a guarantee."
        ),
    ]
    if compact:
        sell_results = [
            result
            for result in results
            if result.evaluated_action == "SELL"
        ][:2]
        compact_rows: list[list[Any]] = []
        for result in sell_results:
            enough_samples = (
                result.sample_count >= minimum_samples
                and result.trading_day_count >= minimum_days
            )
            probability_text = (
                f"{100 * result.win_probability:.1f}% / "
                f"{100 * result.probability_low:.1f}%"
                if enough_samples
                and result.win_probability is not None
                and result.probability_low is not None
                else "n/a"
            )
            failed = [
                gate
                for gate, status in result.gate_statuses.items()
                if status != "PASS"
            ]
            compact_rows.append([
                f"{result.strike:g}",
                result.option_type,
                format_decimal(result.entry_ltp),
                f"{result.sample_count}/{result.trading_day_count}",
                probability_text,
                (
                    f"{result.expected_value_pct:+.2f}%"
                    if enough_samples
                    and result.expected_value_pct is not None
                    else "n/a"
                ),
                (
                    f"{result.hedge_strike:g}"
                    if result.hedge_strike is not None
                    else "n/a"
                ),
                result.model_signal,
                ", ".join(failed) if failed else "none",
            ])
        if not compact_rows:
            compact_rows = [["n/a"] * 9]
        lines.extend(render_ascii_table(
            [
                "Strike", "Type", "LTP", "N/Days", "Win/Low", "EV",
                "Hedge", "Signal", "Failed gates",
            ],
            compact_rows,
            [8, 4, 8, 8, 14, 9, 8, 22, 52],
        ))
        lines.extend([
            "Only the two leading directional sell rows are shown here. Full "
            "probability evidence and every gate are in the individual "
            f"{horizon_minutes}-minute TXT report.",
            "A signal requires every gate to pass and a defined-risk hedge.",
        ])
        return lines

    numeric_rows: list[list[Any]] = []
    reason_rows: list[list[Any]] = []
    for result in results:
        enough_samples = (
            result.sample_count >= minimum_samples
            and result.trading_day_count >= minimum_days
        )
        probability = (
            f"{100 * result.win_probability:.1f}%"
            if enough_samples and result.win_probability is not None
            else "n/a"
        )
        probability_range = (
            f"{100 * result.probability_low:.1f}% - "
            f"{100 * result.probability_high:.1f}%"
            if enough_samples
            and result.probability_low is not None
            and result.probability_high is not None
            else "n/a"
        )
        numeric_rows.append([
            f"{result.strike:g}",
            result.option_type,
            format_decimal(result.entry_ltp),
            result.moneyness,
            result.evaluated_action,
            probability,
            probability_range,
            result.sample_count,
            result.trading_day_count,
            (
                f"{result.expected_value_pct:+.2f}%"
                if enough_samples and result.expected_value_pct is not None
                else "n/a"
            ),
            result.model_signal,
        ])
        reason_rows.append([
            f"{result.strike:g}",
            result.option_type,
            (
                f"{result.average_win_pct:+.2f}%"
                if enough_samples and result.average_win_pct is not None
                else "n/a"
            ),
            (
                f"{result.average_loss_pct:+.2f}%"
                if enough_samples and result.average_loss_pct is not None
                else "n/a"
            ),
            result.data_quality,
            result.reason,
        ])
    if not numeric_rows:
        numeric_rows = [["n/a"] * 11]
    lines.extend(render_ascii_table(
        [
            "Strike", "Type", "LTP", "Mny", "Tested", "Win %",
            "95% range", "N", "Days", "EV", "Model signal",
        ],
        numeric_rows,
        [8, 4, 8, 4, 8, 7, 15, 5, 5, 9, 38],
    ))
    if reason_rows:
        lines.extend(["", "Probability evidence and filters:"])
        lines.extend(render_ascii_table(
            [
                "Strike", "Type", "Avg win", "Avg loss", "Worst", "Tail5",
                "OCR", "Reason",
            ],
            [
                [
                    *row[:4],
                    (
                        f"{result.worst_loss_pct:+.2f}%"
                        if result.worst_loss_pct is not None else "n/a"
                    ),
                    (
                        f"{result.tail_loss_pct:+.2f}%"
                        if result.tail_loss_pct is not None else "n/a"
                    ),
                    *row[4:],
                ]
                for row, result in zip(reason_rows, results)
            ],
            [8, 4, 9, 9, 9, 9, 6, 66],
        ))
    sell_results = [
        result for result in results if result.evaluated_action == "SELL"
    ]
    total_sell_results = len(sell_results)
    if gate_audit_limit is not None:
        sell_results = sell_results[:max(0, gate_audit_limit)]
    for result in sell_results:
        hedge_text = (
            f"BUY {result.hedge_strike:g} {result.option_type} @ "
            f"{result.hedge_ltp:.2f}"
            if result.hedge_strike is not None
            and result.hedge_ltp is not None
            else "n/a"
        )
        lines.extend([
            "",
            (
                f"Strict gate audit: SELL {result.strike:g} "
                f"{result.option_type}; hedge={hedge_text}"
            ),
        ])
        lines.extend(render_ascii_table(
            ["Gate", "Status", "Evidence"],
            [
                [
                    gate,
                    status,
                    result.gate_details.get(gate, "n/a"),
                ]
                for gate, status in result.gate_statuses.items()
            ],
            [23, 6, 78],
        ))
    if total_sell_results and not sell_results:
        lines.append(
            "Full per-strike gate audits are stored in the individual "
            f"{horizon_minutes}-minute TXT report."
        )
    lines.extend([
        "A sell signal is issued only when every displayed gate passes and a "
        "defined-risk hedge is available; naked option selling is rejected.",
        "Model output is statistical research, not personalized financial advice.",
    ])
    return lines


def estimate_support_resistance(
    current_rows: Iterable[StrikeRow],
    current_price: float | None,
) -> SupportResistanceEstimate:
    """Estimate OI-based levels from concentration, buildup, and unwinding."""

    rows = list(current_rows)
    if not rows:
        return SupportResistanceEstimate(
            None, None, None, None, None, None, 0,
            ["No reliable strike rows were available."],
        )

    support_rows = (
        [row for row in rows if row.strike <= current_price]
        if current_price is not None
        else rows
    )
    resistance_rows = (
        [row for row in rows if row.strike >= current_price]
        if current_price is not None
        else rows
    )
    def maximum_strike(
        candidates: list[StrikeRow],
        field_name: str,
    ) -> float | None:
        usable = [
            row
            for row in candidates
            if getattr(row, field_name) is not None
        ]
        if not usable:
            return None
        return max(
            usable,
            key=lambda row: float(getattr(row, field_name)),
        ).strike

    def strongest_positive(
        candidates: list[StrikeRow],
        field_name: str,
    ) -> float | None:
        usable = [
            row
            for row in candidates
            if (getattr(row, field_name) or 0) > 0
        ]
        if not usable:
            return None
        return max(
            usable,
            key=lambda row: float(getattr(row, field_name)),
        ).strike

    def strongest_negative(
        candidates: list[StrikeRow],
        field_name: str,
    ) -> float | None:
        usable = [
            row
            for row in candidates
            if (getattr(row, field_name) or 0) < 0
        ]
        if not usable:
            return None
        return min(
            usable,
            key=lambda row: float(getattr(row, field_name)),
        ).strike

    primary_support = maximum_strike(support_rows, "put_oi")
    primary_resistance = maximum_strike(resistance_rows, "call_oi")
    developing_support = strongest_positive(
        support_rows,
        "put_change_oi",
    )
    developing_resistance = strongest_positive(
        resistance_rows,
        "call_change_oi",
    )
    weakening_support = strongest_negative(
        support_rows,
        "put_change_oi",
    )
    weakening_resistance = strongest_negative(
        resistance_rows,
        "call_change_oi",
    )

    strikes = sorted({row.strike for row in rows})
    intervals = [
        current - previous
        for previous, current in zip(strikes, strikes[1:])
        if current > previous
    ]
    strike_interval = statistics.median(intervals) if intervals else 0

    def levels_agree(
        primary: float | None,
        developing: float | None,
    ) -> bool:
        if primary is None or developing is None:
            return False
        tolerance = max(strike_interval, 1)
        return abs(primary - developing) <= tolerance

    agreement_count = sum((
        levels_agree(primary_support, developing_support),
        levels_agree(primary_resistance, developing_resistance),
    ))
    average_confidence = statistics.mean(
        row.confidence for row in rows
    )
    coverage_score = min(20, len(rows) * 4)
    confidence = round(
        average_confidence * 0.60
        + coverage_score
        + agreement_count * 10
    )
    if primary_support is None or primary_resistance is None:
        confidence -= 15
    confidence = max(0, min(90, confidence))

    notes = [
        "Support uses Put OI; resistance uses Call OI.",
        "Developing levels use positive Change in OI.",
        "Negative Change in OI marks a potentially weakening level.",
    ]
    if current_price is not None and not support_rows:
        notes.append("No support strike was visible below current price.")
    if current_price is not None and not resistance_rows:
        notes.append("No resistance strike was visible above current price.")
    return SupportResistanceEstimate(
        primary_support=primary_support,
        developing_support=developing_support,
        weakening_support=weakening_support,
        primary_resistance=primary_resistance,
        developing_resistance=developing_resistance,
        weakening_resistance=weakening_resistance,
        confidence=confidence,
        notes=notes,
    )


def intraday_window_summary(
    intraday_times: list[str],
    config: dict[str, Any],
) -> list[str]:
    if not intraday_times:
        return []

    label = str(config.get("report_timezone_label", "")).strip()
    suffix = f" {label}" if label else ""
    lines = [
        "Intraday times read: " + ", ".join(intraday_times),
    ]

    if len(intraday_times) >= 2:
        try:
            first = parse_clock_word(intraday_times[0])
            second = parse_clock_word(intraday_times[1])
            oldest = parse_clock_word(intraday_times[-1])
            if first and second:
                first_seconds = first[0] * 3600 + first[1] * 60 + first[2]
                second_seconds = second[0] * 3600 + second[1] * 60 + second[2]
                interval = max(0, first_seconds - second_seconds)
                lines.append(
                    f"Previous row inside image: {intraday_times[1]}{suffix}"
                )
                lines.append(
                    f"Table interval: {elapsed_text(interval)}"
                )
            if first and oldest:
                oldest_seconds = oldest[0] * 3600 + oldest[1] * 60 + oldest[2]
                covered = max(0, first_seconds - oldest_seconds)
                lines.append(
                    f"{len(intraday_times)}-row range: "
                    f"{intraday_times[-1]} to {intraday_times[0]}{suffix} "
                    f"({elapsed_text(covered)})"
                )
        except (TypeError, ValueError):
            pass

    return lines


def build_report(
    snapshot_id: int,
    captured_at: str,
    instrument: str | None,
    current_rows: dict[float, StrikeRow],
    previous_snapshot: sqlite3.Row | None,
    previous_rows: dict[float, StrikeRow],
    differences: list[dict[str, Any]],
    direction: str,
    confidence: int,
    explanations: list[str],
    comparison_seconds: int | None,
    comparison_status: str,
    intraday_times: list[str],
    intraday_signals: IntradaySignalResult,
    config: dict[str, Any],
) -> str:
    """Build a short, plain-language report suitable for Telegram."""

    quality, average_ocr_confidence = data_quality(
        current_rows,
        float(config["minimum_summary_row_confidence"]),
    )
    title, icon = direction_title(direction)
    current_support = max_oi_strike(current_rows.values(), "put_oi")
    current_resistance = max_oi_strike(current_rows.values(), "call_oi")

    lines = [
        f"📊 {instrument or 'MARKET'} EASY UPDATE",
        f"Latest time inside image: {format_snapshot_clock(captured_at, config)}",
        "Time source: Intraday Trend → Time column",
    ]
    lines.extend(intraday_window_summary(intraday_times, config))
    if intraday_signals.option_signal:
        lines.append(
            "Latest Option Signal: "
            f"{intraday_signals.option_signal} "
            f"(OCR {intraday_signals.option_confidence:.0f}%)"
        )
    if intraday_signals.vwap_signal:
        lines.append(
            "Latest VWAP Signal: "
            f"{intraday_signals.vwap_signal} "
            f"(OCR {intraday_signals.vwap_confidence:.0f}%)"
        )
    lines.extend(["", f"Overall view: {icon} {title}"])

    if comparison_status == "STALE":
        lines.extend([
            "",
            "No fresh market interval was detected.",
            f"The latest Intraday Trend time is still {format_snapshot_clock(captured_at, config)}.",
            "The source image may have been reposted, or the market/source may be closed.",
            "No new strike-direction comparison was made from this duplicate market time.",
            "",
            f"Strikes read: {len(current_rows)}",
            f"Data quality: {quality} (OCR {average_ocr_confidence}%)",
            "",
            "⚠️ This is an OCR-based summary, not a trade order.",
        ])
        return "\n".join(lines)

    if previous_snapshot is None:
        baseline_explanation = (
            "The explicit latest Option Signal is used for the current "
            "direction while this image is saved as the comparison baseline."
            if intraday_signals.option_signal
            else "This image is saved as the internal-time baseline."
        )
        lines.extend([
            "",
            f"No earlier distinct {instrument or 'same-market'} Intraday Trend time was available for this trading day.",
            baseline_explanation,
            "",
            f"Strikes read: {len(current_rows)}",
            f"Data quality: {quality} (OCR {average_ocr_confidence}%)",
            "",
            "⚠️ This is an OCR-based summary, not a trade order.",
        ])
        return "\n".join(lines)

    lines.append(
        "Compared with previous image time: "
        f"{format_snapshot_clock(str(previous_snapshot['captured_at']), config)}"
    )
    if comparison_seconds is not None:
        lines.append(
            f"Actual internal-time gap: {elapsed_text(comparison_seconds)}"
        )

    effective_confidence = 0
    if confidence:
        if intraday_signals.option_signal:
            effective_confidence = min(
                confidence,
                round(intraday_signals.option_confidence),
                90,
            )
        else:
            sample_size_cap = min(90, 40 + len(differences) * 5)
            effective_confidence = min(
                confidence,
                sample_size_cap,
                average_ocr_confidence,
            )
        lines.append(
            f"Confidence: {confidence_label(effective_confidence)} "
            f"({effective_confidence}%)"
        )
    else:
        lines.append("Confidence: Not available")

    if explanations:
        lines.extend(["", "Signal checks:"])
        lines.extend(f"• {item}" for item in explanations[:5])

    previous_support = max_oi_strike(previous_rows.values(), "put_oi")
    previous_resistance = max_oi_strike(previous_rows.values(), "call_oi")

    bullish_rows = sum(
        1 for item in differences if item.get("row_score", 0) > 0
    )
    bearish_rows = sum(
        1 for item in differences if item.get("row_score", 0) < 0
    )
    mixed_rows = sum(
        1 for item in differences if item.get("row_score", 0) == 0
    )

    lines.extend([
        "",
        "What changed:",
        f"• Bullish strikes: {bullish_rows}",
        f"• Bearish strikes: {bearish_rows}",
        f"• Mixed / small changes: {mixed_rows}",
    ])

    support_sentence = movement_sentence(
        "OI-based support",
        previous_support,
        current_support,
    )
    resistance_sentence = movement_sentence(
        "OI-based resistance",
        previous_resistance,
        current_resistance,
    )
    if support_sentence:
        lines.append(f"• {support_sentence}")
    if resistance_sentence:
        lines.append(f"• {resistance_sentence}")

    lines.extend(["", "Simple meaning:"])
    lines.extend(
        f"• {item}"
        for item in simple_meaning(
            direction,
            current_support,
            current_resistance,
        )
    )

    strongest = strongest_change_lines(
        differences,
        int(config["easy_summary_rows"]),
    )
    if strongest:
        lines.extend(["", "Main strike changes:"])
        lines.extend(f"• {item}" for item in strongest)

    lines.extend([
        "",
        f"Strikes read: {len(current_rows)}",
        f"Data quality: {quality} (OCR {average_ocr_confidence}%)",
    ])

    if quality == "Low":
        lines.append(
            "⚠️ Image reading quality is low. Check debug_ocr before trusting this update."
        )

    lines.extend([
        "",
        "⚠️ This is an OCR and OI-change summary, not an automatic buy/sell instruction.",
    ])
    return "\n".join(lines)


def build_timeframe_report(
    snapshot_id: int,
    captured_at: str,
    instrument: str | None,
    current_rows: dict[float, StrikeRow],
    differences: list[dict[str, Any]],
    analysis: MarketAnalysis,
    intraday_trend_rows: list[IntradayTrendRow],
    comparison_status: str,
    previous_snapshot: sqlite3.Row | None,
    comparison_seconds: int | None,
    config: dict[str, Any],
    strike_comparison: TimeframeStrikeComparison | None = None,
    option_probabilities: list[OptionProbabilityResult] | None = None,
) -> str:
    """Render a complete standalone report for one analysis timeframe."""

    horizon_minutes = analysis.horizon_minutes
    expected_interval = int(
        config.get("intraday_expected_interval_minutes", 15)
    )
    expected_rows = horizon_minutes // expected_interval + 1
    minimum_confidence = float(
        config.get("prediction_min_intraday_confidence", 60)
    )

    if analysis.state != "UNCONFIRMED":
        row_by_time = {
            row.time_text: row for row in intraday_trend_rows
        }
        window_rows = [
            row_by_time[time_text]
            for time_text in analysis.window_times
            if time_text in row_by_time
        ]
    else:
        reliable_rows = [
            row
            for row in intraday_trend_rows
            if row.math_valid and row.confidence >= minimum_confidence
        ]
        reliable_rows.sort(key=trend_row_seconds, reverse=True)
        window_rows = sorted(
            reliable_rows[:expected_rows],
            key=trend_row_seconds,
        )

    quality, average_ocr_confidence = data_quality(
        current_rows,
        float(config["minimum_summary_row_confidence"]),
    )
    strike_mover_limit = int(config.get("strike_movers_rows", 15))
    levels = estimate_support_resistance(
        current_rows.values(),
        analysis.current_price,
    )
    current_support = max_oi_strike(current_rows.values(), "put_oi")
    current_resistance = max_oi_strike(current_rows.values(), "call_oi")
    title, _ = direction_title(analysis.state)
    latest_row = window_rows[-1] if window_rows else (
        intraday_trend_rows[0] if intraday_trend_rows else None
    )

    lines = [
        f"{instrument or 'MARKET'} {horizon_minutes}-MINUTE DETAILED ANALYSIS",
        f"Snapshot ID: {snapshot_id}",
        f"Latest table time: {format_snapshot_clock(captured_at, config)}",
        (
            f"Validated rows: {len(window_rows)}/{expected_rows} "
            f"at {expected_interval}-minute intervals"
        ),
        "Diff definition: Put - Call",
        "",
        "Intraday rows used:",
    ]
    if window_rows:
        for row in window_rows:
            lines.append(
                f"- {row.time_text}: Call={format_number(row.call_value)}, "
                f"Put={format_number(row.put_value)}, "
                f"Diff={format_number(row.diff_value)}, "
                f"PCR={row.pcr:.3f}, "
                f"Price={format_exact_number(row.price)}, "
                f"VWAP={format_exact_number(row.vwap)}, "
                f"Option={row.option_signal or 'n/a'}, "
                f"VWAP signal={row.vwap_signal or 'n/a'}, "
                f"OCR={row.confidence:.0f}%"
            )
    else:
        lines.append("- No validated Intraday Trend rows were available.")

    forecast = (
        analysis.predicted_label
        if analysis.state != "UNCONFIRMED"
        else "NOT ISSUED"
    )
    lines.extend([
        "",
        "Timeframe result:",
        f"- State: {title} ({analysis.state})",
        f"- Average condition: {analysis.current_condition}",
        f"- Latest row condition: {analysis.latest_condition}",
        f"- Flow momentum: {analysis.momentum}",
        f"- Next {horizon_minutes}-minute forecast: {forecast}",
        f"- Composite score: {analysis.score:+.3f} (-1 down, +1 up)",
        f"- Confidence: {confidence_label(analysis.confidence)} "
        f"({analysis.confidence}%)",
        "",
        f"{horizon_minutes}-minute averages:",
        f"- Average Call: {format_number(analysis.average_call)}",
        f"- Average Put: {format_number(analysis.average_put)}",
        f"- Average Diff: {format_number(analysis.average_diff)}",
        f"- Average PCR: {format_number(analysis.average_pcr)}",
        f"- Average Price: {format_exact_number(analysis.average_price)}",
        f"- Average VWAP: {format_exact_number(analysis.average_vwap)}",
        (
            f"- Average normalized imbalance: {analysis.imbalance:+.3f}"
            if analysis.imbalance is not None
            else "- Average normalized imbalance: n/a"
        ),
        "",
        "Latest-row and momentum checks:",
        f"- Current PCR: {format_number(analysis.current_pcr)}",
        f"- PCR change: {format_number(analysis.pcr_change)}",
        f"- Call total change: {format_percent(analysis.call_change_pct)}",
        f"- Put total change: {format_percent(analysis.put_change_pct)}",
        f"- Diff change: {format_number(analysis.diff_change)}",
        (
            f"- Latest normalized imbalance: "
            f"{analysis.latest_imbalance:+.3f}"
            if analysis.latest_imbalance is not None
            else "- Latest normalized imbalance: n/a"
        ),
        (
            f"- Latest Price / VWAP: {format_exact_number(latest_row.price)} / "
            f"{format_exact_number(latest_row.vwap)}"
            if latest_row is not None
            else "- Latest Price / VWAP: n/a / n/a"
        ),
        (
            f"- Latest signals: Option={latest_row.option_signal or 'n/a'}, "
            f"VWAP={latest_row.vwap_signal or 'n/a'}"
            if latest_row is not None
            else "- Latest signals: Option=n/a, VWAP=n/a"
        ),
    ])

    component_labels = {
        "flow_momentum": f"{horizon_minutes}-minute Call/Put flow",
        "average_balance": f"{expected_rows}-row average Call/Put balance",
        "price_vwap": "average price and VWAP",
        "option_signal": "Option Signal",
        "vwap_signal": "VWAP Signal",
        "strike_confirmation": "strike-table confirmation",
    }
    lines.extend(["", "Composite components:"])
    if analysis.component_scores:
        for name, value in analysis.component_scores.items():
            lines.append(
                f"- {component_labels.get(name, name)}: {value:+.3f}"
            )
    else:
        lines.append(
            "- Not calculated because the complete window was unavailable."
        )

    lines.extend(["", "Signal checks:"])
    if analysis.reasons:
        lines.extend(f"- {reason}" for reason in analysis.reasons)
    else:
        lines.append("- No reliable timeframe signal checks were available.")
    if comparison_status == "STALE":
        lines.append(
            "- This is a repeated market time; no new prediction was stored."
        )

    if strike_comparison is None:
        strike_comparison = TimeframeStrikeComparison(
            horizon_minutes=horizon_minutes,
            current_time=format_snapshot_clock(captured_at, config),
            previous_time=None,
            movers=[],
            current_strike_count=len(current_rows),
            common_strike_count=0,
            status="NO COMPARABLE SNAPSHOT",
        )
    lines.extend(timeframe_strike_comparison_lines(
        strike_comparison,
        strike_mover_limit,
    ))
    lines.extend(option_probability_lines(
        option_probabilities or [],
        horizon_minutes,
        config,
    ))

    bullish_rows = sum(
        1 for item in differences if item.get("row_score", 0) > 0
    )
    bearish_rows = sum(
        1 for item in differences if item.get("row_score", 0) < 0
    )
    mixed_rows = sum(
        1 for item in differences if item.get("row_score", 0) == 0
    )
    lines.extend([
        "",
        "Estimated OI support and resistance:",
        f"- Primary support: {format_exact_number(levels.primary_support)}",
        f"- Developing support: {format_exact_number(levels.developing_support)}",
        f"- Weakening support: {format_exact_number(levels.weakening_support)}",
        f"- Primary resistance: {format_exact_number(levels.primary_resistance)}",
        f"- Developing resistance: {format_exact_number(levels.developing_resistance)}",
        f"- Weakening resistance: {format_exact_number(levels.weakening_resistance)}",
        f"- Level confidence: {confidence_label(levels.confidence)} "
        f"({levels.confidence}%)",
        "- These are OI-derived estimates, not guaranteed turning points.",
        "",
        "Strike-table confirmation:",
        f"- Bullish / bearish / mixed rows: "
        f"{bullish_rows} / {bearish_rows} / {mixed_rows}",
        f"- OI support / resistance: "
        f"{format_exact_number(current_support)} / "
        f"{format_exact_number(current_resistance)}",
        f"- Strikes read: {len(current_rows)}",
        f"- Data quality: {quality} (OCR {average_ocr_confidence}%)",
    ])
    if previous_snapshot is None:
        lines.append("- External snapshot comparison: baseline saved.")
    else:
        lines.append(
            "- Compared with prior snapshot: "
            f"{format_snapshot_clock(str(previous_snapshot['captured_at']), config)}"
        )
        if comparison_seconds is not None:
            lines.append(
                f"- External snapshot gap: {elapsed_text(comparison_seconds)}"
            )

    lines.extend(["", "Plain-language conclusion:"])
    lines.extend(
        f"- {item}"
        for item in simple_meaning(
            analysis.state,
            levels.primary_support,
            levels.primary_resistance,
            horizon_minutes=horizon_minutes,
        )
    )
    lines.extend([
        "",
        "This is an OCR and statistical analysis, not an automatic trade order.",
    ])
    return "\n".join(lines)


def build_market_report(
    snapshot_id: int,
    captured_at: str,
    instrument: str | None,
    current_rows: dict[float, StrikeRow],
    previous_snapshot: sqlite3.Row | None,
    previous_rows: dict[float, StrikeRow],
    differences: list[dict[str, Any]],
    direction: str,
    confidence: int,
    explanations: list[str],
    comparison_seconds: int | None,
    comparison_status: str,
    intraday_times: list[str],
    intraday_signals: IntradaySignalResult,
    analysis: MarketAnalysis,
    timeframe_analyses: dict[int, MarketAnalysis],
    intraday_trend_rows: list[IntradayTrendRow],
    config: dict[str, Any],
    strike_comparisons: dict[int, TimeframeStrikeComparison] | None = None,
    option_probabilities: dict[int, list[OptionProbabilityResult]] | None = None,
) -> str:
    """Render a short easy-reading report for the dashboard and Telegram."""

    del (
        snapshot_id,
        previous_snapshot,
        previous_rows,
        differences,
        explanations,
        comparison_seconds,
        intraday_times,
        intraday_signals,
        strike_comparisons,
    )
    quality, average_ocr_confidence = data_quality(
        current_rows,
        float(config["minimum_summary_row_confidence"]),
    )
    levels = estimate_support_resistance(
        current_rows.values(),
        analysis.current_price,
    )
    latest_row = intraday_trend_rows[0] if intraday_trend_rows else None

    def easy_direction(value: str) -> str:
        labels = {
            "BULLISH": "UP",
            "BEARISH": "DOWN",
            "BALANCED": "SIDEWAYS",
            "UP": "UP",
            "DOWN": "DOWN",
            "FLAT": "SIDEWAYS",
        }
        return labels.get(value.upper(), "WAITING FOR DATA")

    lines = [
        f"{instrument or 'MARKET'} EASY REPORT — "
        f"{format_snapshot_clock(captured_at, config)}",
        "",
        f"Current price: {format_exact_number(analysis.current_price)}",
        (
            f"VWAP: {format_exact_number(latest_row.vwap)}"
            if latest_row is not None
            else "VWAP: n/a"
        ),
        f"Market direction: {easy_direction(analysis.current_condition)}",
        "",
    ]

    for minutes in (15, 30, 45):
        timeframe = timeframe_analyses.get(minutes)
        if timeframe is None or timeframe.state == "UNCONFIRMED":
            lines.append(
                f"{minutes}-minute view: WAITING FOR MORE DATA"
            )
        else:
            lines.append(
                f"{minutes}-minute view: "
                f"{easy_direction(timeframe.predicted_label)} "
                f"(confidence {timeframe.confidence}%)"
            )

    lines.extend([
        "",
        f"Support strike: {format_exact_number(levels.primary_support)}",
        f"Resistance strike: {format_exact_number(levels.primary_resistance)}",
        "",
    ])

    probability_results = option_probabilities or {}
    ready_setups = sorted(
        (
            (minutes, result)
            for minutes, results in probability_results.items()
            for result in results
            if result.model_signal != "NO TRADE"
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    if ready_setups:
        minutes, setup = ready_setups[0]
        lines.extend([
            f"Option selling decision: YES — {minutes}-MINUTE SETUP",
            f"Sell strike: {format_exact_number(setup.strike)} "
            f"{setup.option_type}",
            f"Hedge strike: {format_exact_number(setup.hedge_strike)} "
            f"{setup.option_type}",
            "Use the defined-risk hedge; do not sell the option naked.",
        ])
    else:
        lines.extend([
            "Option selling decision: WAIT",
            "No option-selling setup passed every safety check.",
        ])

    reason_lines: list[str] = []
    if latest_row is not None and latest_row.price is not None:
        if latest_row.vwap is None:
            reason_lines.append("VWAP could not be read reliably.")
        elif latest_row.price < latest_row.vwap:
            reason_lines.append("Price is below VWAP.")
        elif latest_row.price > latest_row.vwap:
            reason_lines.append("Price is above VWAP.")
        else:
            reason_lines.append("Price is at VWAP.")

    state_reasons = {
        "BEARISH_STRENGTHENING": "Downward pressure is strengthening.",
        "BEARISH_WEAKENING": "Downward pressure is weakening.",
        "BULLISH_STRENGTHENING": "Upward pressure is strengthening.",
        "BULLISH_WEAKENING": "Upward pressure is weakening.",
        "REVERSAL_WATCH": "A possible reversal is developing but is not confirmed.",
        "NEUTRAL_TRANSITION": "The market does not have a stable direction yet.",
        "UNCONFIRMED": "More clean 15-minute rows are needed.",
    }
    reason_lines.append(
        state_reasons.get(
            direction,
            "Wait for the next confirmed update before making a decision.",
        )
    )
    if not ready_setups:
        reason_lines.append(
            "Do not sell options until every safety check passes."
        )

    lines.extend(["", "Reason:", *reason_lines])
    if comparison_status == "STALE":
        lines.extend([
            "",
            "Data warning: No fresh market time was detected.",
        ])
    if instrument is None:
        lines.extend([
            "",
            "Data warning: NIFTY/BANKNIFTY title was not identified.",
        ])
    lines.extend([
        "",
        f"Data quality: {quality.upper()} (OCR {average_ocr_confidence}%)",
    ])
    if quality == "Low":
        lines.append(
            "Data warning: Image reading quality is low. Wait for a clean update."
        )
    lines.extend([
        "",
        "This is a market-data summary, not an automatic trade order.",
    ])
    return "\n".join(lines)


def build_recent_instrument_report(
    instrument: str,
    captured_at: str,
    minutes: int,
    analysis: MarketAnalysis,
    current_rows: dict[float, StrikeRow],
    intraday_trend_rows: list[IntradayTrendRow],
    image_count: int,
    option_probabilities: list[OptionProbabilityResult],
    config: dict[str, Any],
) -> str:
    """Build one simple on-demand report for a selected recent window."""

    quality, average_ocr_confidence = data_quality(
        current_rows,
        float(config["minimum_summary_row_confidence"]),
    )
    levels = estimate_support_resistance(
        current_rows.values(),
        analysis.current_price,
    )
    row_by_time = {row.time_text: row for row in intraday_trend_rows}
    ordered_window_times = sorted(
        set(analysis.window_times),
        key=lambda value: (
            parse_clock_word(value) or (99, 99, 99)
        ),
    )
    latest_row = (
        row_by_time.get(ordered_window_times[-1])
        if ordered_window_times
        else intraday_trend_rows[0] if intraday_trend_rows else None
    )

    def easy_direction(value: str) -> str:
        return {
            "BULLISH": "UP",
            "BEARISH": "DOWN",
            "BALANCED": "SIDEWAYS",
            "UP": "UP",
            "DOWN": "DOWN",
            "FLAT": "SIDEWAYS",
        }.get(value.upper(), "WAITING FOR DATA")

    view = (
        "WAITING FOR MORE DATA"
        if analysis.state == "UNCONFIRMED"
        else f"{easy_direction(analysis.predicted_label)} "
        f"(confidence {analysis.confidence}%)"
    )
    window_text = (
        f"{ordered_window_times[0]} to {ordered_window_times[-1]} IST"
        if ordered_window_times
        else "complete image window unavailable"
    )
    captured_clock = format_snapshot_clock(captured_at, config)
    latest_valid_clock = (
        f"{ordered_window_times[-1]} IST"
        if ordered_window_times
        else None
    )
    lines = [
        f"{instrument} — LAST {minutes}-MINUTE EASY REPORT",
        f"Latest table time: {captured_clock}",
        f"Stored images used: {image_count}",
        f"Data window used: {window_text}",
        "",
        f"Current price: {format_exact_number(analysis.current_price)}",
        (
            f"VWAP: {format_exact_number(latest_row.vwap)}"
            if latest_row is not None
            else "VWAP: n/a"
        ),
        f"Current direction: {easy_direction(analysis.current_condition)}",
        f"{minutes}-minute view: {view}",
        "",
        f"Support strike: {format_exact_number(levels.primary_support)}",
        f"Resistance strike: {format_exact_number(levels.primary_resistance)}",
        "",
    ]

    ready_setup = next(
        (
            result
            for result in option_probabilities
            if result.model_signal != "NO TRADE"
        ),
        None,
    )
    if ready_setup is None:
        lines.extend([
            "Option selling decision: WAIT",
            "No option-selling setup passed every safety check.",
        ])
    else:
        lines.extend([
            "Option selling decision: YES — DEFINED-RISK SPREAD",
            f"Sell strike: {format_exact_number(ready_setup.strike)} "
            f"{ready_setup.option_type}",
            f"Hedge strike: {format_exact_number(ready_setup.hedge_strike)} "
            f"{ready_setup.option_type}",
            "Do not sell the option without the displayed hedge.",
        ])

    reasons: list[str] = []
    if latest_row is not None and latest_row.price is not None:
        if latest_row.vwap is None:
            reasons.append("VWAP could not be read reliably.")
        elif latest_row.price < latest_row.vwap:
            reasons.append("Price is below VWAP.")
        elif latest_row.price > latest_row.vwap:
            reasons.append("Price is above VWAP.")
        else:
            reasons.append("Price is at VWAP.")
    reasons.append({
        "BEARISH_STRENGTHENING": "Downward pressure is strengthening.",
        "BEARISH_WEAKENING": "Downward pressure is weakening.",
        "BULLISH_STRENGTHENING": "Upward pressure is strengthening.",
        "BULLISH_WEAKENING": "Upward pressure is weakening.",
        "REVERSAL_WATCH": "A possible reversal is not confirmed yet.",
        "UNCONFIRMED": f"A complete {minutes}-minute window was not available.",
    }.get(
        analysis.state,
        "Wait for another confirmed update if the direction is unclear.",
    ))
    if ready_setup is None:
        reasons.append("Do not sell options until every safety check passes.")

    lines.extend([
        "",
        "Reason:",
        *reasons,
    ])
    if latest_valid_clock is not None and latest_valid_clock != captured_clock:
        lines.extend([
            "",
            "Data warning: The latest valid Intraday Trend row used was "
            f"{latest_valid_clock}, but the image time was {captured_clock}.",
            "The newest row may not have been read correctly; check OCR debug.",
        ])
    lines.extend([
        "",
        f"Data quality: {quality.upper()} (OCR {average_ocr_confidence}%)",
        "",
        "This is a market-data summary, not an automatic trade order.",
    ])
    return "\n".join(lines)


def regenerate_recent_reports(
    minutes: int,
    config: dict[str, Any],
) -> Path:
    """Rebuild a selected recent-window report for NIFTY and BANKNIFTY."""

    if minutes not in {15, 30, 45}:
        raise ValueError("Report window must be 15, 30, or 45 minutes.")

    market_timezone = configured_market_timezone(config)
    market_day = datetime.now(market_timezone).date().isoformat()
    connection = connect_database()
    sections: list[str] = []
    try:
        for instrument in ("NIFTY", "BANKNIFTY"):
            snapshot = connection.execute(
                """
                SELECT *
                FROM snapshots
                WHERE instrument = ?
                  AND substr(captured_at, 1, 10) = ?
                  AND row_count > 0
                ORDER BY captured_at DESC, id DESC
                LIMIT 1
                """,
                (instrument, market_day),
            ).fetchone()
            if snapshot is None:
                sections.append("\n".join([
                    f"{instrument} — LAST {minutes}-MINUTE EASY REPORT",
                    "",
                    f"ERROR: {instrument} data is not available for {market_day}.",
                    "Check the Telegram source images and OCR debug files.",
                ]))
                continue

            snapshot_id = int(snapshot["id"])
            captured_at = str(snapshot["captured_at"])
            current_rows = load_rows(connection, snapshot_id)
            trend_rows, image_count = load_recent_intraday_rows_from_images(
                connection=connection,
                instrument=instrument,
                market_day=market_day,
                latest_captured_at=captured_at,
                minutes=minutes,
            )
            target_time = (
                parse_iso_datetime(captured_at) - timedelta(minutes=minutes)
            ).isoformat()
            previous = connection.execute(
                """
                SELECT *
                FROM snapshots
                WHERE instrument = ?
                  AND substr(captured_at, 1, 10) = ?
                  AND captured_at <= ?
                  AND row_count > 0
                ORDER BY captured_at DESC, id DESC
                LIMIT 1
                """,
                (instrument, market_day, target_time),
            ).fetchone()
            strike_direction = "BASELINE"
            if previous is not None:
                _, strike_direction, _, _ = compare_snapshots(
                    load_rows(connection, int(previous["id"])),
                    current_rows,
                    config,
                )

            analysis = analyze_market_window(
                trend_rows,
                strike_direction,
                config,
                horizon_minutes=minutes,
            )
            probabilities = estimate_option_probabilities(
                connection=connection,
                instrument=instrument,
                current_rows=current_rows,
                analysis=analysis,
                config=config,
                intraday_trend_rows=trend_rows,
                volume_confirmation=None,
            )
            sections.append(build_recent_instrument_report(
                instrument=instrument,
                captured_at=captured_at,
                minutes=minutes,
                analysis=analysis,
                current_rows=current_rows,
                intraday_trend_rows=trend_rows,
                image_count=image_count,
                option_probabilities=probabilities,
                config=config,
            ))
    finally:
        connection.close()

    output_directory = REPORT_DIR / market_day
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = (
        output_directory
        / f"regenerated_{minutes}min_combined_report.txt"
    )
    output_path.write_text(
        ("\n\n" + "=" * 64 + "\n\n").join(sections),
        encoding="utf-8",
    )
    LOGGER.info(
        "Regenerated last-%d-minute NIFTY and BANKNIFTY report: %s",
        minutes,
        output_path,
    )
    print(f"REPORT_PATH={output_path}", flush=True)
    return output_path


# -----------------------------------------------------------------------------
# Image processing pipeline
# -----------------------------------------------------------------------------


def make_file_source_key(image_path: Path) -> str:
    stat = image_path.stat()
    return f"file:{image_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"


def process_image(
    image_path: Path,
    source_key: str,
    captured_at: str,
    message_id: int | None,
    channel: str,
    config: dict[str, Any],
) -> SnapshotResult | None:
    connection = connect_database()
    try:
        if source_already_processed(
            connection,
            source_key,
            image_path,
        ):
            LOGGER.info("Already processed: %s", image_path.name)
            return None

        image = cv2.imread(str(image_path))
        if image is None:
            raise RuntimeError(f"Unable to read image: {image_path}")

        scale = float(config["ocr_scale"])
        processed = preprocess_image(image, scale)
        words = extract_words(processed, scale, float(config["minimum_ocr_confidence"]))
        grouped_rows = group_words_into_rows(words, float(config["row_vertical_tolerance_ratio"]))
        raw_ocr_text = "\n".join(" ".join(word.text for word in row) for row in grouped_rows)

        image_height, image_width = image.shape[:2]
        strike_x, header_y = locate_strike_column(
            words,
            image_width,
            float(config["strike_x_ratio_fallback"]),
        )
        option_columns = locate_option_chain_columns(
            words,
            strike_x,
            header_y,
            image_width,
        )
        parsed_rows = parse_strike_rows(
            grouped_rows,
            strike_x,
            header_y,
            image_width,
            config,
            option_columns,
        )
        instrument = infer_instrument(raw_ocr_text)
        expiry = infer_expiry(raw_ocr_text)
        snapshot_time_result = extract_latest_snapshot_datetime(
            words=words,
            raw_ocr_text=raw_ocr_text,
            reference_at=captured_at,
            image_width=image_width,
            config=config,
        )
        corrected_latest_time = (
            parse_iso_datetime(snapshot_time_result[0]).strftime("%H:%M:%S")
            if snapshot_time_result is not None
            else None
        )
        intraday_trend_rows = extract_intraday_trend_rows(
            words,
            image_width,
            config,
            corrected_latest_time,
        )
        if intraday_trend_rows:
            latest_trend_row = intraday_trend_rows[0]
            intraday_signals = IntradaySignalResult(
                option_signal=latest_trend_row.option_signal,
                option_confidence=latest_trend_row.confidence,
                vwap_signal=latest_trend_row.vwap_signal,
                vwap_confidence=latest_trend_row.confidence,
            )
        else:
            intraday_signals = extract_latest_intraday_signals(
                words,
                image_width,
            )

        save_debug_files(image, processed, words, parsed_rows, strike_x, source_key)

        if snapshot_time_result is None:
            report_text = "\n".join([
                f"📊 {instrument or 'MARKET'} UPDATE",
                "Snapshot time: NOT READ",
                "",
                "The image was downloaded, but the Intraday Trend Time column could not be read reliably.",
                "This image was not added to the internal-time comparison.",
                "",
                "Check the matching files in debug_ocr and make sure the time is visible in the image.",
            ])
            error_name = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:12]
            report_path = (
                report_day_directory(captured_at, config)
                / f"time_unreadable_{error_name}.txt"
            )
            report_path.write_text(report_text, encoding="utf-8")
            LOGGER.warning("Snapshot time not detected in %s", image_path.name)
            return SnapshotResult(
                snapshot_id=0,
                source_key=source_key,
                image_path=str(image_path),
                captured_at="",
                instrument=instrument,
                expiry=expiry,
                strike_x=strike_x,
                rows=parsed_rows,
                report_text=report_text,
                diff_csv_path=None,
            )

        captured_at, snapshot_time_raw, intraday_times = snapshot_time_result
        if intraday_trend_rows:
            intraday_times = [
                row.time_text for row in intraday_trend_rows
            ]
        LOGGER.info(
            "Snapshot time read from image: %s (%s)",
            captured_at,
            snapshot_time_raw,
        )

        snapshot_id = insert_snapshot(
            connection=connection,
            source_key=source_key,
            message_id=message_id,
            channel=channel,
            captured_at=captured_at,
            image_path=image_path,
            instrument=instrument,
            expiry=expiry,
            strike_x=strike_x,
            raw_ocr_text=raw_ocr_text,
            rows=parsed_rows,
            intraday_rows=intraday_trend_rows,
        )

        previous_snapshot, comparison_seconds, comparison_status = (
            find_previous_internal_snapshot(
                connection=connection,
                current_snapshot_id=snapshot_id,
                instrument=instrument,
                expiry=expiry,
                current_captured_at=captured_at,
            )
        )
        current_map = {row.strike: row for row in parsed_rows}

        if comparison_status == "STALE":
            previous_map = {}
            differences = []
            direction = "STALE"
            confidence = 0
            explanations = []
        elif previous_snapshot is None:
            previous_map = {}
            differences = []
            direction = "BASELINE"
            confidence = 0
            explanations = []
        else:
            previous_map = load_rows(connection, int(previous_snapshot["id"]))
            differences, direction, confidence, explanations = compare_snapshots(
                previous_map,
                current_map,
                config,
            )

        strike_direction = direction
        timeframe_minutes = sorted({
            *(
                int(value)
                for value in config.get(
                    "analysis_timeframes_minutes",
                    [15, 30, 45],
                )
            ),
            int(config.get("prediction_horizon_minutes", 45)),
        })
        timeframe_analyses = {
            minutes: analyze_market_window(
                intraday_trend_rows,
                strike_direction,
                config,
                horizon_minutes=minutes,
            )
            for minutes in timeframe_minutes
        }
        analysis = combine_timeframe_analyses(
            timeframe_analyses,
            config,
        )
        timeframe_strike_comparisons = build_timeframe_strike_comparisons(
            connection=connection,
            current_snapshot_id=snapshot_id,
            instrument=instrument,
            expiry=expiry,
            captured_at=captured_at,
            timeframe_minutes=timeframe_minutes,
            current_rows=current_map,
            config=config,
        )
        evaluated_option_rows = evaluate_pending_option_outcomes(
            connection=connection,
            instrument=instrument,
            expiry=expiry,
            captured_at=captured_at,
            current_rows=current_map,
            config=config,
        )
        if evaluated_option_rows:
            LOGGER.info(
                "Evaluated %d prior option premium outcome(s).",
                evaluated_option_rows,
            )
        option_probabilities = {
            minutes: estimate_option_probabilities(
                connection=connection,
                instrument=instrument,
                current_rows=current_map,
                analysis=timeframe_analysis,
                config=config,
                intraday_trend_rows=intraday_trend_rows,
                volume_confirmation=None,
            )
            for minutes, timeframe_analysis in timeframe_analyses.items()
        }
        if comparison_status != "STALE":
            direction = analysis.state
            confidence = analysis.confidence
            explanations = [
                *analysis.reasons,
                *explanations,
            ]

            evaluated = evaluate_pending_predictions(
                connection=connection,
                instrument=instrument,
                captured_at=captured_at,
                actual_price=analysis.current_price,
                config=config,
            )
            if evaluated:
                LOGGER.info(
                    "Evaluated %d prior 45-minute prediction(s).",
                    evaluated,
                )
            insert_prediction(
                connection=connection,
                snapshot_id=snapshot_id,
                instrument=instrument,
                captured_at=captured_at,
                analysis=analysis,
            )
            insert_option_outcomes(
                connection=connection,
                snapshot_id=snapshot_id,
                instrument=instrument,
                expiry=expiry,
                captured_at=captured_at,
                current_rows=current_map,
                timeframe_analyses=timeframe_analyses,
                config=config,
            )
            write_backtest_reports(connection)
            write_option_backtest_report(connection)

        report_directory = report_day_directory(captured_at, config)
        diff_csv_path = write_diff_csv(
            snapshot_id,
            differences,
            output_directory=report_directory,
        )
        report_text = build_market_report(
            snapshot_id=snapshot_id,
            captured_at=captured_at,
            instrument=instrument,
            current_rows=current_map,
            previous_snapshot=previous_snapshot,
            previous_rows=previous_map,
            differences=differences,
            direction=direction,
            confidence=confidence,
            explanations=explanations,
            comparison_seconds=comparison_seconds,
            comparison_status=comparison_status,
            intraday_times=intraday_times,
            intraday_signals=intraday_signals,
            analysis=analysis,
            timeframe_analyses=timeframe_analyses,
            intraday_trend_rows=intraday_trend_rows,
            config=config,
            strike_comparisons=timeframe_strike_comparisons,
            option_probabilities=option_probabilities,
        )
        report_path = (
            report_directory
            / f"snapshot_{snapshot_id}_combined_report.txt"
        )
        report_path.write_text(report_text, encoding="utf-8")

        for minutes, timeframe_analysis in sorted(
            timeframe_analyses.items(),
            reverse=True,
        ):
            timeframe_report = build_timeframe_report(
                snapshot_id=snapshot_id,
                captured_at=captured_at,
                instrument=instrument,
                current_rows=current_map,
                differences=differences,
                analysis=timeframe_analysis,
                intraday_trend_rows=intraday_trend_rows,
                comparison_status=comparison_status,
                previous_snapshot=previous_snapshot,
                comparison_seconds=comparison_seconds,
                config=config,
                strike_comparison=timeframe_strike_comparisons.get(minutes),
                option_probabilities=option_probabilities.get(minutes, []),
            )
            timeframe_path = (
                report_directory
                / f"snapshot_{snapshot_id}_{minutes}min_report.txt"
            )
            timeframe_path.write_text(
                timeframe_report,
                encoding="utf-8",
            )

        LOGGER.info("Processed %s: %d strike rows", image_path.name, len(parsed_rows))
        LOGGER.info("Report: %s", report_path)
        return SnapshotResult(
            snapshot_id=snapshot_id,
            source_key=source_key,
            image_path=str(image_path),
            captured_at=captured_at,
            instrument=instrument,
            expiry=expiry,
            strike_x=strike_x,
            rows=parsed_rows,
            report_text=report_text,
            diff_csv_path=str(diff_csv_path) if diff_csv_path else None,
        )
    finally:
        connection.close()


def image_capture_datetime(image_path: Path) -> datetime:
    """
    Return the original Telegram post time for an already-downloaded image.

    Files downloaded by this project are named like:
        2026-07-17_07-15-30_message_12345.jpg

    Telegram dates are UTC. Using the filename is important because Windows
    may give many downloaded or extracted files the same modified timestamp.
    """
    match = re.search(
        r"(?P<date>\d{4}-\d{2}-\d{2})_"
        r"(?P<time>\d{2}-\d{2}-\d{2})_"
        r"message_(?P<message_id>\d+)",
        image_path.stem,
    )

    if match:
        timestamp_text = (
            f"{match.group('date')}_{match.group('time')}"
        )
        parsed = datetime.strptime(
            timestamp_text,
            "%Y-%m-%d_%H-%M-%S",
        )
        return parsed.replace(tzinfo=timezone.utc)

    # Fallback for manually copied images whose names do not contain
    # a Telegram timestamp.
    return datetime.fromtimestamp(
        image_path.stat().st_mtime,
        tz=timezone.utc,
    )


def configured_market_timezone(
    config: dict[str, Any],
) -> timezone:
    offset_minutes = int(
        config.get(
            "snapshot_timezone_offset_minutes",
            config.get("report_timezone_offset_minutes", 330),
        )
    )
    return timezone(timedelta(minutes=offset_minutes))


def image_day_folder_name(
    captured_at: datetime,
    config: dict[str, Any],
) -> str:
    """Return the Telegram image's trading-day folder in market time."""

    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    return captured_at.astimezone(
        configured_market_timezone(config)
    ).strftime("%Y-%m-%d")


def telegram_image_destination(
    captured_at: datetime,
    message_id: int,
    config: dict[str, Any],
) -> Path:
    """Create the daily folder and return the extension-free media path."""

    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    daily_directory = (
        DOWNLOAD_DIR / image_day_folder_name(captured_at, config)
    )
    daily_directory.mkdir(parents=True, exist_ok=True)

    # Keep the filename timestamp in UTC for backward compatibility with
    # image_capture_datetime(); the parent folder represents the market day.
    timestamp = captured_at.astimezone(timezone.utc).strftime(
        "%Y-%m-%d_%H-%M-%S"
    )
    return daily_directory / f"{timestamp}_message_{message_id}"


def organize_downloaded_images_by_day(
    config: dict[str, Any],
) -> int:
    """Move legacy flat downloads into market-day folders once."""

    supported = {
        suffix.lower()
        for suffix in config["supported_extensions"]
    }
    flat_images = sorted(
        path
        for path in DOWNLOAD_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in supported
    )
    if not flat_images:
        return 0

    moves: list[tuple[Path, Path]] = []
    for source in flat_images:
        captured_at = image_capture_datetime(source)
        destination_directory = (
            DOWNLOAD_DIR
            / image_day_folder_name(captured_at, config)
        )
        destination_directory.mkdir(parents=True, exist_ok=True)
        destination = destination_directory / source.name
        if destination.exists():
            LOGGER.warning(
                "Daily image destination already exists; leaving legacy "
                "file in place: %s",
                source,
            )
            continue
        source.replace(destination)
        moves.append((source, destination))

    if moves and DATABASE_PATH.exists():
        connection = sqlite3.connect(DATABASE_PATH)
        connection.row_factory = sqlite3.Row
        try:
            for source, destination in moves:
                database_rows = connection.execute(
                    """
                    SELECT id, source_key
                    FROM snapshots
                    WHERE image_path = ?
                    """,
                    (str(source),),
                ).fetchall()
                for database_row in database_rows:
                    source_key = str(database_row["source_key"])
                    if source_key.startswith("file:"):
                        source_key = make_file_source_key(destination)
                    connection.execute(
                        """
                        UPDATE snapshots
                        SET image_path = ?, source_key = ?
                        WHERE id = ?
                        """,
                        (
                            str(destination),
                            source_key,
                            int(database_row["id"]),
                        ),
                    )
            connection.commit()
        finally:
            connection.close()

    if moves:
        LOGGER.info(
            "Organized %d downloaded image(s) into daily folders.",
            len(moves),
        )
    return len(moves)


def process_existing_images(config: dict[str, Any]) -> None:
    supported = {
        suffix.lower()
        for suffix in config["supported_extensions"]
    }

    images = sorted(
        (
            path
            for path in DOWNLOAD_DIR.rglob("*")
            if path.is_file()
            and path.suffix.lower() in supported
        ),
        key=lambda path: (
            image_capture_datetime(path),
            path.name,
        ),
    )

    LOGGER.info("Existing images found: %d", len(images))

    for image_path in images:
        captured_at = image_capture_datetime(
            image_path
        ).isoformat()

        process_image(
            image_path=image_path,
            source_key=make_file_source_key(image_path),
            captured_at=captured_at,
            message_id=None,
            channel=str(config["channel"]),
            config=config,
        )


# -----------------------------------------------------------------------------
# Telegram live monitoring
# -----------------------------------------------------------------------------


def telegram_report_chunks(
    report_text: str,
    maximum_length: int = 3800,
) -> list[str]:
    """Split a report at line boundaries without silently truncating it."""

    if maximum_length <= 0:
        raise ValueError("maximum_length must be positive")
    if len(report_text) <= maximum_length:
        return [report_text]

    chunks: list[str] = []
    current_lines: list[str] = []
    current_length = 0
    for line in report_text.splitlines():
        required_length = len(line) + (1 if current_lines else 0)
        if current_lines and current_length + required_length > maximum_length:
            chunks.append("\n".join(current_lines))
            current_lines = []
            current_length = 0

        if len(line) > maximum_length:
            if current_lines:
                chunks.append("\n".join(current_lines))
                current_lines = []
                current_length = 0
            chunks.extend(
                line[index:index + maximum_length]
                for index in range(0, len(line), maximum_length)
            )
            continue

        current_lines.append(line)
        current_length += len(line) + (1 if len(current_lines) > 1 else 0)

    if current_lines:
        chunks.append("\n".join(current_lines))
    return chunks or [""]


def is_image_message(message: Any) -> bool:
    if message.photo is not None:
        return True
    return bool(
        message.document is not None
        and message.document.mime_type
        and message.document.mime_type.startswith("image/")
    )


def telegram_message_already_processed(
    channel: str,
    message_id: int,
) -> bool:
    """Return True when this Telegram post is already in the database."""

    source_key = f"telegram:{channel}:{message_id}"
    filename_marker = f"%message_{message_id}.%"

    connection = connect_database()
    try:
        row = connection.execute(
            """
            SELECT 1
            FROM snapshots
            WHERE source_key = ?
               OR (channel = ? AND message_id = ?)
               OR image_path LIKE ?
            LIMIT 1
            """,
            (
                source_key,
                channel,
                message_id,
                filename_marker,
            ),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def existing_telegram_image(message_id: int) -> Path | None:
    """Find a previously downloaded file for a Telegram message."""

    matches = sorted(
        path
        for path in DOWNLOAD_DIR.rglob(f"*message_{message_id}.*")
        if path.is_file()
    )
    return matches[0] if matches else None


async def run_live_monitor(
    config: dict[str, Any],
    catch_up_today: bool = False,
) -> None:
    api_id_text = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    if not api_id_text or not api_hash:
        raise RuntimeError(
            "TELEGRAM_API_ID or TELEGRAM_API_HASH is missing from .env"
        )

    channel = os.getenv(
        "TELEGRAM_CHANNEL",
        str(config["channel"]),
    )
    report_target = os.getenv(
        "TELEGRAM_REPORT_TARGET",
        str(config["report_target"]),
    )

    client = TelegramClient(
        str(PROJECT_DIR / "autotrend_session"),
        int(api_id_text),
        api_hash,
    )
    processing_lock = asyncio.Lock()
    send_lock = asyncio.Lock()

    async def send_telegram_message(text: str) -> bool:
        """Send without allowing Telegram rate limits to stop the monitor."""
        async with send_lock:
            chunks = telegram_report_chunks(text)
            total_parts = len(chunks)
            for part_number, chunk in enumerate(chunks, start=1):
                payload = (
                    f"[Combined report {part_number}/{total_parts}]\n{chunk}"
                    if total_parts > 1
                    else chunk
                )
                delivered = False
                for attempt in range(1, 4):
                    try:
                        await client.send_message(report_target, payload)
                        delivered = True
                        break
                    except errors.FloodWaitError as error:
                        wait_seconds = max(1, int(error.seconds) + 1)
                        if wait_seconds > 300:
                            LOGGER.error(
                                "Telegram requested a %d-second flood wait; "
                                "report delivery was skipped.",
                                wait_seconds,
                            )
                            return False
                        LOGGER.warning(
                            "Telegram rate limit: waiting %d seconds before "
                            "report retry %d/3.",
                            wait_seconds,
                            attempt,
                        )
                        await asyncio.sleep(wait_seconds)
                    except errors.ChatAdminRequiredError:
                        LOGGER.error(
                            "Telegram cannot post to @%s. Add the logged-in "
                            "account as a channel administrator with Post "
                            "Messages permission, then retry delivery.",
                            str(report_target).lstrip("@"),
                        )
                        return False
                    except Exception:
                        LOGGER.exception(
                            "Telegram report delivery failed; monitoring will "
                            "continue."
                        )
                        return False
                if not delivered:
                    return False
            LOGGER.info(
                "Telegram report delivered to %s (%d part(s)).",
                report_target,
                total_parts,
            )
            return True

    async def download_process_and_report(
        message: Any,
        send_report: bool = True,
    ) -> SnapshotResult | None:
        """
        Download and process one Telegram image.

        Returns the result only when a new snapshot was actually processed.
        """

        if not is_image_message(message):
            return None

        message_id = int(message.id)
        message_datetime = message.date
        if message_datetime.tzinfo is None:
            message_datetime = message_datetime.replace(
                tzinfo=timezone.utc
            )

        async with processing_lock:
            already_done = await asyncio.to_thread(
                telegram_message_already_processed,
                channel,
                message_id,
            )
            if already_done:
                LOGGER.info(
                    "Already processed Telegram message %s",
                    message_id,
                )
                return None

            image_path = existing_telegram_image(message_id)

            if image_path is None:
                destination = telegram_image_destination(
                    message_datetime,
                    message_id,
                    config,
                )
                saved_path = await message.download_media(
                    file=str(destination)
                )
                if not saved_path:
                    LOGGER.error(
                        "Telegram media download failed for message %s",
                        message_id,
                    )
                    return None
                image_path = Path(saved_path)
                LOGGER.info(
                    "Downloaded: %s",
                    image_path.relative_to(DOWNLOAD_DIR),
                )
            else:
                LOGGER.info(
                    "Using already-downloaded image: %s",
                    image_path.relative_to(DOWNLOAD_DIR),
                )

            try:
                post_timestamp = message_datetime.timestamp()
                os.utime(
                    image_path,
                    (post_timestamp, post_timestamp),
                )
            except OSError:
                LOGGER.warning(
                    "Could not preserve file timestamp for %s",
                    image_path.name,
                )

            source_key = f"telegram:{channel}:{message_id}"

            try:
                result = await asyncio.to_thread(
                    process_image,
                    image_path,
                    source_key,
                    message_datetime.isoformat(),
                    message_id,
                    channel,
                    config,
                )
            except Exception as error:
                LOGGER.exception(
                    "Failed to process Telegram image %s",
                    message_id,
                )
                await send_telegram_message(
                    (
                        "⚠️ Snapshot processing failed.\n"
                        f"Message ID: {message_id}\n"
                        f"Error: {error}"
                    ),
                )
                return None

            if (
                result
                and send_report
                and bool(config["send_report_to_telegram"])
            ):
                await send_telegram_message(result.report_text)

            return result

    async def catch_up_images_posted_today() -> tuple[int, int]:
        """
        Download/process today's missed channel images, oldest first.

        "Today" is calculated using the configured market/report timezone,
        normally IST. Telegram upload time is used only to decide which posts
        belong to today. The report time still comes from inside the image.
        """

        offset_minutes = int(
            config.get("report_timezone_offset_minutes", 330)
        )
        local_timezone = timezone(
            timedelta(minutes=offset_minutes)
        )
        now_local = datetime.now(local_timezone)
        start_local = now_local.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        start_utc = start_local.astimezone(timezone.utc)

        LOGGER.info(
            "Checking @%s for images posted today since %s...",
            channel,
            start_local.strftime("%d-%b-%Y 00:00"),
        )

        todays_images: list[Any] = []

        async for message in client.iter_messages(channel):
            message_datetime = message.date
            if message_datetime.tzinfo is None:
                message_datetime = message_datetime.replace(
                    tzinfo=timezone.utc
                )

            if message_datetime < start_utc:
                break

            if is_image_message(message):
                todays_images.append(message)

        todays_images.reverse()

        processed_count = 0
        latest_results: dict[str, SnapshotResult] = {}
        for message in todays_images:
            result = await download_process_and_report(
                message,
                send_report=False,
            )
            if result:
                processed_count += 1
                latest_results[result.instrument or "MARKET"] = result

        # A catch-up can contain dozens of screenshots. Sending every historic
        # report can trigger Telegram flood limits. Deliver only the newest
        # newly processed result for each market.
        if bool(config["send_report_to_telegram"]):
            for result in latest_results.values():
                await send_telegram_message(result.report_text)

        LOGGER.info(
            "Today's check complete: %d image post(s), %d new image(s) processed.",
            len(todays_images),
            processed_count,
        )
        return len(todays_images), processed_count

    async def on_new_message(event: Any) -> None:
        message = event.message
        if not is_image_message(message):
            return
        await download_process_and_report(message)

    await client.start()
    me = await client.get_me()
    LOGGER.info(
        "Logged in as %s",
        me.first_name or me.username or me.id,
    )

    # Register the live listener before catch-up. The lock and database checks
    # prevent duplicate processing if a post arrives during the startup scan.
    client.add_event_handler(
        on_new_message,
        events.NewMessage(chats=channel),
    )

    if catch_up_today:
        await catch_up_images_posted_today()

    LOGGER.info("Monitoring @%s for new images", channel)
    LOGGER.info("Reports will be sent to %s", report_target)
    await client.run_until_disconnected()

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor Telegram option-chain images and compare strike changes."
    )
    parser.add_argument(
        "--existing-only",
        action="store_true",
        help="Process downloaded_images and exit without connecting to Telegram.",
    )
    parser.add_argument(
        "--live-only",
        action="store_true",
        help="Skip local images and monitor only future Telegram posts.",
    )
    parser.add_argument(
        "--today-and-live",
        action="store_true",
        help=(
            "Check today's channel images, download/process any missed "
            "ones, and then keep monitoring new posts."
        ),
    )
    parser.add_argument(
        "--regenerate-minutes",
        type=int,
        choices=(15, 30, 45),
        help=(
            "Rebuild one easy report for NIFTY and BANKNIFTY using the "
            "latest stored 15, 30, or 45-minute image window, then exit."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    ensure_directories()
    configure_tesseract()
    config = load_config()
    organize_downloaded_images_by_day(config)

    if args.regenerate_minutes is not None:
        regenerate_recent_reports(args.regenerate_minutes, config)
        return

    if args.existing_only:
        if bool(config["process_existing_images_on_start"]):
            process_existing_images(config)
        return

    if args.today_and_live:
        asyncio.run(
            run_live_monitor(
                config,
                catch_up_today=True,
            )
        )
        return

    if not args.live_only and bool(config["process_existing_images_on_start"]):
        process_existing_images(config)

    asyncio.run(
        run_live_monitor(
            config,
            catch_up_today=False,
        )
    )


if __name__ == "__main__":
    main()
