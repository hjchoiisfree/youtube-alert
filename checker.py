import os
import re
import time
import requests
from datetime import datetime, timezone

# 백업용 라이브러리 (Supadata 실패 시 시도). 없어도 동작하도록 방어.
try:
    from youtube_transcript_api import YouTubeTranscriptApi
    _HAS_YTAPI = True
except Exception:
    _HAS_YTAPI = False

YOUTUBE_API_KEY  = os.environ["YOUTUBE_API_KEY"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
# Supadata 키는 없을 수도 있으니 get으로 (없으면 백업 방식만 시도)
SUPADATA_API_KEY = os.environ.get("SUPADATA_API_KEY", "")

SEEN_FILE = "seen_ids.txt"
ARCHIVE_DIR = "archive"
KEYWORD = "이선엽"

# 요약에 넣을 자막 최대 길이 (토큰/비용 절약)
MAX_TRANSCRIPT_CHARS = 20000
# 아카이브에 원문 자막을 접어서 저장할 때 최대 길이
MAX_ARCHIVE_TRANSCRIPT = 40000

# 주제 태그 자동 분류용 키워드 사전
TOPIC_KEYWORDS = {
    "금리": ["금리", "기준금리", "연준", "fomc", "인상", "인하"],
    "환율": ["환율", "달러", "원화", "엔화", "위안"],
    "반도체": ["반도체", "hbm", "삼성전자", "sk하이닉스", "엔비디아", "ai칩"],
    "부동산": ["부동산", "아파트", "전세", "pf", "프로젝트파이낸싱"],
    "증시전망": ["코스피", "코스닥", "나스닥", "s&p", "폭락", "조정", "상승장", "하락장"],
    "채권": ["채권", "국채", "스프레드", "장단기"],
    "인플레이션": ["인플레이션", "물가", "cpi", "스태그플레이션"],
    "정책": ["정부", "규제", "정책", "감세", "재정"],
}


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


def get_duration_seconds(vid_id):
    """영상 길이를 초 단위 정수로 반환 (길이 필터용). 실패 시 -1."""
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {"part": "contentDetails", "id": vid_id, "key": YOUTUBE_API_KEY}
    res = requests.get(url, params=params)
    items = res.json().get("items", [])
    if not items:
        return -1
    duration = items[0]["contentDetails"]["duration"]
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration)
    if not match:
        return -1
    h = int(match.group(1) or 0)
    m = int(match.group(2) or 0)
    s = int(match.group(3) or 0)
    return h * 3600 + m * 60 + s


# ────────────────────────────────────────────────
# 자막 추출: Supadata(서버 기반, IP 차단 없음) 우선 → 라이브러리 백업
# ────────────────────────────────────────────────

def _transcript_via_supadata(vid_id):
    """Supadata API로 자막 추출. 20분+ 영상은 비동기(202) → 폴링."""
    if not SUPADATA_API_KEY:
        return None, "Supadata 키 없음"

    video_url = f"https://www.youtube.com/watch?v={vid_id}"
    endpoint = "https://api.supadata.ai/v1/transcript"
    headers = {"x-api-key": SUPADATA_API_KEY}
    # mode=auto: 자막 있으면 가져오고, 없으면 AI로 생성(무료 크레딧 내)
    params = {"url": video_url, "text": "true", "lang": "ko", "mode": "auto"}

    try:
        res = requests.get(endpoint, params=params, headers=headers, timeout=90)
    except Exception as e:
        return None, f"Supadata 요청 오류: {type(e).__name__}"

    # 성공 (즉시 반환)
    if res.status_code == 200:
        data = res.json()
        content = data.get("content", "")
        if isinstance(content, list):  # 혹시 청크 형식이면 합치기
            content = " ".join(seg.get("text", "") for seg in content)
        content = (content or "").strip()
        return (content, None) if content else (None, "Supadata: 내용 없음")

    # 비동기 처리 (20분+ 영상) → jobId 폴링
    if res.status_code == 202:
        job_id = res.json().get("jobId")
        if not job_id:
            return None, "Supadata: jobId 없음"
        poll_url = f"{endpoint}/{job_id}"
        for _ in range(60):  # 최대 약 120초 대기
            time.sleep(2)
            try:
                pr = requests.get(poll_url, headers=headers, timeout=30)
                pdata = pr.json()
            except Exception:
                continue
            status = pdata.get("status")
            if status == "completed":
                content = pdata.get("content", "")
                if isinstance(content, list):
                    content = " ".join(seg.get("text", "") for seg in content)
                content = (content or "").strip()
                return (content, None) if content else (None, "Supadata: 내용 없음")
            if status == "failed":
                return None, f"Supadata 생성 실패: {str(pdata.get('error',''))[:40]}"
        return None, "Supadata: 시간 초과"

    if res.status_code == 206:
        return None, "Supadata: 자막 없음(206)"
    return None, f"Supadata 오류 {res.status_code}"


