"""
월별 통합 관점 생성 스크립트 (1회성).
- archive/ 폴더의 md 노트들을 업로드 월별로 묶음
- 각 달마다 그 달 영상 요약들을 종합 → 월별 섹션 생성
- 전체를 관점_종합.md로 저장하고 텔레그램으로 발송
- checker.py의 함수 재사용

아카이브 파일명 형식: YYYY-MM-DD_제목.md (백필/일상봇이 저장한 형식)
실행: GEMINI_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID 등 필요
"""
import os
import re
import glob
import time
import requests

import checker

ARCHIVE_DIR = "archive"
PERSPECTIVE_FILE = "관점_종합.md"
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]


# 원문 자막 섹션의 시작을 알리는 표식들.
# 아카이브 형식이 <details> 태그에서 마크다운 헤딩으로 바뀌어서
# 예전 <details> 하나만 보면 자막이 통째로 남는다.
TRANSCRIPT_MARKERS = [
    "<details>",
    "원문 자막",
    "📜 원문",
    "## 📜",
]


def extract_summary_from_md(path):
    """아카이브 md에서 요약 본문만 추출한다 (원문 자막 제외).

    자막을 제대로 잘라내지 못하면 프롬프트가 수만 자로 불어나
    joined[:120000] 에서 뒤쪽 영상들이 통째로 잘려나간다.
    """
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    cut = len(content)
    for marker in TRANSCRIPT_MARKERS:
        idx = content.find(marker)
        if idx != -1:
            cut = min(cut, idx)

    body = content[:cut].strip()

    # 코드블록이 열린 채로 잘렸으면 남은 백틱 제거
    if body.count("```") % 2 == 1:
        body = body.rsplit("```", 1)[0].strip()

    return body


def summarize_month(month, md_bodies):
    """한 달치 요약들을 하나의 월별 종합으로 압축."""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-3.1-flash-lite:generateContent?key={GEMINI_API_KEY}"
    )
    # 노트 하나가 지나치게 길면 뒤쪽 영상이 통째로 잘려나가므로
    # 개별 노트에도 상한을 둬서 모든 영상이 최소한 반영되게 한다.
    per_note = max(1500, 110000 // max(1, len(md_bodies)))
    trimmed = [b[:per_note] for b in md_bodies]
    joined = "\n\n---\n\n".join(trimmed)
    prompt = (
        f"아래는 {month} 한 달 동안 이선엽 대표가 출연한 여러 영상의 분석 노트입니다.\n"
        f"이 달 이선엽의 시장 관점을 아래 두 카테고리로 종합하세요.\n\n"
        "## 📊 시장 종합\n"
        "- 이 달 이선엽이 시장 전체를 본 큰 그림(강세/약세, 핵심 변수, 주요 논리).\n\n"
        "## 🎯 주목 섹터·종목\n"
        "- 이 달 반복해서 주목한 섹터·테마. 개별 종목 추천은 하지 않으니, "
        "언급된 종목이 있으면 '추천'이 아니라 '언급 맥락'으로만.\n\n"
        "간결한 마크다운. 이 달 특징이 드러나게.\n\n"
        f"[{month} 영상 노트들]\n{joined[:120000]}"
    )
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    for attempt in range(4):
        try:
            res = requests.post(url, json=body, timeout=120)
            data = res.json()
        except Exception as e:
            print(f"  [요청 오류] {type(e).__name__}, 10초 후 재시도")
            time.sleep(10)
            continue

        err = data.get("error", {})
        code = err.get("code")
        msg = err.get("message", "")

        # 일일 쿼터 소진은 기다려도 소용없다. 즉시 포기해야
        # 남은 달들이 무의미하게 시간을 끌지 않는다.
        if code == 429 and "per day" in msg.lower():
            print(f"  [일일 쿼터 소진] {month} 중단")
            return None
        if code == 429:
            wait = 30 * (attempt + 1)
            print(f"  429, {wait}초 대기 ({attempt + 1}/4)")
            time.sleep(wait)
            continue
        if err:
            print(f"  [API 오류] {msg[:80]}")
            return None

        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            # safety block 등으로 candidates가 없는 경우
            print(f"  [파싱 실패] 응답에 candidates 없음, 재시도")
            time.sleep(10)

    return None


TG_LIMIT = 3500  # 텔레그램 상한 4096보다 여유를 둔다


def _tg_post(text, markdown=True):
    """실제 발송. 성공 여부를 bool로 돌려준다."""
    payload = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text}
    if markdown:
        payload["parse_mode"] = "Markdown"
    try:
        res = requests.post(
            f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage",
            json=payload, timeout=30,
        )
        if res.status_code == 200:
            return True
        print(f"  [텔레그램 {res.status_code}] {res.text[:120]}")
        return False
    except Exception as e:
        print(f"  [텔레그램 요청 오류] {type(e).__name__}")
        return False


