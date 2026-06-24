import os
import requests
from datetime import datetime, timezone

YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SEEN_FILE = "seen_ids.txt"
KEYWORD = "이선엽"

def get_seen_ids():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip())

def save_seen_id(video_id):
    with open(SEEN_FILE, "a") as f:
        f.write(video_id + "\n")

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

def format_date(published_at):
    dt = datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ")
    dt = dt.replace(tzinfo=timezone.utc)
    kst = dt.astimezone(tz=None)
    return kst.strftime("%Y년 %m월 %d일 %H:%M")

def send_telegram(title, video_id, channel, published_at):
    date_str = format_date(published_at)
    text = (
        f"🎬 *이선엽 대표* 새 영상!\n\n"
        f"*{title}*\n"
        f"채널: {channel}\n"
        f"📅 업로드: {date_str}\n"
        f"https://www.youtube.com/watch?v={video_id}"
    )
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
    )

def main():
    seen = get_seen_ids()
    print(f"[INFO] 이미 본 영상 수: {len(seen)}")
    videos = search_youtube()
    new_count = 0

    for item in videos:
        vid_id       = item["id"]["videoId"]
        title        = item["snippet"]["title"]
        channel      = item["snippet"]["channelTitle"]
        published_at = item["snippet"]["publishedAt"]

        # 제목에 "이선엽" 포함된 영상만 알림
        if "이선엽" not in title:
            print(f"[SKIP-필터] {title}")
            continue

        if vid_id not in seen:
            send_telegram(title, vid_id, channel, published_at)
            save_seen_id(vid_id)
            new_count += 1
            print(f"[NEW] {title}")
        else:
            print(f"[SKIP] {title}")

    print(f"완료: 신규 {new_count}건 알림 발송")

if __name__ == "__main__":
    main()
