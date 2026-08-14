"""usage.jsonl 을 집계해서 대시보드가 읽을 docs/usage.json 을 만든다.

Supadata 는 /v1/me 로 실측 크레딧을 가져오고,
나머지는 봇이 남긴 원장(ledger)을 합산한다.
"""

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

LEDGER = Path("state/usage.jsonl")
OUT = Path("docs/usage.json")

# ── 요금표 ────────────────────────────────────────────────────────────
# 백만 토큰당 USD. 2026-08-14 ai.google.dev / anthropic 문서 기준.
# 무료 티어 키를 쓰면 실제 청구는 $0 이고, 이 표는 유료 전환 시 참고치다.
# 출력 단가에는 thinking 토큰이 포함된다.
PRICES = {
    "gemini-3.7-flash": {"in": 0.75, "out": 3.75},   # 2026-12-31 까지 프로모션가
    "gemini-3.6-flash": {"in": 0.75, "out": 3.75},
    "gemini-3.5-flash": {"in": 1.50, "out": 9.00},
    "gemini-3.5-flash-lite": {"in": 0.30, "out": 2.50},
    "gemini-3.1-flash-lite": {"in": 0.25, "out": 1.50},
    "claude-sonnet-4-6": {"in": 3.00, "out": 15.00},
}

# YouTube Data API 일일 쿼터. 다른 봇과 키를 공유하면 이 봇 몫만 잡힌다.
YOUTUBE_DAILY_QUOTA = 10_000


def load_rows():
    if not LEDGER.exists():
        return []
    rows = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            r["_dt"] = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
            rows.append(r)
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return rows


def fetch_supadata():
    """실측 크레딧. 이 호출 자체는 크레딧을 소모하지 않는다."""
    key = os.getenv("SUPADATA_API_KEY")
    if not key:
        return None
    try:
        r = requests.get(
            "https://api.supadata.ai/v1/me",
            headers={"x-api-key": key},
            timeout=15,
        )
        r.raise_for_status()
        d = r.json()
        return {
            "plan": d.get("plan"),
            "used": d.get("usedCredits", 0),
            "max": d.get("maxCredits", 0),
        }
    except Exception as e:  # noqa: BLE001
        print(f"[supadata] /me 조회 실패: {e}")
        return None


def token_cost(model, in_tok, out_tok):
    p = PRICES.get(model)
    if not p:
        return None
    return (in_tok / 1_000_000) * p["in"] + (out_tok / 1_000_000) * p["out"]


def main():
    now = datetime.now(timezone.utc)
    rows = load_rows()

    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_rows = [r for r in rows if r["_dt"] >= month_start]

    # YouTube 쿼터는 태평양 표준시 자정에 리셋된다 (UTC 기준 08:00 / 서머타임 07:00).
    pt_offset = timedelta(hours=-7)
    pt_now = now + pt_offset
    quota_day_start = (pt_now.replace(hour=0, minute=0, second=0, microsecond=0)) - pt_offset

    # 일별 집계 (최근 30일)
    daily = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if (now - r["_dt"]).days > 30:
            continue
        day = r["_dt"].strftime("%Y-%m-%d")
        daily[day][r["provider"]] += r.get("units", 0)

    # LLM 토큰 및 비용
    llm = defaultdict(lambda: {"in": 0, "out": 0, "calls": 0, "cost": 0.0, "priced": True})
    for r in month_rows:
        if r["provider"] not in ("gemini", "anthropic"):
            continue
        model = r.get("model", "unknown")
        k = f"{r['provider']}/{model}"
        llm[k]["in"] += r.get("in_tokens", 0)
        llm[k]["out"] += r.get("out_tokens", 0)
        llm[k]["calls"] += 1
        c = token_cost(model, r.get("in_tokens", 0), r.get("out_tokens", 0))
        if c is None:
            llm[k]["priced"] = False
        else:
            llm[k]["cost"] += c

    # 로컬 원장 기준 Supadata 크레딧 (실측값과 대조용)
    ledger_credits = sum(
        r.get("units", 0) for r in month_rows if r["provider"] == "supadata"
    )
    supa = fetch_supadata()

    # 소진 예상: 이번 달 경과일 대비 소모 속도
    days_elapsed = max((now - month_start).total_seconds() / 86400, 0.5)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    days_in_month = (next_month - month_start).days
    burn = (supa["used"] if supa else ledger_credits) / days_elapsed
    projected = burn * days_in_month
    # 이미 다 쓴 경우 음수가 나오지 않게 0에서 자른다 (0 = 지금 소진 상태)
    remaining = max(supa["max"] - supa["used"], 0) if supa else None
    days_left = (remaining / burn) if (remaining is not None and burn > 0) else None

    payload = {
        "generated_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "month": now.strftime("%Y-%m"),
        "supadata": {
            **(supa or {"plan": None, "used": ledger_credits, "max": None}),
            "ledger_used": ledger_credits,
            "live": supa is not None,
            "burn_per_day": round(burn, 2),
            "projected_month": round(projected, 1),
            "days_until_empty": round(days_left, 1) if days_left is not None else None,
        },
        "youtube": {
            "today_units": sum(
                r.get("units", 0)
                for r in rows
                if r["provider"] == "youtube" and r["_dt"] >= quota_day_start
            ),
            "daily_quota": YOUTUBE_DAILY_QUOTA,
            "month_units": sum(
                r.get("units", 0) for r in month_rows if r["provider"] == "youtube"
            ),
            "resets": "매일 태평양시 자정 (한국시간 오후 4~5시)",
        },
        "llm": [
            {
                "key": k,
                "provider": k.split("/")[0],
                "model": k.split("/", 1)[1],
                "calls": v["calls"],
                "in_tokens": v["in"],
                "out_tokens": v["out"],
                "cost_usd": round(v["cost"], 4) if v["priced"] else None,
            }
            for k, v in sorted(llm.items())
        ],
        "daily": [
            {"date": d, **{p: round(u, 1) for p, u in providers.items()}}
            for d, providers in sorted(daily.items())
        ],
        "videos_this_month": len(
            {r.get("video") for r in month_rows if r.get("video")}
        ),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[usage] {OUT} 갱신 완료")

    # Actions 실행 화면에도 요약을 남긴다
    s = payload["supadata"]
    if summary := os.getenv("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### 이번 달 사용량\n\n")
            f.write(f"- Supadata: **{s['used']}**/{s['max'] or '?'} 크레딧 "
                    f"(하루 {s['burn_per_day']}, 월말 예상 {s['projected_month']})\n")
            if s["days_until_empty"] is not None:
                f.write(f"- 이 속도면 **{s['days_until_empty']}일** 뒤 소진\n")
            f.write(f"- YouTube: 오늘 {payload['youtube']['today_units']}/"
                    f"{YOUTUBE_DAILY_QUOTA} 유닛\n")
            f.write(f"- 처리한 영상: {payload['videos_this_month']}개\n")


if __name__ == "__main__":
    main()
