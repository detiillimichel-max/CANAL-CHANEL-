#!/usr/bin/env python3
"""Conservatively check public stream endpoints and annotate country catalogues.

Checks only the supplied URL with one small HTTP GET (Range request). It does
not follow playlist entries, download media segments, bypass restrictions,
authenticate, proxy, or restream content.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE_DIR = ROOT / "catalogos"
COUNTRIES = ("br", "pt", "us")
WORKERS = 8
TIMEOUT_SECONDS = 8
READ_LIMIT_BYTES = 4096
USER_AGENT = "CANAL-CHANEL-StreamHealthCheck/1.0 (+https://github.com/detiillimichel-max/CANAL-CHANEL-)"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify_http(status: int, body: bytes) -> tuple[str, str]:
    sample = body[:READ_LIMIT_BYTES].decode("utf-8", errors="ignore").casefold()
    geo_words = ("geo-block", "geoblock", "not available in your country",
                 "outside your region", "country restriction", "region restriction")
    if status in (200, 206):
        if b"#EXTM3U" in body.upper():
            return "reachable_hls", "hls_accessible"
        return "reachable_non_hls", "http_ok_not_hls"
    if status == 401:
        return "authentication_required", "http_401"
    if status == 403:
        if any(word in sample for word in geo_words):
            return "geo_blocked_evidence", "http_403_geo_message"
        return "forbidden_or_geo_restricted", "http_403"
    if status == 404:
        return "not_found", "http_404"
    if status == 410:
        return "gone", "http_410"
    if status == 429:
        return "rate_limited", "http_429"
    if status == 451:
        return "legal_or_region_restriction", "http_451"
    if 500 <= status <= 599:
        return "server_error", f"http_{status}"
    return "http_error", f"http_{status}"


def check_stream(stream: dict) -> dict:
    url = stream.get("url", "")
    checked_at = now_utc()
    result = {
        "checked_at": checked_at,
        "http_status": None,
        "final_url": None,
        "result": "invalid_url",
        "hls_status": "not_confirmed",
        "detail": "URL ausente ou inválida",
        "check_method": "GET_range_0_4095",
    }
    if not url.startswith(("http://", "https://")):
        return result

    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, */*",
            "Range": f"bytes=0-{READ_LIMIT_BYTES - 1}",
            "Connection": "close",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(READ_LIMIT_BYTES)
            status = response.status
            result["http_status"] = status
            result["final_url"] = response.geturl()
            result["result"], result["detail"] = classify_http(status, body)
    except HTTPError as exc:
        body = b""
        try:
            body = exc.read(READ_LIMIT_BYTES)
        except Exception:
            pass
        result["http_status"] = exc.code
        result["final_url"] = exc.geturl()
        result["result"], result["detail"] = classify_http(exc.code, body)
    except URLError as exc:
        reason = str(getattr(exc, "reason", exc))
        if "timed out" in reason.casefold():
            result["result"] = "timeout"
            result["detail"] = "connection_timeout"
        elif "name or service not known" in reason.casefold() or "temporary failure in name resolution" in reason.casefold() or "getaddrinfo failed" in reason.casefold():
            result["result"] = "dns_error"
            result["detail"] = "dns_resolution_failed"
        else:
            result["result"] = "connection_error"
            result["detail"] = reason[:180]
    except TimeoutError:
        result["result"] = "timeout"
        result["detail"] = "connection_timeout"
    except Exception as exc:
        result["result"] = "connection_error"
        result["detail"] = str(exc)[:180]

    result["hls_status"] = (
        "accessible" if result["result"] == "reachable_hls"
        else "not_confirmed"
    )
    return result


def main() -> int:
    totals = Counter()
    checked_at = now_utc()

    for code in COUNTRIES:
        path = CATALOGUE_DIR / f"catalogo-{code}.json"
        if not path.is_file():
            raise FileNotFoundError(f"Catálogo não encontrado: {path.relative_to(ROOT)}")
        catalogue = json.loads(path.read_text(encoding="utf-8"))
        tasks = []
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            for channel_index, channel in enumerate(catalogue.get("channels", [])):
                for stream_index, stream in enumerate(channel.get("streams", [])):
                    future = executor.submit(check_stream, stream)
                    tasks.append((future, channel_index, stream_index))

            for future, channel_index, stream_index in tasks:
                validation = future.result()
                stream = catalogue["channels"][channel_index]["streams"][stream_index]
                stream["validation"] = validation
                stream["availability_status"] = validation["result"]
                totals[validation["result"]] += 1

        catalogue["validation"] = {
            "checked_at": checked_at,
            "method": "single_http_range_request",
            "workers": WORKERS,
            "timeout_seconds": TIMEOUT_SECONDS,
            "media_downloaded": False,
            "geo_restrictions_bypassed": False,
        }
        catalogue["summary"]["validation"] = dict(sorted(
            Counter(
                stream.get("validation", {}).get("result", "not_checked")
                for channel in catalogue.get("channels", [])
                for stream in channel.get("streams", [])
            ).items()
        ))
        catalogue["summary"]["validation_checked_at"] = checked_at
        path.write_text(
            json.dumps(catalogue, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"[OK] {path.relative_to(ROOT)}: {sum(catalogue['summary']['validation'].values())} streams verificados")

    print("[RESUMO] " + json.dumps(dict(sorted(totals.items())), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[ERRO] {exc}", file=sys.stderr)
        raise SystemExit(1)
