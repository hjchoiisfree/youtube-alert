import os
import re
import requests
from datetime import datetime, timezone

YOUTUBE_API_KEY   = os.environ["YOUTUBE_API_KEY"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SEEN_FILE = "seen_ids.txt"
KEYWORD = "이선엽"

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

def get_subtitle(vid_id):
    try:
        # 한국어 자막 시도
        url = f"https://www.youtube.com/api/timedtext?v={vid_id}&lang=ko&fmt=json3"
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if res.status_code == 200 and res.text.strip():
            data = res.json()
            events = data.get("events", [])
            texts = []
            for e in events:
                for s in e.get("segs", []):
                    t = s.get("utf8", "").strip()
                    if t and t != "\n":
                        texts.append(t)
            text = " ".join(texts)
            if text:
                print(f"[자막 성공] {len(text)}자")
                return text[:4000]

        # 영어 자막 시도
        url_en = f"https://www.youtube.com/api/timedtext?v={vid_id}&lang=en&fmt=json3"
        res_en = requests.get(url_en, headers={"User-Agent": "Mozilla/5.0"})
        if res_en.status_code == 200 and res_en.text.strip():
            data = res_en.json()
            events = data.get("events", [])
            texts = []
            for e in events:
                for s in e.get("segs", []):
                    t = s.get("utf8", "").strip()
                    if t and t != "\n":
                        texts.append(t)
            text = " ".join(texts)
            if text:
                print(f"[영어 자막 성공] {len(text)}자")
                return text[:4000]

        print("[자막 없음] 자막을 찾을 수 없음")
        return None
    except Exception as e:
        print(f"[자막 오류] {e}")
        return None

def summarize_with_claude(title, subtitle_text):
    if not subtitle_text:
        return "자막 없음 — 요약 불가"
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1000,
        "messages": [
            {
                "role": "user",
                "content": (
                    f"다음은 YouTube 영상 \"{title}\"의 자막입니다.\n\n"
                    f"{subtitle_text}\n\n"
                    "당신은 주식 투자자입니다. 이선엽 대표의 발언 중 투자자 관점에서 주목해야 할 포인트를 아래 형식으로 작성해주세요.\n\n"
                    "📌 핵심 투자 포인트 (2~3개)\n"
                    "- 각 포인트는 구체적인 종목/섹터/매크로 시그널 위주로\n\n"
                    "⚠️ 주의해야 할 리스크 (1~2개)\n"
                    "- 이선엽 대표가 언급한 위험 요소\n\n"
                    "한국어로 간결하게 작성해주세요."
                ),
            }
        ],
    }
    res = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
    data = res.json()
    return data["content"][0]["text"]

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
            subtitle     = get_subtitle(vid_id)
            summary      = summarize_with_claude(title, subtitle)

            text = (
                f"🎬 *이선엽 대표* 새 영상!\n\n"
                f"*{title}*\n"
                f"채널: {channel}\n"
                f"📅 업로드: {date_str}\n"
                f"⏱ 길이: {duration_str}\n\n"
                f"📝 *투자 포인트*\n{summary}\n\n"
                f"https://www.youtube.com/watch?v={vid_id}"
            )
            send_telegram(text)
            save_seen_id(vid_id)
            new_count += 1
            print(f"[NEW] {title}")
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