def _transcript_via_library(vid_id):
    """백업: youtube-transcript-api (클라우드 IP에서 차단될 수 있음)."""
    if not _HAS_YTAPI:
        return None, "라이브러리 없음"
    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(vid_id, languages=["ko"])
        text = " ".join(seg.text for seg in fetched if seg.text.strip()).strip()
        return (text, None) if text else (None, "자막 비어 있음")
    except Exception as e:
        return None, f"라이브러리 실패: {type(e).__name__}"


def get_transcript(vid_id):
    """자막을 추출한다. 성공 시 (텍스트, None), 실패 시 (None, 사유)."""
    # 1순위: Supadata
    text, reason = _transcript_via_supadata(vid_id)
    if text:
        return text, None
    print(f"[자막] Supadata 실패({reason}), 라이브러리 시도")

    # 2순위: 라이브러리
    text2, reason2 = _transcript_via_library(vid_id)
    if text2:
        return text2, None

    # 둘 다 실패
    return None, f"{reason} / {reason2}"


# ────────────────────────────────────────────────
# 요약
# ────────────────────────────────────────────────

def summarize_with_gemini(title, transcript):
    """transcript가 있으면 자막 기반, 없으면 제목 기반(환각 위험)으로 요약."""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-3.1-flash-lite:generateContent?key={GEMINI_API_KEY}"
    )

    if transcript:
        source_block = (
            "아래는 영상의 실제 자막 전문입니다. 반드시 이 자막 내용에만 근거해 작성하세요. "
            "자막에 없는 내용을 지어내지 마세요.\n\n"
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
        "투자자 관점에서 분석해주세요. 저는 이선엽 대표의 시장을 보는 관점과 논리를 "
        "꾸준히 학습해 제 것으로 만들고자 합니다.\n\n"
        f"{source_block}\n\n"
        "다음 형식으로 한국어로 작성하세요. 중요한 부분은 **볼드**로 강조하세요.\n\n"
        "### 📄 3줄 요약\n"
        "- 영상 핵심 내용을 3줄로\n\n"
        "### 🧭 이선엽의 관점·논리\n"
        "- 그가 시장을 어떤 프레임으로 보는지, 어떤 근거로 그렇게 판단하는지 2~3개\n\n"
        "### 🎙 진행자 Q & 이선엽 A\n"
        "- 이선엽 대표는 보통 게스트로 출연해 진행자의 질문에 답하지만, 진행자 없이 "
        "혼자 말하는 영상(숏폼 등)도 있습니다.\n"
        "- **중요: 자막에 실제로 진행자의 질문이 있을 때만** 'Q. 질문 / A. 답변' 형태로 정리하세요. "
        "질문이 없는데 있는 것처럼 지어내지 마세요.\n"
        "- 자막에 진행자 질문이 전혀 없으면 이 섹션에는 '진행자 질문 없음 (단독 발언 영상)'이라고만 쓰세요.\n"
        "- 답변은 핵심만 요약하되, 중요한 문장은 **볼드** 처리하세요.\n\n"
        "### 🎯 이선엽이 주목한 섹터·테마\n"
        "- 이선엽 대표는 보통 개별 종목 추천은 하지 않습니다. 그가 실제로 긍정적으로 "
        "언급하거나 방향성을 제시한 섹터/테마만 뽑으세요.\n"
        "- 각 항목에 그렇게 본 근거(실제 발언 내용)를 짧게 붙이세요.\n"
        "- 만약 특정 종목명을 언급했다면 그가 말한 맥락 그대로만 적고, '매수하라'는 식으로 각색하지 마세요.\n"
        "- 자막에 섹터 언급이 전혀 없으면 이 항목은 '언급 없음'이라고만 쓰세요.\n\n"
        "### ⚠️ 주의해야 할 리스크\n"
        "- 그가 경계한 위험 요소 1~2개"
    )

    body = {"contents": [{"parts": [{"text": prompt}]}]}

    data = None
    for attempt in range(3):
        try:
            res = requests.post(url, json=body, timeout=90)
            data = res.json()
        except Exception as e:
            print(f"[Gemini 요청 오류] {e}")
            return "요약 실패 (Gemini 요청 오류)"

        err = data.get("error", {})
        if err.get("code") == 429 and "per day" not in err.get("message", "").lower():
            wait = 20 * (attempt + 1)
            print(f"[Gemini 429] rate limit, {wait}초 대기 후 재시도 ({attempt+1}/3)")
            time.sleep(wait)
            continue
        break

    if data and "error" in data:
        msg = data["error"].get("message", "알 수 없는 오류")
        print(f"[Gemini API 오류] {msg}")
        return f"요약 실패 ({msg[:80]})"

    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print(f"[Gemini 파싱 오류] {e} / 응답: {data}")
        return "요약 실패 (응답 파싱 불가)"


