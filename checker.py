import os
import re
import requests
from datetime import datetime, timezone

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)

YOUTUBE_API_KEY  = os.environ["YOUTUBE_API_KEY"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
SEEN_FILE = "seen_ids.txt"
KEYWORD = "이선엽"

# 자막이 너무 길면 프롬프트 비용/토큰 절약을 위해 앞부분만 사용
MAX_TRANSCRIPT_CHARS = 15000


def get_seen_ids():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip())


def save_seen_id(vid_id):
    with open(SEEN_FILE, "a") as f:
        f.write(vid_id + "\n")


def search_youtube():
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "q": KEYWORD,
        "type": "video",
        "order": "date",
        "maxResults": 10,
        "key": YOUTUBE_API_KEY,
    }
    res = requests.get(url, params=params)
    return res.json().get("items", [])


def get_duration(vid_id):
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "contentDetails",
        "id": vid_id,
        "key": YOUTUBE_API_KEY,
    }
    res = requests.get(url, params=params)
    items = res.json().get("items", [])
    if not items:
        return "알 수 없음"
    duration = items[0]["contentDetails"]["duration"]
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration)
    if not match:
        return "알 수 없음"
    hours   = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    if hours > 0:
        return f"{hours}시간 {minutes:02d}분 {seconds:02d}초"
    elif minutes > 0:
        return f"{minutes}분 {seconds:02d}초"
    else:
        return f"{seconds}초"


def get_transcript(vid_id):
    """한국어 자막(수동/자동)을 추출한다.
    성공 시 (자막텍스트, None), 실패 시 (None, 사유문자열) 반환."""
    try:
        api = YouTubeTranscriptApi()
        # 한국어만 사용 (수동 ko, 자동생성 ko 모두 languages=['ko']로 커버됨)
        fetched = api.fetch(vid_id, languages=["ko"])
        text = " ".join(seg.text for seg in fetched if seg.text.strip())
        text = text.strip()
        if not text:
            return None, "자막이 비어 있음"
        return text, None
    except (TranscriptsDisabled, NoTranscriptFound):
        return None, "한국어 자막 없음"
    except VideoUnavailable:
        return None, "영상을 볼 수 없음"
    except Exception as e:
        # IP 차단(YouTubeRequestFailed) 등 예외도 여기서 흡수
        print(f"[자막 오류] {vid_id}: {type(e).__name__}: {str(e)[:120]}")
        return None, "자막 추출 실패"


def summarize_with_gemini(vid_id, title, transcript):
    """transcript가 있으면 자막 기반, 없으면 제목 기반(환각 위험)으로 요약."""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    )

    if transcript:
        source_block = (
            "아래는 영상의 실제 자막 전문입니다. 반드시 이 자막 내용에만 근거해 작성하세요.\n\n"
            f"[영상 제목] {title}\n\n"
            f"[자막]\n{transcript[:MAX_TRANSCRIPT_CHARS]}"
        )
    else:
        source_block = (
            "※ 이 영상은 자막을 가져올 수 없어 제목만 제공됩니다.\n"
            "자막이 없으므로 실제 발언 내용을 알 수 없습니다. "
            "제목만으로 합리적으로 추정 가능한 범위에서만 작성하고, "
            "확신할 수 없는 내용은 단정하지 마세요.\n\n"
            f"[영상 제목] {title}"
        )

    prompt = (
        "당신은 주식 투자자입니다. 증권 애널리스트 이선엽 대표가 출연한 아래 YouTube 영상을 "
        "투자자 관점에서 분석해주세요.\n\n"
        f"{source_block}\n\n"
        "다음 형식으로 한국어로 간결하게 작성하세요.\n\n"
        "📄 3줄 요약\n"
        "- 영상 핵심 내용을 3줄로\n\n"
        "📌 핵심 투자 포인트 (2~3개)\n"
        "- 구체적인 종목/섹터/매크로 시그널 위주로\n\n"
        "⚠️ 주의해야 할 리스크 (1~2개)\n"
        "- 언급된 위험 요소"
    )

    body = {"contents": [{"parts": [{"text": prompt}]}]}
    res = requests.post(url, json=body)
    data = res.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print(f"[Gemini 오류] {e} / 응답: {data}")
        return "요약 실패"


def format_date(published_at):
    dt = datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ")
    dt = dt.replace(tzinfo=timezone.utc)
    kst = dt.astimezone(tz=None)
    return kst.strftime("%Y년 %m월 %d일 %H:%M")


def send_telegram(text):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
    )


def main():
    seen = get_seen_ids()
    videos = search_youtube()
    new_count = 0

    for item in videos:
        vid_id       = item["id"]["videoId"]
        title        = item["snippet"]["title"]
        channel      = item["snippet"]["channelTitle"]
        published_at = item["snippet"]["publishedAt"]

        if "이선엽" not in title:
            print(f"[SKIP-필터] {title}")
            continue

        if vid_id not in seen:
            date_str     = format_date(published_at)
            duration_str = get_duration(vid_id)

            transcript, fail_reason = get_transcript(vid_id)
            summary = summarize_with_gemini(vid_id, title, transcript)

            if transcript:
                source_note = "🟢 자막 기반 요약"
            else:
                source_note = f"🟡 제목 기반 추정 ({fail_reason}) — 실제 발언과 다를 수 있음"

            text = (
                f"🎬 *이선엽 대표* 새 영상!\n\n"
                f"*{title}*\n"
                f"채널: {channel}\n"
                f"📅 업로드: {date_str}\n"
                f"⏱ 길이: {duration_str}\n\n"
                f"{source_note}\n{summary}\n\n"
                f"_※ AI가 생성한 참고 정보이며 투자 조언이 아닙니다._\n"
                f"https://www.youtube.com/watch?v={vid_id}"
            )
            send_telegram(text)
            save_seen_id(vid_id)
            new_count += 1
            print(f"[NEW] {title} ({'자막' if transcript else '제목기반'})")
        else:
            print(f"[SKIP] {title}")

    if new_count == 0:
        now = datetime.now().strftime("%Y년 %m월 %d일 %H:%M")
        send_telegram(f"✅ 이선엽 대표 새 영상 없음\n({now} 기준)")
        print("완료: 신규 영상 없음 알림 발송")
    else:
        print(f"완료: 신규 {new_count}건 알림 발송")


if __name__ == "__main__":
    main()
