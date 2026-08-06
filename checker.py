import os
import re
import json
import html
import time
import requests
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))  # 한국 시간 (GitHub Actions 러너는 UTC라서 명시 필요)

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

# 표기 흔들림·오타 대응. 실제로 채널 설명란에 '이선혁'으로 잘못 적힌 사례가 있었다.
# 새 오타를 발견하면 여기에 추가한다.
KEYWORD_VARIANTS = ["이선엽", "이선혁", "이 선엽", "이선 엽"]

# 자동 학습된 출연 채널 목록 (git에 커밋되어 실행 간 유지된다)
CHANNELS_FILE = "channels.json"
# 필터에서 걸러진 영상 기록 (Gemini 오판 사후 검증용)
REJECTED_LOG = "rejected.log"
# 마지막 놓침 점검 실행일
AUDIT_FILE = "last_audit.txt"

# 놓침 점검 주기(일)와 조회 범위(일)
AUDIT_INTERVAL_DAYS = 7
AUDIT_LOOKBACK_DAYS = 30

# 길이·자막 필터로 버려진 영상도 텔레그램으로 알릴지 여부
NOTIFY_SKIPS = False


def _has_keyword(text):
    """키워드 변형 중 하나라도 포함되면 True."""
    if not text:
        return False
    return any(k in text for k in KEYWORD_VARIANTS)

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


# 검색 범위: 최근 N일 이내 영상을 페이지 넘기며 전부 수집
LOOKBACK_DAYS = 14
# 쿼터 보호용 최대 페이지 수 (1페이지=50개, 검색 1회당 쿼터 100 소모)
MAX_SEARCH_PAGES = 3