# ────────────────────────────────────────────────
# 아카이브 저장
# ────────────────────────────────────────────────

def extract_topics(title, transcript):
    """제목+자막에서 주제 태그를 자동 추출."""
    haystack = (title + " " + (transcript or "")).lower()
    tags = []
    for tag, kws in TOPIC_KEYWORDS.items():
        if any(kw.lower() in haystack for kw in kws):
            tags.append(tag)
    return tags


def safe_filename(text, maxlen=40):
    """파일명에 쓸 수 없는 문자 제거."""
    text = re.sub(r'[\\/:*?"<>|#\n\r]+', "", text)
    text = text.replace(" ", "-").strip("-")
    return text[:maxlen] or "untitled"


def save_archive(vid_id, title, channel, date_str, duration_str,
                 summary, transcript, tags, has_transcript):
    """영상별 Markdown 노트를 archive/ 폴더에 저장."""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    date_prefix = datetime.now().strftime("%Y-%m-%d")
    m = re.match(r"(\d{4})년 (\d{2})월 (\d{2})일", date_str)
    if m:
        date_prefix = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    fname = f"{date_prefix}_{safe_filename(title)}.md"
    path = os.path.join(ARCHIVE_DIR, fname)

    source_note = "자막 기반" if has_transcript else "제목 기반 추정(자막 없음)"
    tag_line = " ".join(f"#{t}" for t in tags) if tags else "#미분류"
    video_url = f"https://www.youtube.com/watch?v={vid_id}"

    lines = [
        f"# {title}",
        "",
        f"- **채널**: {channel}",
        f"- **업로드**: {date_str}",
        f"- **길이**: {duration_str}",
        f"- **영상**: {video_url}",
        f"- **분석 근거**: {source_note}",
        f"- **주제 태그**: {tag_line}",
        "",
        "---",
        "",
        summary,
        "",
        "---",
        "",
        "<details>",
        "<summary>📜 원문 자막 (펼치기)</summary>",
        "",
    ]
    if transcript:
        lines.append("```")
        lines.append(transcript[:MAX_ARCHIVE_TRANSCRIPT])
        if len(transcript) > MAX_ARCHIVE_TRANSCRIPT:
            lines.append("\n...(이하 생략)...")
        lines.append("```")
    else:
        lines.append("_자막을 가져오지 못했습니다._")
    lines += ["", "</details>", ""]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[아카이브 저장] {path}")
    return path


# ────────────────────────────────────────────────

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


PERSPECTIVE_FILE = "관점_종합.md"
MIN_DURATION_SEC = 600  # 10분 = 600초


def update_perspective(new_items):
    """관점_종합.md를 롤링 갱신. new_items는 이번에 새로 처리한
    영상들의 (title, date_str, tags, summary) 리스트.
    시장 종합 + 추천 섹터/종목 두 카테고리로 작성. 갱신본 반환."""
    prev = ""
    if os.path.exists(PERSPECTIVE_FILE):
        with open(PERSPECTIVE_FILE, "r", encoding="utf-8") as f:
            prev = f.read()

    # 이번에 새로 추가된 영상 요약들을 하나로 합침
    new_block = ""
    for (title, date_str, tags, summary) in new_items:
        new_block += (
            f"\n\n=== 신규 영상: {title} ({date_str}) [{', '.join(tags)}] ===\n{summary}"
        )

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-3.1-flash-lite:generateContent?key={GEMINI_API_KEY}"
    )
    prompt = (
        "당신은 이선엽 대표의 시장 관점을 누적 정리하는 애널리스트입니다.\n"
        "아래 [기존 종합]에 [이번 신규 영상들]의 내용을 반영해 종합본을 갱신하세요.\n\n"
        "반드시 아래 두 카테고리로 나눠 마크다운으로 작성하세요:\n\n"
        "## 📊 시장 종합\n"
        "- 이선엽이 현재 시장 전체를 보는 큰 그림(강세/약세 판단, 핵심 변수, 주요 논리).\n"
        "- 관점이 시간에 따라 어떻게 바뀌었는지 날짜와 함께 흐름이 보이게 정리.\n\n"
        "## 🎯 추천 섹터·종목\n"
        "- 이선엽이 반복해서 주목한 섹터·테마를 상위에 정리(언제 언급했는지 날짜 포함).\n"
        "- 이선엽은 개별 종목 추천을 하지 않습니다. 그가 실제로 이름을 언급한 종목이 있으면 "
        "'추천'이 아니라 '언급된 맥락' 그대로만 기록하세요. 매수 신호처럼 각색 금지.\n\n"
        "규칙:\n"
        "- 같은 섹터/주제가 반복되면 최신 내용을 덧붙이되 과거도 날짜와 함께 남겨 흐름을 보존하세요.\n"
        "- 너무 오래되고 더 이상 언급 안 되는 항목은 압축하세요.\n\n"
        f"[기존 종합]\n{prev if prev else '(아직 없음 - 처음부터 작성)'}\n"
        f"\n[이번 신규 영상들]{new_block}"
    )
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        res = requests.post(url, json=body, timeout=120)
        data = res.json()
        updated = data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print(f"[관점_종합 갱신 오류] {e}")
        return None

    with open(PERSPECTIVE_FILE, "w", encoding="utf-8") as f:
        f.write(updated)
    print("[관점_종합 갱신 완료]")
    return updated


