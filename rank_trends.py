#!/usr/bin/env python3
"""
rank_trends.py
CSV（URL＋数値メトリクスのスナップショット時系列）から
直近2点の差分（per hour）で「急上昇（トレンド重視）」ランキングJSONを出力する。

Usage:
  python rank_trends.py --csv snapshots.csv --out ranking.json --top 20
  python rank_trends.py --make-sample

Input CSV columns (header required):
  url,captured_at,views,likes,comments,shares

Notes:
- Standard library only (no pandas).
- shares can be blank -> treated as 0.

Output JSON example (items truncated):
{
  "generated_at": "2026-01-14T21:00:00+09:00",
  "window_hours": 6,
  "stats": {...},
  "items": [...]
}
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple


WINDOW_HOURS = 6
MIN_DT_HOURS = 1.0


@dataclass
class Snapshot:
    url: str
    captured_at: datetime
    views: int
    likes: int
    comments: int
    shares: int


def parse_iso_datetime(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    if not s:
        return None
    # Allow trailing 'Z'
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    # If naive, assume local time (JST is common) but keep as local naive -> convert to aware UTC?:
    # We'll treat naive as local time and attach local tz offset (system). To avoid surprises, attach UTC.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_int(s: str, default: int = 0) -> Optional[int]:
    if s is None:
        return default
    s = s.strip()
    if s == "":
        return default
    try:
        return int(s)
    except ValueError:
        return None


def read_snapshots(csv_path: str) -> Tuple[List[Snapshot], Dict[str, int]]:
    snapshots: List[Snapshot] = []
    stats = {
        "rows_total": 0,
        "rows_parsed": 0,
        "rows_skipped_bad_datetime": 0,
        "rows_skipped_bad_int": 0,
        "rows_skipped_missing_url": 0,
    }

    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        required = {"url", "captured_at", "views", "likes", "comments", "shares"}
        if reader.fieldnames is None:
            raise ValueError("CSVにヘッダがありません。")
        missing_cols = required - set([h.strip() for h in reader.fieldnames])
        if missing_cols:
            raise ValueError(f"CSVに必要カラムが不足しています: {sorted(missing_cols)}")

        for row in reader:
            stats["rows_total"] += 1
            url = (row.get("url") or "").strip()
            if not url:
                stats["rows_skipped_missing_url"] += 1
                continue

            dt = parse_iso_datetime(row.get("captured_at", ""))
            if dt is None:
                stats["rows_skipped_bad_datetime"] += 1
                continue

            views = parse_int(row.get("views", ""))
            likes = parse_int(row.get("likes", ""))
            comments = parse_int(row.get("comments", ""))
            shares = parse_int(row.get("shares", ""), default=0)

            if None in (views, likes, comments, shares):
                stats["rows_skipped_bad_int"] += 1
                continue

            snapshots.append(
                Snapshot(
                    url=url,
                    captured_at=dt,
                    views=views,
                    likes=likes,
                    comments=comments,
                    shares=shares,
                )
            )
            stats["rows_parsed"] += 1

    return snapshots, stats


def compute_score(now: Snapshot, prev: Snapshot) -> Optional[dict]:
    dt_seconds = (now.captured_at - prev.captured_at).total_seconds()
    dt_hours = dt_seconds / 3600.0
    if dt_hours < MIN_DT_HOURS:
        return None

    dv = max(0, now.views - prev.views)
    dl = max(0, now.likes - prev.likes)
    dc = max(0, now.comments - prev.comments)
    ds = max(0, now.shares - prev.shares)

    vph = dv / dt_hours
    lph = dl / dt_hours
    cph = dc / dt_hours
    sph = ds / dt_hours

    # Trend-only score (sqrt damping)
    score = (
        1.0 * math.sqrt(vph)
        + 3.0 * math.sqrt(lph)
        + 6.0 * math.sqrt(cph)
        + 10.0 * math.sqrt(sph)
    )

    return {
        "captured_at_now": now.captured_at.isoformat(),
        "captured_at_prev": prev.captured_at.isoformat(),
        "dt_hours": round(dt_hours, 6),
        "delta": {"views": dv, "likes": dl, "comments": dc, "shares": ds},
        "per_hour": {
            "views": round(vph, 6),
            "likes": round(lph, 6),
            "comments": round(cph, 6),
            "shares": round(sph, 6),
        },
        "score": round(score, 6),
    }


def build_ranking(snapshots: List[Snapshot], top_n: int) -> Tuple[List[dict], Dict[str, int]]:
    by_url: Dict[str, List[Snapshot]] = {}
    for s in snapshots:
        by_url.setdefault(s.url, []).append(s)

    stats = {
        "urls_total": len(by_url),
        "urls_ranked": 0,
        "urls_excluded_not_enough_snapshots": 0,
        "urls_excluded_dt_too_short": 0,
    }

    items: List[dict] = []
    for url, snaps in by_url.items():
        snaps.sort(key=lambda x: x.captured_at)
        if len(snaps) < 2:
            stats["urls_excluded_not_enough_snapshots"] += 1
            continue

        now = snaps[-1]
        prev = snaps[-2]

        computed = compute_score(now, prev)
        if computed is None:
            stats["urls_excluded_dt_too_short"] += 1
            continue

        items.append({"url": url, **computed})
        stats["urls_ranked"] += 1

    items.sort(key=lambda x: x["score"], reverse=True)
    items = items[: max(0, top_n)]

    # assign ranks
    for i, it in enumerate(items, start=1):
        it["rank"] = i

    return items, stats


def make_sample_csv(path: str = "sample_snapshots.csv") -> None:
    # 5 urls x 2 snapshots, 6h gap
    base = datetime(2026, 1, 14, 0, 0, 0, tzinfo=timezone(timedelta(hours=9)))
    rows = []
    urls = [
        "https://www.tiktok.com/@a/video/111",
        "https://www.tiktok.com/@b/video/222",
        "https://www.tiktok.com/@c/video/333",
        "https://www.tiktok.com/@d/video/444",
        "https://www.tiktok.com/@e/video/555",
    ]
    # prev snapshots
    prev_metrics = [
        (1000, 50, 5, 1),
        (5000, 200, 10, 5),
        (800, 30, 3, 0),
        (12000, 400, 30, 20),
        (3000, 120, 8, 2),
    ]
    # now snapshots (simulate growth)
    now_metrics = [
        (2600, 180, 18, 6),
        (6500, 260, 12, 7),
        (3000, 220, 35, 15),
        (13500, 420, 32, 21),
        (9000, 600, 60, 40),
    ]

    for url, (v, l, c, s) in zip(urls, prev_metrics):
        rows.append(
            {
                "url": url,
                "captured_at": base.isoformat(),
                "views": v,
                "likes": l,
                "comments": c,
                "shares": s,
            }
        )

    base2 = base + timedelta(hours=6)
    for url, (v, l, c, s) in zip(urls, now_metrics):
        rows.append(
            {
                "url": url,
                "captured_at": base2.isoformat(),
                "views": v,
                "likes": l,
                "comments": c,
                "shares": s,
            }
        )

    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=["url", "captured_at", "views", "likes", "comments", "shares"]
        )
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", dest="csv_path", default="snapshots.csv")
    ap.add_argument("--out", dest="out_path", default="")
    ap.add_argument("--top", dest="top_n", type=int, default=20)
    ap.add_argument("--make-sample", action="store_true")
    args = ap.parse_args()

    if args.make_sample:
        make_sample_csv()
        print("Created sample_snapshots.csv")
        print("Try: python rank_trends.py --csv sample_snapshots.csv --top 20")
        return 0

    try:
        snapshots, row_stats = read_snapshots(args.csv_path)
    except Exception as e:
        print(f"[ERROR] CSV読み込み失敗: {e}", file=sys.stderr)
        return 1

    items, url_stats = build_ranking(snapshots, args.top_n)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_hours": WINDOW_HOURS,
        "stats": {**row_stats, **url_stats},
        "items": items,
    }

    text = json.dumps(output, ensure_ascii=False, indent=2)
    if args.out_path:
        try:
            with open(args.out_path, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"Wrote {args.out_path}")
        except Exception as e:
            print(f"[ERROR] JSON書き込み失敗: {e}", file=sys.stderr)
            return 1
    else:
        print(text)

    # Summary to stderr for quick check
    skipped = (
        row_stats["rows_skipped_bad_datetime"]
        + row_stats["rows_skipped_bad_int"]
        + row_stats["rows_skipped_missing_url"]
    )
    print(
        f"[SUMMARY] rows_total={row_stats['rows_total']} parsed={row_stats['rows_parsed']} skipped={skipped} "
        f"urls_total={url_stats['urls_total']} ranked={url_stats['urls_ranked']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Sample:
  python rank_trends.py --make-sample
  python rank_trends.py --csv sample_snapshots.csv --top 3

(Example output head)
{
  "generated_at": "...",
  "window_hours": 6,
  "stats": {...},
  "items": [
    {
      "url": "...",
      "captured_at_now": "...",
      "captured_at_prev": "...",
      "dt_hours": 6.0,
      "delta": {...},
      "per_hour": {...},
      "score": ...,
      "rank": 1
    },
    ...
  ]
}
"""