def search_youtube():
    """'이선엽' 검색 결과를 최신순으로 페이지 넘기며 수집한다.
    최근 LOOKBACK_DAYS일 이내 영상만, 최대 MAX_SEARCH_PAGES 페이지(150개)까지."""
    url = "https://www.googleapis.com/youtube/v3/search"
    published_after = (
        datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    all_items = []
    page_token = None
    for page in range(MAX_SEARCH_PAGES):
        params = {
            "part": "snippet",
            "q": KEYWORD,
            "type": "video",
            "order": "date",
            "maxResults": 50,
            "publishedAfter": published_after,
            "key": YOUTUBE_API_KEY,
        }
        if page_token:
            params["pageToken"] = page_token

        try:
            res = requests.get(url, params=params, timeout=30)
            data = res.json()
        except Exception as e:
            print(f"[검색 오류] {page+1}페이지: {e}")
            break

        if "error" in data:
            print(f"[검색 API 오류] {data['error'].get('message', '')[:80]}")
            break

        items = data.get("items", [])
        all_items.extend(items)
        print(f"[검색] {page+1}페이지: {len(items)}건 (누적 {len(all_items)}건)")

        page_token = data.get("nextPageToken")
        if not page_token or not items:
            break  # 더 이상 페이지 없음 → 기간 내 영상 전부 수집 완료

    return all_items


# 이선엽 대표가 자주 출연하는 채널 (채널ID: 표시명) — 수동 시드.
# 여기에 없어도 키워드 검색으로 한 번 잡히면 channels.json에 자동 학습된다.
CHANNELS = {
    "UCLv3v82YNNsa8EsxrcPMjGQ": "SBS 시사교양 라디오 - 시교라",
    "UC6kZpTl39-_SqfBrF1-N2oQ": "연합뉴스경제TV",
    "UCD0k4Kq7SJROxxV-9N5v8IA": "깨비증권 마블TV [KB증권]",
}

# 채널당 조회할 최대 페이지 수 (1페이지=50개).
# playlistItems는 호출당 쿼터 1이라 넉넉히 잡아도 부담이 없다.
# 정치·뉴스 클립을 하루 수십 개 올리는 채널은 2페이지로는 14일을 못 채운다.
MAX_CHANNEL_PAGES = 8


def load_channels():
    """수동 시드(CHANNELS) + 자동 학습분(channels.json)을 합쳐 반환."""
    learned = {}
    if os.path.exists(CHANNELS_FILE):
        try:
            with open(CHANNELS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            learned = {cid: v.get("name", cid) for cid, v in data.items()}
        except Exception as e:
            print(f"[채널목록 로드 오류] {e}")

    merged = dict(CHANNELS)
    merged.update(learned)
    return merged


def learn_channel(channel_id, channel_name):
    """출연이 확인된 영상의 채널을 자동 학습 목록에 기록한다.
    이미 있으면 히트 수와 최근 출연일만 갱신한다."""
    if not channel_id:
        return

    data = {}
    if os.path.exists(CHANNELS_FILE):
        try:
            with open(CHANNELS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    today = datetime.now(KST).strftime("%Y-%m-%d")
    if channel_id in data:
        data[channel_id]["hits"] = data[channel_id].get("hits", 0) + 1
        data[channel_id]["last_hit"] = today
    else:
        data[channel_id] = {
            "name": channel_name,
            "added": today,
            "last_hit": today,
            "hits": 1,
        }
        print(f"[채널 학습] 신규 등록: {channel_name} ({channel_id})")

    try:
        with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[채널목록 저장 오류] {e}")


def fetch_channel_uploads(channel_id, channel_name, cutoff_iso):
    """채널 업로드 플레이리스트를 최신순으로 읽어 cutoff 이후 영상만 반환.
    업로드 플레이리스트 ID는 채널ID의 'UC'를 'UU'로 바꾼 값이다.
    playlistItems는 호출당 쿼터 1 (search.list는 100)이라 훨씬 저렴하다."""
    playlist_id = "UU" + channel_id[2:]
    url = "https://www.googleapis.com/youtube/v3/playlistItems"
    out, page_token = [], None

    for _ in range(MAX_CHANNEL_PAGES):
        params = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": 50,
            "key": YOUTUBE_API_KEY,
        }
        if page_token:
            params["pageToken"] = page_token

        try:
            data = requests.get(url, params=params, timeout=30).json()
        except Exception as e:
            print(f"[채널 조회 오류] {channel_name}: {e}")
            break

        if "error" in data:
            print(f"[채널 API 오류] {channel_name}: "
                  f"{data['error'].get('message', '')[:80]}")
            break

        stop = False
        for it in data.get("items", []):
            pub = it["contentDetails"].get("videoPublishedAt", "")
            if not pub:
                continue
            if pub < cutoff_iso:      # 최신순 정렬이므로 기간을 벗어나면 중단
                stop = True
                break
            # search 결과와 같은 형태로 맞춰서 main()이 그대로 처리하게 함
            out.append({
                "id": {"videoId": it["contentDetails"]["videoId"]},
                "snippet": {
                    "title": it["snippet"]["title"],
                    "channelTitle": channel_name,
                    "channelId": channel_id,
                    "publishedAt": pub,
                    "description": it["snippet"].get("description", ""),
                },
                # playlistItems는 설명란 전체를 주므로 추가 조회가 불필요하다는 표시
                "_full_desc": True,
            })

        if stop or not data.get("nextPageToken"):
            break
        page_token = data["nextPageToken"]

    print(f"[채널] {channel_name}: {len(out)}건")
    return out


def collect_videos():
    """키워드 검색 + 채널 순회를 합치고 videoId 기준으로 중복 제거.

    두 경로는 서로를 보완한다.
    - 검색: 모르는 채널까지 닿지만, 관련성 점수로 임의로 잘려 신뢰도가 낮다.
    - 채널 순회: 등록된 채널만 보지만 그 채널 업로드는 100% 가져온다.
    """
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    merged, seen_vids = [], set()

    for item in search_youtube():
        vid = item["id"]["videoId"]
        if vid not in seen_vids:
            seen_vids.add(vid)
            merged.append(item)
    search_count = len(merged)

    channels = load_channels()
    print(f"[채널 목록] 총 {len(channels)}개 "
          f"(시드 {len(CHANNELS)} + 학습 {len(channels) - len(CHANNELS)})")

    for cid, cname in channels.items():
        for item in fetch_channel_uploads(cid, cname, cutoff):
            vid = item["id"]["videoId"]
            if vid not in seen_vids:
                seen_vids.add(vid)
                merged.append(item)

    print(f"[수집 완료] 총 {len(merged)}건 "
          f"(검색 {search_count} + 채널 {len(merged) - search_count})")
    return merged


def get_full_description(vid_id):
    """videos API로 영상의 전체 설명란을 가져온다.
    (search API의 snippet.description은 앞부분만 잘려서 오기 때문에,
    출연자 목록이 설명 뒷부분에 있는 경우를 놓치지 않기 위함)"""
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {"part": "snippet", "id": vid_id, "key": YOUTUBE_API_KEY}
    try:
        res = requests.get(url, params=params, timeout=30)
        items = res.json().get("items", [])
        if not items:
            return ""
        return items[0]["snippet"].get("description", "")
    except Exception as e:
        print(f"[설명 조회 오류] {vid_id}: {e}")
        return ""


def verify_appearance_with_gemini(title, description):
    """설명란에서만 '이선엽'이 발견된 애매한 경우, 실제 출연 영상인지 판별.
    설명란의 추천 영상 링크·해시태그에만 이름이 있는 가짜 매칭을 걸러낸다.
    반환: True(출연) / False(미출연). API 오류 시 True(놓치는 것보다 안전)."""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-3.1-flash-lite:generateContent?key={GEMINI_API_KEY}"
    )
    prompt = (
        "당신은 YouTube 영상 필터입니다. 아래 영상에 증권 애널리스트 '이선엽' 대표가 "
        "실제로 출연(발언자/게스트/진행자)하는지 판단하세요.\n\n"
        "판단 규칙:\n"
        "- 제목이나 설명란의 출연자 소개에 이선엽이 있으면 YES.\n"
        "- '이선혁' 처럼 한 글자만 다른 비슷한 이름이 출연자 소개 위치에 적혀 있고, "
        "해시태그나 다른 곳에는 '이선엽'이 있다면 오타로 보고 YES.\n"
        "- 설명란의 '추천 영상', '지난 방송', '관련 영상' 링크 목록이나 "
        "채널 상용구에만 이선엽이 등장하고 이 영상 자체에는 다른 사람이 출연하면 NO.\n"
        "- 다만 해시태그에 #이선엽이 있으면서 본문에도 출연자로 보이는 언급이 있으면 "
        "YES로 판단하세요. 해시태그만 있다고 무조건 NO는 아닙니다.\n"
        "- 제목에 다른 출연자 이름이 명시돼 있고 이선엽은 링크 목록에만 보이면 NO.\n"
        "- 여러 명이 함께 출연하는 방송이라도 이선엽이 그중 한 명이면 YES.\n\n"
        f"[제목] {title}\n\n[설명란]\n{description[:3000]}\n\n"
        "반드시 YES 또는 NO 한 단어로만 답하세요."
    )
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        res = requests.post(url, json=body, timeout=60)
        data = res.json()
        answer = data["candidates"][0]["content"]["parts"][0]["text"].strip().upper()
        verdict = answer.startswith("YES")
        print(f"[출연검증] {title[:40]} → {answer[:10]}")
        return verdict
    except Exception as e:
        print(f"[출연검증 오류] {e} → 일단 통과 처리")
        return True


def matches_keyword(title, snippet_desc, vid_id, desc_is_full=False):
    """제목 → 설명란 순으로 키워드를 검사한다.
    반환: (통과 여부, 사유코드)
      title      - 제목에서 발견 (확실)
      gemini-ok  - 설명란에서 발견 + Gemini가 출연 인정
      gemini-no  - 설명란에서 발견했으나 Gemini가 미출연 판정
      no-keyword - 제목·설명란 어디에도 없음
    """
    if _has_keyword(title):
        return True, "title"

    if desc_is_full:
        # 채널 순회 결과 → 이미 전체 설명란을 갖고 있어 추가 조회 불필요
        description = snippet_desc
    else:
        # 전체 설명란 확보 (search API의 snippet.description은 잘려서 와서
        # 링크 목록인지 출연자 소개인지 문맥 판단이 어려움 → 전체를 가져온다)
        description = get_full_description(vid_id) or snippet_desc

    if not _has_keyword(description):
        return False, "no-keyword"

    # 설명란에만 이름이 있는 애매한 경우 → 출연 여부 검증
    if verify_appearance_with_gemini(title, description):
        return True, "gemini-ok"
    return False, "gemini-no"


def log_rejection(reason, title, vid_id, channel=""):
    """필터에서 걸러진 영상을 기록한다.
    단순 키워드 미포함은 양이 너무 많아 남기지 않고,
    Gemini가 미출연으로 판정한 '애매한 건'만 사후 검증용으로 쌓는다."""
    line = (
        f"{datetime.now(KST).strftime('%Y-%m-%d %H:%M')}\t{reason}\t"
        f"{channel}\t{title}\thttps://www.youtube.com/watch?v={vid_id}\n"
    )
    try:
        with open(REJECTED_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print(f"[거부 로그 저장 오류] {e}")


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

    date_prefix = datetime.now(KST).strftime("%Y-%m-%d")
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
    kst = dt.astimezone(KST)
    return kst.strftime("%Y년 %m월 %d일 %H:%M")


def to_telegram(md):
    """마크다운을 텔레그램 레거시 Markdown이 이해하는 형태로 변환.
    텔레그램은 ##/### 헤더와 **볼드**를 지원하지 않는다(후자는 파싱 오류 유발)."""
    if not md:
        return md
    out = []
    for line in md.split("\n"):
        s = line.strip()
        if s.startswith("#"):                       # ### 제목 → *제목*
            t = s.lstrip("#").strip()
            out.append(f"*{t}*" if t else "")
            continue
        if set(s) <= {"-", "─", "=", "*", "_"} and len(s) >= 3:  # --- → 구분선
            out.append("━━━━━━━━━━━━━━━")
            continue
        line = re.sub(r"\*\*(.+?)\*\*", r"*\1*", line)  # **볼드** → *볼드*
        out.append(line)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)          # 빈 줄 3개 이상 압축
    return text.strip()


def clip(text, limit=3800):
    """텔레그램 길이 제한에 맞춰 자르되, 문장/줄 중간에서 끊지 않는다."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    nl = cut.rfind("\n")
    if nl > limit * 0.7:
        cut = cut[:nl]
    return cut.rstrip() + "\n\n…(이하 생략)"


def send_telegram(text):
    """Markdown으로 시도하고, 파싱 오류가 나면 서식 없이 재전송한다.
    (서식 하나 깨졌다고 알림 자체를 놓치는 일을 막기 위함)"""
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        res = requests.post(
            api,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=30,
        )
        if res.status_code == 200:
            return True
        print(f"[텔레그램 오류 {res.status_code}] {res.text[:120]} → 평문 재시도")
    except Exception as e:
        print(f"[텔레그램 요청 오류] {e} → 평문 재시도")

    try:
        res = requests.post(
            api, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=30
        )
        return res.status_code == 200
    except Exception as e:
        print(f"[텔레그램 재시도 실패] {e}")
        return False


PERSPECTIVE_FILE = "관점_종합.md"
MIN_DURATION_SEC = 600  # 10분 = 600초


# ────────────────────────────────────────────────
# 놓침 점검 (주기적 감사)
# ────────────────────────────────────────────────

def should_run_audit():
    """마지막 점검으로부터 AUDIT_INTERVAL_DAYS 이상 지났는지."""
    if not os.path.exists(AUDIT_FILE):
        return True
    try:
        with open(AUDIT_FILE, encoding="utf-8") as f:
            last = datetime.strptime(f.read().strip(), "%Y-%m-%d").date()
    except Exception:
        return True
    return (datetime.now(KST).date() - last).days >= AUDIT_INTERVAL_DAYS


def mark_audit_done():
    try:
        with open(AUDIT_FILE, "w", encoding="utf-8") as f:
            f.write(datetime.now(KST).strftime("%Y-%m-%d"))
    except Exception as e:
        print(f"[점검일 기록 오류] {e}")


def audit_missed():
    """평소와 '다른 각도'로 검색해 놓친 영상이 있는지 점검한다.

    본 수집은 order=date + 14일이라 관련성 상위권만 훑는다.
    여기서는 order=relevance + 30일로 돌려 다른 결과 집합을 얻고,
    그중 seen_ids에 없는 것을 후보로 보고한다.
    자동 처리하지 않고 사람이 판단하도록 목록만 알린다."""
    seen = get_seen_ids()
    url = "https://www.googleapis.com/youtube/v3/search"
    published_after = (
        datetime.now(timezone.utc) - timedelta(days=AUDIT_LOOKBACK_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    candidates, page_token = [], None
    for page in range(2):
        params = {
            "part": "snippet",
            "q": KEYWORD,
            "type": "video",
            "order": "relevance",     # 본 수집(date)과 다른 정렬 → 다른 결과 집합
            "maxResults": 50,
            "publishedAfter": published_after,
            "key": YOUTUBE_API_KEY,
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            data = requests.get(url, params=params, timeout=30).json()
        except Exception as e:
            print(f"[점검 오류] {e}")
            break
        if "error" in data:
            print(f"[점검 API 오류] {data['error'].get('message', '')[:80]}")
            break

        for it in data.get("items", []):
            vid = it["id"].get("videoId")
            if not vid or vid in seen:
                continue
            title = html.unescape(it["snippet"]["title"])
            candidates.append((vid, title, it["snippet"]["channelTitle"]))

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    mark_audit_done()

    if not candidates:
        print("[놓침 점검] 후보 없음")
        send_telegram(
            f"🔍 *놓침 점검 완료* (최근 {AUDIT_LOOKBACK_DAYS}일)\n"
            "미처리 후보가 발견되지 않았습니다."
        )
        return

    print(f"[놓침 점검] 후보 {len(candidates)}건")
    lines = [
        f"🔍 *놓침 점검* (최근 {AUDIT_LOOKBACK_DAYS}일)",
        f"seen_ids에 없는 검색 결과 {len(candidates)}건입니다.",
        "실제 출연작이 섞여 있으면 필터 개선이 필요합니다.\n",
    ]
    for vid, title, ch in candidates[:15]:
        lines.append(f"• {title[:60]}\n  {ch}\n  https://www.youtube.com/watch?v={vid}")
    if len(candidates) > 15:
        lines.append(f"\n…외 {len(candidates) - 15}건")

    send_telegram(clip("\n".join(lines)))


def normalize_dates(text):
    """LLM이 프롬프트 규칙을 어겨도 날짜 표기를 강제로 통일한다.
    (26-07-31) → `07.31` / (26-07) → `07월` / (26-01~08) → `01~08월`
    올해 날짜는 연도 생략, 작년 이전은 'YY.MM' 유지."""
    if not text:
        return text

    yy = datetime.now(KST).year % 100  # 26

    def _pick(y, m, d=None):
        """연도(2자리 int) 기준으로 표기 결정."""
        if y != yy:                       # 작년 이전 → 연도 유지
            return f"`{y:02d}.{m:02d}`" if d is None else f"`{y:02d}.{m:02d}.{d:02d}`"
        return f"`{m:02d}월`" if d is None else f"`{m:02d}.{d:02d}`"

    def _range(mo):
        y = int(mo.group(1)[-2:])
        m1, m2 = int(mo.group(2)), int(mo.group(3))
        head = "" if y == yy else f"{y:02d}년 "
        return f"`{head}{m1:02d}~{m2:02d}월`"

    out = []
    for line in text.split("\n"):
        # 제목 줄(#)은 '2026년 8월' 같은 표기를 유지해야 하므로 건드리지 않는다
        if line.lstrip().startswith("#"):
            out.append(line)
            continue

        # 1) (26-01~08) → `01~08월`
        line = re.sub(r"\(?\b(\d{2}|\d{4})-(\d{1,2})\s*~\s*(\d{1,2})\)?", _range, line)
        # 2) (26-07-31) → `07.31`
        line = re.sub(
            r"\(?\b(\d{2}|\d{4})-(\d{1,2})-(\d{1,2})\)?",
            lambda mo: _pick(int(mo.group(1)[-2:]), int(mo.group(2)), int(mo.group(3))),
            line,
        )
        # 3) (26-07) → `07월`   ※ 위 두 패턴이 먼저 소비되므로 남은 것만 매칭
        line = re.sub(
            r"\(?\b(\d{2}|\d{4})-(\d{1,2})\)?(?![\d~-])",
            lambda mo: _pick(int(mo.group(1)[-2:]), int(mo.group(2))),
            line,
        )
        # 4) 백틱 중복 제거 (``07.31`` → `07.31`)
        line = re.sub(r"`{2,}([^`]+)`{2,}", r"`\1`", line)
        out.append(line)

    return "\n".join(out)


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
    this_year = datetime.now(KST).year

    prompt = (
        "당신은 이선엽 대표의 시장 관점을 누적 정리하는 애널리스트입니다.\n"
        "아래 [기존 종합]에 [이번 신규 영상들]의 내용을 반영해 종합본을 갱신하세요.\n\n"
        # ── 날짜 표기 규칙 (가장 중요) ──────────────────
        "■ 날짜 표기 규칙 (반드시 지킬 것)\n"
        f"- 올해는 {this_year}년입니다. 올해 날짜는 연도를 쓰지 마세요.\n"
        "- 특정 하루  → `08.01`   (백틱 포함, MM.DD)\n"
        "- 특정 한 달 → `07월`    (백틱 포함, MM월)\n"
        "- 여러 달    → `01~08월` (백틱 포함, MM~MM월)\n"
        f"- 작년 이전만 연도 2자리를 붙입니다 → `{str(this_year - 1)[2:]}.12`\n"
        "- 날짜는 **항목의 맨 앞**에 두세요. 문장 끝 괄호에 넣지 마세요.\n"
        "  올바른 예: - `08.01` 미·중 패권 전쟁의 본질\n"
        "  틀린 예:   - 미·중 패권 전쟁의 본질 (26-08-01)\n"
        "- `26-07-31`, `(26-01~08)`, `2026-08-01` 같은 옛 형식이 [기존 종합]에 남아 있으면 "
        "위 규칙대로 **전부 고쳐서** 다시 쓰세요.\n"
        "- 한 항목에 날짜는 하나만. 제목과 본문에 중복해서 넣지 마세요.\n\n"
        # ── 배지 ──────────────────────────────────────
        "■ 배지\n"
        "- 이번 신규 영상에서 새로 나온 항목은 날짜 앞에 🆕\n"
        "- 3개월 이상 반복해서 언급된 항목은 날짜 앞에 ⭐\n"
        "- 배지는 섹터 항목에만 붙입니다. 둘 다 해당하면 🆕만 씁니다.\n\n"
        # ── 본문 구조 ─────────────────────────────────
        "■ 본문 구조 (아래 순서와 형식을 그대로 지킬 것)\n\n"
        "💡 *한 줄 요약*\n"
        "지금 시장에서 가장 중요한 판단 한 문장. 40자 이내. 맨 위에 둡니다.\n\n"
        "---\n\n"
        "🆕 *이번에 추가된 관점*\n"
        "- [이번 신규 영상들]에서 처음 나온 내용만. 최대 3개.\n"
        "- 형식: `날짜` *제목* → 줄바꿈 → 설명 한 줄(60자 이내)\n"
        "- 신규 내용이 없으면 이 섹션 전체를 생략하세요.\n\n"
        "---\n\n"
        "📊 *시장 관점*\n"
        "- 누적된 시장 판단. **한 항목당 반드시 한 줄.**\n"
        "- 형식: `날짜` *제목* — 요약 (전체 50자 이내, 줄바꿈 없이)\n"
        "- 최신순 정렬. 최대 5개. 넘치면 오래된 것부터 버립니다.\n\n"
        "---\n\n"
        "🎯 *주목 섹터*\n"
        "- 형식: `날짜` *섹터명* — 근거 (전체 50자 이내, 줄바꿈 없이)\n"
        "- 3개월 이상 반복 언급된 섹터는 맨 앞에 ⭐, 이번 신규는 🆕를 붙입니다.\n"
        "- '언급 맥락' 같은 라벨은 쓰지 마세요. 날짜 뒤에 바로 내용을 씁니다.\n"
        "- 이선엽은 개별 종목 추천을 하지 않습니다. 그가 이름을 언급한 종목이 있으면 "
        "'추천'이 아니라 '언급된 맥락' 그대로만 기록하세요. 매수 신호처럼 각색 금지.\n"
        "- 최대 6개.\n\n"
        "■ 그 밖의 규칙\n"
        "- 섹션 사이에는 --- 한 줄만 넣으세요.\n"
        "- ## 같은 마크다운 헤더는 쓰지 마세요. 위에 지정한 *굵게* 표기만 씁니다.\n"
        "- 같은 주제가 반복되면 최신 내용으로 덮어쓰되 날짜 범위를 늘려 흐름을 보존하세요.\n"
        "- 전체 길이는 공백 포함 1800자를 넘기지 마세요. 넘치면 오래된 항목부터 버립니다.\n\n"
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

    # 모델이 규칙을 어겼을 경우를 대비한 강제 정규화
    updated = normalize_dates(updated)

    with open(PERSPECTIVE_FILE, "w", encoding="utf-8") as f:
        f.write(updated)
    print("[관점_종합 갱신 완료]")
    return updated


def main():
    seen = get_seen_ids()
    videos = collect_videos()
    new_count = 0
    new_items = []  # 이번에 처리한 (title, date_str, tags, summary)

    for item in videos:
        vid_id       = item["id"]["videoId"]
        # YouTube API는 제목의 특수문자를 &#39; 같은 HTML 엔티티로 반환할 수 있어 디코딩
        title        = html.unescape(item["snippet"]["title"])
        channel      = item["snippet"]["channelTitle"]
        channel_id   = item["snippet"].get("channelId", "")
        published_at = item["snippet"]["publishedAt"]
        snippet_desc = item["snippet"].get("description", "")
        # 채널 순회로 온 항목은 설명란이 이미 전체라 재조회가 필요 없다
        desc_is_full = item.get("_full_desc", False)

        # 제목뿐 아니라 설명란(출연자 목록 등)까지 검사.
        # 이미 본 영상(seen)은 API 호출 아끼기 위해 필터 전에 먼저 거른다.
        if vid_id in seen:
            print(f"[SKIP] {title}")
            continue

        passed, reason = matches_keyword(title, snippet_desc, vid_id, desc_is_full)
        if not passed:
            print(f"[SKIP-필터:{reason}] {title}")
            # Gemini가 판단한 애매한 건만 기록 (단순 미포함은 양이 많아 제외)
            if reason == "gemini-no":
                log_rejection(reason, title, vid_id, channel)
            continue

        # 10분 미만 영상(숏폼 등)은 요약하지 않고 건너뜀
        dur_sec = get_duration_seconds(vid_id)
        if 0 <= dur_sec < MIN_DURATION_SEC:
            print(f"[SKIP-길이] {title} ({dur_sec}초 < 10분)")
            log_rejection("short", title, vid_id, channel)
            if NOTIFY_SKIPS:
                send_telegram(
                    f"⏭ *짧은 영상 건너뜀* ({dur_sec // 60}분 {dur_sec % 60}초)\n"
                    f"{title}\n{channel}\n"
                    f"https://www.youtube.com/watch?v={vid_id}"
                )
            save_seen_id(vid_id)  # 다음 실행 때 또 안 걸리게 기록
            continue

        date_str     = format_date(published_at)
        duration_str = get_duration(vid_id)

        transcript, fail_reason = get_transcript(vid_id)
        # 자막 없으면 요약/아카이브 안 함 (자막 있는 것만)
        if not transcript:
            print(f"[SKIP-자막없음] {title} ({fail_reason})")
            log_rejection("no-transcript", title, vid_id, channel)
            if NOTIFY_SKIPS:
                send_telegram(
                    f"⏭ *자막 없어 요약 생략*\n{title}\n{channel}\n"
                    f"📅 {date_str}  ⏱ {duration_str}\n"
                    f"https://www.youtube.com/watch?v={vid_id}"
                )
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
            f"{clip(to_telegram(summary))}\n\n"
            f"_※ AI 생성 참고 정보이며 투자 조언이 아닙니다._\n"
            f"https://www.youtube.com/watch?v={vid_id}"
        )
        send_telegram(video_msg)

        # 출연이 확인된 채널은 자동 학습 → 다음부터 검색에 의존하지 않는다
        learn_channel(channel_id, channel)

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
                f"{clip(to_telegram(updated))}\n\n"
                f"_※ AI 생성 참고 정보이며 투자 조언이 아닙니다._"
            )
            send_telegram(persp_msg)
            print("[관점 종합 메시지 발송]")

    if new_count == 0:
        now = datetime.now(KST).strftime("%Y년 %m월 %d일 %H:%M")
        send_telegram(f"✅ 이선엽 대표 새 영상 없음\n({now} 기준)")
        print("완료: 신규 영상 없음 알림 발송")
    else:
        print(f"완료: 신규 {new_count}건 알림 발송 + 아카이브 저장")

    # 주기적으로 '놓친 영상이 있는지' 스스로 점검한다.
    # 알림봇의 가장 큰 위험은 놓치는 것이 아니라 놓친 줄 모르는 것이다.
#    if should_run_audit():
#        print("[놓침 점검] 실행")
#        audit_missed()


if __name__ == "__main__":
    main()
