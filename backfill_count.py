"""
2026년 '이선엽' 영상이 몇 건인지 세기만 하는 스크립트.
- 자막/요약 호출 없음 → Supadata·Gemini 크레딧 전혀 안 씀
- YouTube search API만 사용 (검색 1회당 100유닛, 페이지네이션)
- 결과: 제목에 '이선엽' 포함된 2026년 영상 목록과 개수를 콘솔에 출력

실행: YOUTUBE_API_KEY 환경변수만 있으면 됨
"""
import os
import requests

YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
KEYWORD = "이선엽"
PUBLISHED_AFTER = "2026-01-01T00:00:00Z"
PUBLISHED_BEFORE = "2027-01-01T00:00:00Z"


def search_all():
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
        if not page_token:
            break
        if calls >= 20:  # 안전장치 (최대 1000건)
            print("※ 20페이지 도달, 중단")
            break
    return items, calls


def main():
    items, calls = search_all()

    # 제목에 '이선엽' 포함된 것만 (설명란 제외 = 정확·안전)
    matched = [
        it for it in items
        if "이선엽" in it["snippet"]["title"]
    ]

    print(f"검색 API 호출: {calls}회 (약 {calls*100}유닛)")
    print(f"2026년 검색 결과 총: {len(items)}건")
    print(f"제목에 '이선엽' 포함: {len(matched)}건")
    print("-" * 50)
    for it in matched:
        vid = it["id"]["videoId"]
        title = it["snippet"]["title"]
        channel = it["snippet"]["channelTitle"]
        date = it["snippet"]["publishedAt"][:10]
        print(f"{date} | {channel} | {title[:40]} | {vid}")

    print("-" * 50)
    print(f"※ 이 {len(matched)}건 각각에 대해 자막(1크레딧) + 요약(1회)이 필요합니다.")
    print(f"※ Supadata 무료 100크레딧 기준, {len(matched)}건이면 "
          + ("무료 한도 내." if len(matched) <= 100 else f"{len(matched)-100}건 초과."))


if __name__ == "__main__":
    main()