def main():
    seen = get_seen_ids()
    videos = search_youtube()
    new_count = 0
    new_items = []  # 이번에 처리한 (title, date_str, tags, summary)

    for item in videos:
        vid_id       = item["id"]["videoId"]
        title        = item["snippet"]["title"]
        channel      = item["snippet"]["channelTitle"]
        published_at = item["snippet"]["publishedAt"]

        if "이선엽" not in title:
            print(f"[SKIP-필터] {title}")
            continue

        if vid_id in seen:
            print(f"[SKIP] {title}")
            continue

        # 10분 미만 영상(숏폼 등)은 요약하지 않고 건너뜀
        dur_sec = get_duration_seconds(vid_id)
        if 0 <= dur_sec < MIN_DURATION_SEC:
            print(f"[SKIP-길이] {title} ({dur_sec}초 < 10분)")
            save_seen_id(vid_id)  # 다음 실행 때 또 안 걸리게 기록
            continue

        date_str     = format_date(published_at)
        duration_str = get_duration(vid_id)

        transcript, fail_reason = get_transcript(vid_id)
        # 자막 없으면 요약/아카이브 안 함 (자막 있는 것만)
        if not transcript:
            print(f"[SKIP-자막없음] {title} ({fail_reason})")
            save_seen_id(vid_id)
            continue

        summary = summarize_with_gemini(title, transcript)
        tags = extract_topics(title, transcript)

        # 아카이브에 노트 저장 (누적)
        save_archive(vid_id, title, channel, date_str, duration_str,
                     summary, transcript, tags, True)

        # 개별 영상 요약 메시지 발송
        tag_line = " ".join(f"#{t}" for t in tags) if tags else ""
        video_msg = (
            f"🎬 *이선엽 대표* 새 영상 · 노트 추가됨\n\n"
            f"*{title}*\n채널: {channel}\n"
            f"📅 {date_str}  ⏱ {duration_str}\n{tag_line}\n\n"
            f"{summary}\n\n"
            f"_※ AI 생성 참고 정보이며 투자 조언이 아닙니다._\n"
            f"https://www.youtube.com/watch?v={vid_id}"
        )
        send_telegram(video_msg)

        new_items.append((title, date_str, tags, summary))
        save_seen_id(vid_id)
        new_count += 1
        print(f"[NEW] {title}")

    # 루프 종료 후: 신규 영상이 있으면 종합 관점을 별도 메시지 1개로 발송
    if new_count > 0:
        updated = update_perspective(new_items)
        if updated:
            persp_msg = (
                f"🧭 *이선엽 관점 종합* (신규 {new_count}건 반영)\n\n"
                f"{updated[:3800]}\n\n"
                f"_※ AI 생성 참고 정보이며 투자 조언이 아닙니다._"
            )
            send_telegram(persp_msg)
            print("[관점 종합 메시지 발송]")

    if new_count == 0:
        now = datetime.now().strftime("%Y년 %m월 %d일 %H:%M")
        send_telegram(f"✅ 이선엽 대표 새 영상 없음\n({now} 기준)")
        print("완료: 신규 영상 없음 알림 발송")
    else:
        print(f"완료: 신규 {new_count}건 알림 발송 + 아카이브 저장")


if __name__ == "__main__":
    main()