def _split_text(text, limit=TG_LIMIT):
    """줄 경계에서 나눈다. 문자 수로 무작정 자르면
    굵은 글씨(*) 중간이 잘려 Markdown 파싱이 깨진다."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        # 한 줄 자체가 너무 길면 강제로 쪼갠다
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur.strip():
        chunks.append(cur)
    return chunks


def send_telegram(text):
    """길면 나눠 보내고, Markdown이 거부되면 평문으로 재시도한다.
    응답을 확인하지 않으면 실패해도 '발송 완료'로 찍혀 알 수가 없다."""
    ok_all = True
    for chunk in _split_text(text):
        if _tg_post(chunk, markdown=True):
            continue
        # Markdown 파싱 실패 → 서식 없이 재시도
        print("  → 평문으로 재시도")
        if not _tg_post(chunk, markdown=False):
            ok_all = False
        time.sleep(1)
    return ok_all


def main():
    # archive/ 바로 아래의 md만 읽는다.
    # archive/rejected, archive/review 는 하위 폴더라 glob에 잡히지 않지만,
    # 의도를 분명히 하기 위해 명시적으로 걸러둔다.
    files = sorted(glob.glob(os.path.join(ARCHIVE_DIR, "*.md")))
    files = [f for f in files
             if os.path.basename(os.path.dirname(f)) == ARCHIVE_DIR]
    print(f"아카이브 파일: {len(files)}개")

    for sub in ("rejected", "review"):
        d = os.path.join(ARCHIVE_DIR, sub)
        if os.path.isdir(d):
            n = len([x for x in os.listdir(d) if x.endswith(".md")])
            print(f"  (제외) {sub}/ {n}개")

    # 월별로 그룹핑 (파일명 앞 YYYY-MM)
    by_month = {}
    for path in files:
        name = os.path.basename(path)
        m = re.match(r"(\d{4})-(\d{2})-\d{2}_", name)
        if not m:
            continue
        month = f"{m.group(1)}-{m.group(2)}"
        by_month.setdefault(month, []).append(path)

    months = sorted(by_month.keys())
    print(f"월 그룹: {months}")

    full = ["# 이선엽 관점 종합 (월별)\n"]
    failed = []

    for month in months:
        paths = by_month[month]
        bodies = [extract_summary_from_md(p) for p in paths]
        total = sum(len(b) for b in bodies)
        print(f"[{month}] {len(paths)}개 영상, 본문 {total:,}자 종합 중...")

        month_summary = summarize_month(month, bodies)
        if not month_summary:
            print(f"  [{month}] 실패")
            failed.append(month)
            # 실패한 달을 조용히 빼면 종합본에 구멍이 뚫린 줄 모른다.
            full.append(f"\n\n---\n\n# 📅 {month} ({len(paths)}개 영상)\n\n"
                        f"> ⚠️ 이 달은 생성에 실패했습니다. 재실행이 필요합니다.")
            continue

        full.append(f"\n\n---\n\n# 📅 {month} ({len(paths)}개 영상)\n\n{month_summary}")
        time.sleep(5)

    result = "\n".join(full)
    with open(PERSPECTIVE_FILE, "w", encoding="utf-8") as f:
        f.write(result)
    print(f"[저장 완료] {PERSPECTIVE_FILE}")

    # 텔레그램 발송 (월별로 나눠서 - 길이 제한 대응)
    head = (f"🧭 *이선엽 관점 종합 (월별)* 생성 완료\n"
            f"영상 {len(files)}개 / {len(months)}개월\n")
    if failed:
        head += f"⚠️ 실패: {', '.join(failed)} (재실행 필요)\n"
    head += "아래에 월별로 이어서 보냅니다."
    send_telegram(head)
    sent, failed_send = 0, []
    for month in months:
        marker = f"# 📅 {month}"
        idx = result.find(marker)
        if idx == -1:
            continue
        next_idx = result.find("# 📅", idx + 1)
        section = result[idx: next_idx if next_idx != -1 else len(result)]
        # 예전에는 section[:3800] 으로 잘라 보냈는데,
        # 서식 중간이 잘리면 Markdown 파싱이 깨져 통째로 발송 실패했다.
        if send_telegram(section):
            sent += 1
        else:
            failed_send.append(month)
        time.sleep(1)

    print(f"[텔레그램] {sent}/{len(months)}개월 발송 완료")
    if failed_send:
        print(f"[텔레그램 실패] {', '.join(failed_send)}")


if __name__ == "__main__":
    main()
