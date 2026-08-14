"""API 사용량 기록기.

모든 외부 API 호출 직후에 record() 를 한 줄 남긴다.
state/usage.jsonl 에 append-only 로 쌓이고, 워크플로가 seen.json 과 함께 커밋한다.

한 줄 예시:
  {"ts":"2026-08-14T02:11:03Z","provider":"supadata","action":"transcript",
   "units":2,"unit":"credit","video":"dQw4w9WgXcQ","mode":"native"}
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

LEDGER = Path(os.getenv("USAGE_LEDGER", "state/usage.jsonl"))

# 기록만 하고 절대 예외를 던지지 않는다. 계량 실패가 봇을 죽이면 안 된다.
_ENABLED = os.getenv("USAGE_TRACKING", "true").lower() != "false"


def record(provider: str, action: str, units: float, unit: str = "call", **meta) -> None:
    """API 호출 1건을 기록한다.

    provider: supadata | youtube | gemini | anthropic
    action:   호출한 엔드포인트나 작업 이름
    units:    소모량 (크레딧 수, 쿼터 유닛, 토큰 수 등)
    unit:     units 의 단위 (credit | quota | token | call)
    meta:     video, model, status, in_tokens, out_tokens 등 자유 필드
    """
    if not _ENABLED:
        return
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "provider": provider,
            "action": action,
            "units": units,
            "unit": unit,
            "run": os.getenv("GITHUB_RUN_ID", "local"),
        }
        row.update({k: v for k, v in meta.items() if v is not None})
        with LEDGER.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"[usage] 기록 실패 (무시하고 계속): {e}")


# ── 각 provider 별 편의 함수 ────────────────────────────────────────────


def supadata(resp, video: str | None = None, mode: str | None = None) -> None:
    """Supadata 응답 헤더 x-billable-requests 에 실제 소모 크레딧이 담겨 온다."""
    billed = resp.headers.get("x-billable-requests")
    try:
        credits = float(billed) if billed is not None else 0.0
    except ValueError:
        credits = 0.0
    record(
        "supadata",
        "transcript",
        credits,
        unit="credit",
        video=video,
        mode=mode,
        status=resp.status_code,
        header_missing=billed is None or None,
    )


def youtube(action: str, units: int = 1, video: str | None = None, **meta) -> None:
    """list 계열은 호출당 1유닛이지만 search.list 는 100유닛이다.

    units 를 명시적으로 받는 이유가 이것이다. 기본값 1을 믿고
    search 를 1로 기록하면 실제 소모를 100배 과소평가하게 된다.
    """
    record("youtube", action, units, unit="quota", video=video, **meta)


def gemini(resp, model: str, video: str | None = None, action: str = "summarize") -> None:
    """google-genai SDK 응답 객체와 REST 응답 dict 를 모두 받는다.

    주의: gemini-2.5 계열은 thinking 토큰(thoughts_token_count)이
    candidates 와 별도로 잡히는데 요금은 출력 토큰으로 청구된다.
    이걸 빼면 비용이 실제보다 적게 나온다.
    """
    um = None
    if isinstance(resp, dict):
        um = resp.get("usageMetadata") or resp.get("usage_metadata")
    else:
        um = getattr(resp, "usage_metadata", None)

    def pick(*names):
        if um is None:
            return 0
        for nm in names:
            v = um.get(nm) if isinstance(um, dict) else getattr(um, nm, None)
            if v:
                return int(v)
        return 0

    in_tok = pick("prompt_token_count", "promptTokenCount")
    out_tok = pick("candidates_token_count", "candidatesTokenCount")
    think = pick("thoughts_token_count", "thoughtsTokenCount")
    cached = pick("cached_content_token_count", "cachedContentTokenCount")

    record(
        "gemini",
        action,
        in_tok + out_tok + think,
        unit="token",
        model=model,
        video=video,
        in_tokens=in_tok,
        out_tokens=out_tok + think,   # thinking 은 출력 요금
        thinking_tokens=think or None,
        cached_tokens=cached or None,
    )


def anthropic(message, model: str, video: str | None = None, action: str = "summarize") -> None:
    """SDK 의 Message 객체 또는 raw dict 둘 다 받는다."""
    usage = getattr(message, "usage", None)
    if usage is None and isinstance(message, dict):
        usage = message.get("usage", {})
    in_tok = getattr(usage, "input_tokens", None)
    out_tok = getattr(usage, "output_tokens", None)
    if in_tok is None and isinstance(usage, dict):
        in_tok, out_tok = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    in_tok, out_tok = int(in_tok or 0), int(out_tok or 0)
    record(
        "anthropic",
        action,
        in_tok + out_tok,
        unit="token",
        model=model,
        video=video,
        in_tokens=in_tok,
        out_tokens=out_tok,
    )
