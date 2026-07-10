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


def extract_summary_from_md(path):
    """아카이브 md에서 제목/날짜/요약 본문만 추출 (원문 자막 제외)."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    # 원문 자막(<details> 이후) 잘라내기
    body = content.split("<details>")[0]
    return body.strip()


def summarize_month(month, md_bodies):
    """한 달치 요약들을 하나의 월별 종합으로 압축."""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-3.1-flash-lite:generateContent?key={GEMINI_API_KEY}"
    )
    joined = "\n\n---\n\n".join(md_bodies)
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
    for attempt in range(3):
        try:
            res = requests.post(url, json=body, timeout=120)
            data = res.json()
            err = data.get("error", {})
            if err.get("code") == 429:
                wait = 30 * (attempt + 1)
                print(f"  429, {wait}초 대기")
                time.sleep(wait)
                continue
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"  [월 종합 오류] {e}")
            time.sleep(10)
    return None


def send_telegram(text):
    requests.post(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage",
        json={"chat_id": os.environ["TELEGRAM_CHAT_ID"],
              "text": text, "parse_mode": "Markdown"},
    )


def main():
    files = sorted(glob.glob(os.path.join(ARCHIVE_DIR, "*.md")))
    print(f"아카이브 파일: {len(files)}개")

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
    for month in months:
        paths = by_month[month]
        print(f"[{month}] {len(paths)}개 영상 종합 중...")
        bodies = [extract_summary_from_md(p) for p in paths]
        month_summary = summarize_month(month, bodies)
        if not month_summary:
            print(f"  [{month}] 실패, 건너뜀")
            continue
        full.append(f"\n\n---\n\n# 📅 {month} ({len(paths)}개 영상)\n\n{month_summary}")
        time.sleep(5)

    result = "\n".join(full)
    with open(PERSPECTIVE_FILE, "w", encoding="utf-8") as f:
        f.write(result)
    print(f"[저장 완료] {PERSPECTIVE_FILE}")

    # 텔레그램 발송 (월별로 나눠서 - 길이 제한 대응)
    send_telegram("🧭 *이선엽 관점 종합 (월별)* 생성 완료\n아래에 월별로 이어서 보냅니다.")
    for month in months:
        # result에서 해당 월 섹션만 잘라 보내기
        marker = f"# 📅 {month}"
        idx = result.find(marker)
        if idx == -1:
            continue
        next_idx = result.find("# 📅", idx + 1)
        section = result[idx: next_idx if next_idx != -1 else len(result)]
        send_telegram(section[:3800])
        time.sleep(1)
    print("[텔레그램 발송 완료]")


if __name__ == "__main__":
    main()
