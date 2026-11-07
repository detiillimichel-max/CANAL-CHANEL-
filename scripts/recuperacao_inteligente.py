#!/usr/bin/env python3
"""Retry only due streams and preserve a compact validation history."""
from __future__ import annotations

import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from validar_streams import check_stream

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE_DIR = ROOT / "catalogos"
HISTORY_PATH = CATALOGUE_DIR / "historico-validacao.jsonl"
COUNTRIES = ("br", "pt", "us")
WORKERS = 5

# Progressive retry intervals for temporary failures.
TRANSIENT_BACKOFF_HOURS = (1, 6, 24, 72, 168)
STABLE_HLS_DAYS = 7
NON_HLS_DAYS = 7
NOT_FOUND_DAYS = 7
FORBIDDEN_DAYS = 7
GEO_RESTRICTION_DAYS = 30
UNKNOWN_RESULT_HOURS = 24

TRANSIENT_RESULTS = {
    "timeout", "connection_error", "dns_error", "server_error",
    "rate_limited", "http_error",
}
GEO_RESULTS = {
    "geo_blocked_evidence", "forbidden_or_geo_restricted",
    "legal_or_region_restriction",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def retry_due(stream: dict, now: datetime) -> tuple[bool, str]:
    validation = stream.get("validation", {})
    recovery = stream.get("recovery", {})
    result = validation.get("result", stream.get("availability_status", "not_checked"))

    # A confirmed HLS endpoint is retained and not probed again for seven days.
    if result == "reachable_hls":
        checked = parse_time(validation.get("checked_at"))
        due_at = parse_time(recovery.get("next_retry_at"))
        if due_at is None and checked:
            due_at = checked + timedelta(days=STABLE_HLS_DAYS)
        if due_at is None or now >= due_at:
            return True, "scheduled_health_check"
        return False, "stable_hls"

    due_at = parse_time(recovery.get("next_retry_at"))
    if due_at:
        return now >= due_at, "retry_window"

    checked = parse_time(validation.get("checked_at"))
    attempts = int(recovery.get("attempt_count", 0) or 0)

    if result in TRANSIENT_RESULTS:
        delay = TRANSIENT_BACKOFF_HOURS[min(attempts, len(TRANSIENT_BACKOFF_HOURS) - 1)]
        due_at = (checked or now) + timedelta(hours=delay)
    elif result in GEO_RESULTS:
        due_at = (checked or now) + timedelta(days=GEO_RESTRICTION_DAYS)
    elif result in {"not_found", "gone", "authentication_required"}:
        due_at = (checked or now) + timedelta(days=NOT_FOUND_DAYS)
    elif result == "reachable_non_hls":
        due_at = (checked or now) + timedelta(days=NON_HLS_DAYS)
    elif result == "invalid_url":
        return False, "invalid_url_not_retried"
    else:
        due_at = (checked or now) + timedelta(hours=UNKNOWN_RESULT_HOURS)

    return now >= due_at, "retry_window"


def next_retry(result: str, attempt_count: int, checked_at: datetime) -> tuple[str, str]:
    if result == "reachable_hls":
        due = checked_at + timedelta(days=STABLE_HLS_DAYS)
        return "stable_hls", iso(due)
    if result in TRANSIENT_RESULTS:
        delay = TRANSIENT_BACKOFF_HOURS[min(max(attempt_count - 1, 0), len(TRANSIENT_BACKOFF_HOURS) - 1)]
        due = checked_at + timedelta(hours=delay)
        return "retry_scheduled", iso(due)
    if result in GEO_RESULTS:
        return "restricted_review", iso(checked_at + timedelta(days=GEO_RESTRICTION_DAYS))
    if result in {"not_found", "gone", "authentication_required"}:
        return "slow_recheck", iso(checked_at + timedelta(days=NOT_FOUND_DAYS))
    if result == "reachable_non_hls":
        return "non_hls_review", iso(checked_at + timedelta(days=NON_HLS_DAYS))
    if result == "invalid_url":
        return "invalid_url", ""
    return "slow_recheck", iso(checked_at + timedelta(hours=UNKNOWN_RESULT_HOURS))


def main() -> int:
    now = utc_now()
    total = Counter()
    history_rows = []

    for code in COUNTRIES:
        path = CATALOGUE_DIR / f"catalogo-{code}.json"
        if not path.is_file():
            raise FileNotFoundError(f"Catálogo não encontrado: {path.relative_to(ROOT)}")

        catalogue = json.loads(path.read_text(encoding="utf-8"))
        tasks = []
        counters = Counter()

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            for channel_index, channel in enumerate(catalogue.get("channels", [])):
                for stream_index, stream in enumerate(channel.get("streams", [])):
                    due, reason = retry_due(stream, now)
                    if not due:
                        counters[f"skipped_{reason}"] += 1
                        continue
                    future = executor.submit(check_stream, stream)
                    tasks.append((future, channel_index, stream_index, reason))

            for future in as_completed([item[0] for item in tasks]):
                task = next(item for item in tasks if item[0] is future)
                _, channel_index, stream_index, reason = task
                validation = future.result()
                channel = catalogue["channels"][channel_index]
                stream = channel["streams"][stream_index]
                previous_result = stream.get("validation", {}).get("result", "not_checked")
                recovery = stream.get("recovery", {})
                attempt_count = int(recovery.get("attempt_count", 0) or 0) + 1
                checked_at = parse_time(validation.get("checked_at")) or utc_now()
                state, due_at = next_retry(validation["result"], attempt_count, checked_at)

                stream["validation"] = validation
                stream["availability_status"] = validation["result"]
                stream["recovery"] = {
                    "state": state,
                    "attempt_count": attempt_count,
                    "last_attempt_at": iso(checked_at),
                    "next_retry_at": due_at or None,
                    "last_result": validation["result"],
                    "previous_result": previous_result,
                    "reason": reason,
                }

                history_rows.append({
                    "timestamp": iso(checked_at),
                    "country": code,
                    "channel_id": channel.get("id"),
                    "channel_name": channel.get("name"),
                    "url": stream.get("url"),
                    "previous_result": previous_result,
                    "result": validation["result"],
                    "attempt_count": attempt_count,
                    "next_retry_at": due_at or None,
                })
                counters["checked"] += 1
                counters[f"result_{validation['result']}"] += 1

        validation_counts = Counter(
            stream.get("validation", {}).get("result", "not_checked")
            for channel in catalogue.get("channels", [])
            for stream in channel.get("streams", [])
        )
        catalogue.setdefault("summary", {})["validation"] = dict(sorted(validation_counts.items()))
        catalogue["summary"]["recovery"] = dict(sorted(counters.items()))
        catalogue["summary"]["recovery_checked_at"] = iso(now)
        catalogue["recovery"] = {
            "checked_at": iso(now),
            "policy": "retry_due_only_with_progressive_backoff",
            "workers": WORKERS,
            "stable_hls_recheck_days": STABLE_HLS_DAYS,
            "history_file": HISTORY_PATH.relative_to(ROOT).as_posix(),
        }

        path.write_text(
            json.dumps(catalogue, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        total.update(counters)
        print(
            f"[OK] {path.relative_to(ROOT)}: "
            f"{counters.get('checked', 0)} rechecados; "
            f"{sum(value for key, value in counters.items() if key.startswith('skipped_'))} ignorados"
        )

    if history_rows:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_PATH.open("a", encoding="utf-8") as history:
            for row in history_rows:
                history.write(json.dumps(row, ensure_ascii=False) + "\n")

    print("[RESUMO] " + json.dumps(dict(sorted(total.items())), ensure_ascii=False))
    print(f"[HISTÓRICO] {len(history_rows)} novas verificações registradas")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[ERRO] {exc}", file=sys.stderr)
        raise SystemExit(1)
