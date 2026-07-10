"""
2026년 '이선엽' 영상 전량 백필 스크립트 (1회성).
- Supadata 유료 크레딧으로 227건 한 번에 처리
- 자막 있는 것만 md 아카이브 저장 (자막 없으면 건너뜀)
- 텔레그램 발송 안 함 (조용히 저장만)
- 처리한 영상 ID를 seen_ids.txt에도 기록 → 일상 봇이 재처리 안 함
- 중간에 끊겨도 다시 실행하면 남은 것부터 이어서 처리

checker.py의 함수를 그대로 재사용하므로 같은 폴더에 있어야 함.
실행: 필요한 환경변수(YOUTUBE_API_KEY, GEMINI_API_KEY, SUPADATA_API_KEY) 필요.
      (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID는 checker import를 위해 필요하지만 발송엔 안 씀)
"""
import os
import time
import requests

import checker  # 자막/요약/아카이브/태그 함수 재사용

KEYWORD = "이선엽"
PUBLISHED_AFTER = "2026-01-01T00:00:00Z"
PUBLISHED_BEFORE = "2027-01-01T00:00:00Z"
YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]

# 요약 요청 사이 간격(초) - Gemini 무료 한도(RPM) 보호용
SLEEP_BETWEEN = 5


def search_all_2026():
    """2026년 '이선엽' 영상을 페이지네이션으로 모두 수집."""
    url = "https://www.googleapis.com/youtube/v3/search"
    items = []
    page_token = None
    calls = 0
    while True:
        params = {
            "part": "snippet",
            "q": KEYWORD,
            "type": "video",
            "order": "date",
            "maxResults": 50,
            "publishedAfter": PUBLISHED_AFTER,
            "publishedBefore": PUBLISHED_BEFORE,
            "key": YOUTUBE_API_KEY,
        }
        if page_token:
            params["pageToken"] = page_token
        res = requests.get(url, params=params).json()
        calls += 1
        items.extend(res.get("items", []))
        page_token = res.get("nextPageToken")
        if not page_token or calls >= 20:
            break
    return items


def main():
    seen = checker.get_seen_ids()
    videos = search_all_2026()

    # 제목에 '이선엽' 포함 + 아직 처리 안 한 것만
    todo = [
        it for it in videos
        if "이선엽" in it["snippet"]["title"]
        and it["id"]["videoId"] not in seen
    ]

    print(f"2026년 검색 결과: {len(videos)}건")
    print(f"제목 매칭 & 미처리: {len(todo)}건 처리 시작")
    print("=" * 50)

    done, skipped, failed = 0, 0, 0

    for i, item in enumerate(todo, 1):
        vid_id       = item["id"]["videoId"]
        title        = item["snippet"]["title"]
        channel      = item["snippet"]["channelTitle"]
        published_at = item["snippet"]["publishedAt"]

        print(f"[{i}/{len(todo)}] {title[:40]} ({vid_id})")

        date_str     = checker.format_date(published_at)
        duration_str = checker.get_duration(vid_id)

        # 자막 추출 (Supadata 우선)
        transcript, fail_reason = checker.get_transcript(vid_id)

        # 자막 없으면 건너뛰기 (방침: 자막 있는 것만 아카이브)
        if not transcript:
            print(f"   → 자막 없음({fail_reason}), 건너뜀")
            skipped += 1
            # seen에는 넣어서 다음 실행 때 또 시도하지 않게 함
            checker.save_seen_id(vid_id)
            continue

        # 요약
        summary = checker.summarize_with_gemini(title, transcript)
        if summary.startswith("요약 실패"):
            print(f"   → {summary}, 이번엔 건너뜀(다음 실행 때 재시도)")
            failed += 1
            time.sleep(SLEEP_BETWEEN)
            continue

        tags = checker.extract_topics(title, transcript)

        # 아카이브 저장 (텔레그램 발송 없음)
        checker.save_archive(vid_id, title, channel, date_str, duration_str,
                             summary, transcript, tags, True)
        checker.save_seen_id(vid_id)
        done += 1
        print(f"   → 저장 완료 [{' '.join(tags) if tags else '태그없음'}]")

        time.sleep(SLEEP_BETWEEN)

    print("=" * 50)
    print(f"완료: 저장 {done}건 / 자막없음 건너뜀 {skipped}건 / 요약실패 {failed}건")
    if failed:
        print("※ 요약 실패분은 다시 실행하면 이어서 재시도됩니다.")


if __name__ == "__main__":
    main()
