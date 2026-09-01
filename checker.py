import os
import re
import json
import html
import time
import traceback
import requests
import xml.etree.ElementTree as ET
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

import usage_tracker

SEEN_FILE = "seen_ids.txt"
ARCHIVE_DIR = "archive"
KEYWORD = "이선엽"

# 표기 흔들림·오타 대응. 실제로 채널 설명란에 '이선혁'으로 잘못 적힌 사례가 있었다.
# 새 오타를 발견하면 여기에 추가한다.
KEYWORD_VARIANTS = ["이선엽", "이선혁", "이 선엽", "이선 엽"]

# 자동 학습된 출연 채널 목록 (git에 커밋되어 실행 간 유지된다)
CHANNELS_FILE = "channels.json"
# 후보 채널: 설명란에 이름이 등장했으나 아직 출연 확정이 안 된 채널.
# RSS(쿼터 0)로만 감시하므로 오탐 비용이 사실상 없다.
# 이것이 없으면 '놓친 채널은 영원히 놓친다'는 자기강화 사각지대가 생긴다.
CANDIDATE_FILE = "candidates.json"
CANDIDATE_PROMOTE_HITS = 2   # 이름이 이만큼 등장하면 정식 채널로 승격
# 필터에서 걸러진 영상 기록 (Gemini 오판 사후 검증용)
REJECTED_LOG = "rejected.log"
# 마지막 놓침 점검 실행일
AUDIT_FILE = "last_audit.txt"
# 출연검증 결과 캐시 (git 커밋되어 실행 간 유지)
# 같은 영상을 매 실행마다 Gemini에 다시 묻는 낭비를 막는다.
# 이것이 없으면 800건 규모에서 rate limit이 반드시 터진다.
VERDICT_FILE = "verdicts.json"
# 미출연 판정의 유효기간. 오판이 영구 박제되는 것을 막는다.
# True 판정은 만료시키지 않는다 (한 번 출연이면 계속 출연이다).
VERDICT_FALSE_TTL_DAYS = 14
# 자막 확보 실패 영상의 재시도 횟수 (git 커밋되어 실행 간 유지)
# 자동 자막은 업로드 직후 안 붙는 경우가 많아, 한 번 실패했다고 버리면 안 된다.
TRANSCRIPT_RETRY_FILE = "transcript_retry.json"
TRANSCRIPT_MAX_RETRIES = 3   # 하루 1회 실행이므로 사실상 3일간 재시도
# 요약 실패(모델 과부하)는 대개 몇 분~몇 시간이면 풀린다.
# 실행이 하루 2~3회이므로 4회면 하루 반 정도 재시도하는 셈이다.
SUMMARY_MAX_RETRIES = 4

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


# 설명란의 광고·상용구 줄을 식별하는 표지들.
# 이런 줄에만 이름이 있으면 출연이 아니라 홍보 문구다.
AD_MARKERS = (
    "http://", "https://", "👉", "문의", "구독", "할인", "이벤트", "신청",
    "쿠폰", "고객센터", "편성표", "앱 설치", "바로가기", "클래스", "서비스",
)


def looks_like_boilerplate(description):
    """키워드가 광고·상용구 줄에만 등장하는지 판단한다.

    삼프로TV는 모든 영상 설명란에
      '🔥박병창&이선엽&김장열&이권희&장우진&박명석을 지금 만나보세요!'
    같은 구독 서비스 광고를 넣는다. 그래서 이선엽이 출연하지 않는 영상도
    전부 Gemini 출연검증으로 넘어가 호출을 낭비하고 429를 유발한다.
    하루 수십 건을 올리는 채널이라 비용이 그대로 누적된다.

    판정은 보수적으로 한다. 키워드가 등장하는 줄 중 하나라도 광고로
    보이지 않으면 False를 돌려 Gemini에게 맡긴다.
    (놓치는 것보다 한 번 더 묻는 쪽이 낫다.)
    """
    lines = [ln.strip() for ln in description.split("\n") if _has_keyword(ln)]
    if not lines:
        return False

    for line in lines:
        has_marker = any(m in line for m in AD_MARKERS)
        # 'A&B&C' 처럼 이름을 나열한 홍보 문구도 상용구로 본다
        is_name_list = line.count("&") >= 2
        if not (has_marker or is_name_list):
            return False   # 광고가 아닌 줄이 있다 → 판정 보류

    return True

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


# 검색 경로: search.list 는 색인 지연이 있어 넓게 잡는다.
# 좁히면 '어제 놓친 영상'이 창 밖으로 밀려 영구 누락된다. seen_ids 로 중복을 거른다.
LOOKBACK_DAYS = 14
# 채널 순회 경로: RSS가 1차라 페이지 비용이 낮아졌으므로 3일 → 7일로 넓힌다.
# 3일은 '금요일 밤에 놓치면 월요일엔 창 밖'이 되는 위험한 폭이었다.
CHANNEL_LOOKBACK_DAYS = 7
# 쿼터 보호용 최대 페이지 수 (1페이지=50개, 검색 1회당 쿼터 100 소모)
# 쿼리 수가 늘었으므로 쿼리당 페이지는 줄인다.
MAX_SEARCH_PAGES = 2

# 검색에 사용할 (쿼리, 정렬) 조합.
# 단일 쿼리로는 YouTube의 관련성 게이트에 걸려 결과가 임의로 잘린다.
# 특히 제목·tags에 이름이 없고 설명란에만 있는 영상이 누락된다.
# (실제 누락 사례: mynVdWBBU38 / 교양이를 부탁해 / 2026-08-19)
SEARCH_QUERIES = [
    ("이선엽",      "date"),
    ("이선엽",      "relevance"),   # date와 다른 결과 집합이 나온다
    ("AFW파트너스", "date"),        # 현 소속. 설명란에 거의 항상 등장한다
]


def search_youtube():
    """여러 쿼리·정렬로 검색해 합집합을 만든다.
    최근 LOOKBACK_DAYS일 이내 영상만, 쿼리당 최대 MAX_SEARCH_PAGES 페이지."""
    url = "https://www.googleapis.com/youtube/v3/search"
    published_after = (
        datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    all_items, seen_vids = [], set()

    for query, order in SEARCH_QUERIES:
        page_token = None
        for page in range(MAX_SEARCH_PAGES):
            params = {
                "part": "snippet",
                "q": query,
                "type": "video",
                "order": order,
                "maxResults": 50,
                "publishedAfter": published_after,
                "key": YOUTUBE_API_KEY,
            }
            if page_token:
                params["pageToken"] = page_token

            try:
                res = requests.get(url, params=params, timeout=30)
                data = res.json()
                # search 는 호출당 100유닛. 예외가 나도 호출은 이미 나갔을 수 있다.
                usage_tracker.youtube("search.list", 100, note=f"{query}:{order}")
            except Exception as e:
                print(f"[검색 오류] {query}/{order} {page+1}p: {type(e).__name__}")
                break

            if "error" in data:
                print(f"[검색 API 오류] {query}/{order}: "
                      f"{data['error'].get('message', '')[:80]}")
                break

            items = data.get("items", [])
            fresh = 0
            for it in items:
                vid = it.get("id", {}).get("videoId")
                if vid and vid not in seen_vids:
                    seen_vids.add(vid)
                    all_items.append(it)
                    fresh += 1
            print(f"[검색:{query}/{order}] {page+1}p {len(items)}건 (신규 {fresh})")

            page_token = data.get("nextPageToken")
            if not page_token or not items:
                break

    print(f"[검색 합계] {len(all_items)}건")
    return all_items


# 이선엽 대표가 자주 출연하는 채널 (채널ID: 표시명) — 수동 시드.
# 여기에 없어도 키워드 검색으로 한 번 잡히면 channels.json에 자동 학습된다.
CHANNELS = {
    "UCLv3v82YNNsa8EsxrcPMjGQ": "SBS 시사교양 라디오 - 시교라",
    "UC6kZpTl39-_SqfBrF1-N2oQ": "연합뉴스경제TV",
    "UCD0k4Kq7SJROxxV-9N5v8IA": "깨비증권 마블TV [KB증권]",
    "UCIUni4ScRp4mqPXsxy62L5w": "언더스탠딩 : 세상의 모든 지식",
    # ── 2026-08-20 추가 ────────────────────────────────────────
    # 보통 제목에 '(ft.이선엽 AFW파트너스 대표)'를 넣지만, 가끔 빼고
    # 설명란의 '컨트리뷰터' 표기로만 남긴다. 그 경우 검색으로 잡히지 않으므로
    # 반드시 채널 순회로 커버해야 한다.
    "UChY8VUjXv0aA7RF9hDQ0ISg": "교양이를 부탁해 (SBS)",
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


def note_candidate_channel(channel_id, channel_name, vid_id):
    """설명란에 이름이 등장한 채널을 '후보'로 기록한다.

    Gemini가 NO를 냈든 판정 불가였든 상관없이 기록하는 것이 핵심이다.
    기존 learn_channel은 '필터를 통과한' 영상에서만 호출되는데,
    그러면 놓친 영상에서는 학습이 일어나지 않는다.
    채널 등록이 놓침을 막는 수단인데 놓치면 등록이 안 되는 순환이 생긴다.

    후보 채널은 RSS(쿼터 0)로만 감시하므로 오탐 비용이 사실상 없다.
    따라서 문턱을 과감히 낮춰도 된다.
    """
    if not channel_id:
        return

    data = {}
    if os.path.exists(CANDIDATE_FILE):
        try:
            with open(CANDIDATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    today = datetime.now(KST).strftime("%Y-%m-%d")
    entry = data.setdefault(
        channel_id, {"name": channel_name, "added": today, "seen": []}
    )
    entry["name"] = channel_name or entry.get("name", channel_id)
    if vid_id and vid_id not in entry["seen"]:
        entry["seen"].append(vid_id)
    entry["last_seen"] = today

    try:
        with open(CANDIDATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[후보채널 저장 오류] {e}")

    if len(entry["seen"]) >= CANDIDATE_PROMOTE_HITS:
        print(f"[채널 승격] {channel_name} (이름 등장 {len(entry['seen'])}회)")
        learn_channel(channel_id, channel_name)


def load_candidate_channels():
    """후보 채널 목록 {id: name}. RSS 전용으로만 순회한다."""
    if not os.path.exists(CANDIDATE_FILE):
        return {}
    try:
        with open(CANDIDATE_FILE, encoding="utf-8") as f:
            return {cid: v.get("name", cid) for cid, v in json.load(f).items()}
    except Exception as e:
        print(f"[후보채널 로드 오류] {e}")
        return {}


RSS_NS = {
    "a":  "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


def fetch_channel_rss(channel_id, channel_name, cutoff_iso):
    """채널 RSS 피드로 최신 15개를 가져온다.

    playlistItems 대비 결정적 장점 두 가지:
      - API 쿼터 0
      - 색인 지연 없음 (발행 즉시 반영)

    search.list는 색인 지연과 관련성 게이트 때문에 신규 영상을 놓치는데,
    RSS는 이 두 문제를 모두 우회한다. 그래서 채널의 1차 경로로 쓴다.

    단점은 최신 15개만 준다는 것과 설명란이 잘려서 온다는 것이다.
    그래서 _full_desc=False로 두어 matches_keyword가 videos.list로
    전체 설명을 다시 가져오게 한다.
    """
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        res = requests.get(url, timeout=20)
        if res.status_code != 200:
            print(f"[RSS] {channel_name}: HTTP {res.status_code}")
            return []
        root = ET.fromstring(res.content)
    except Exception as e:
        print(f"[RSS 오류] {channel_name}: {type(e).__name__}")
        return []

    out = []
    for entry in root.findall("a:entry", RSS_NS):
        vid = entry.findtext("yt:videoId", default="", namespaces=RSS_NS)
        pub = entry.findtext("a:published", default="", namespaces=RSS_NS)
        title = entry.findtext("a:title", default="", namespaces=RSS_NS)
        if not vid or len(pub) < 19:
            continue
        # '2026-08-19T12:05:17+00:00' → '2026-08-19T12:05:17Z'
        pub_iso = pub[:19] + "Z"
        if pub_iso < cutoff_iso:
            continue        # break 아님: RSS도 순서가 뒤집힐 수 있다
        out.append({
            "id": {"videoId": vid},
            "snippet": {
                "title": html.unescape(title or ""),
                "channelTitle": channel_name,
                "channelId": channel_id,
                "publishedAt": pub_iso,
                "description": "",
            },
            "_full_desc": False,
        })

    if out:
        print(f"[RSS] {channel_name}: {len(out)}건")
    return out


def fetch_channel_uploads(channel_id, channel_name, cutoff_iso):
    """채널 업로드 플레이리스트를 최신순으로 읽어 cutoff 이후 영상만 반환.
    업로드 플레이리스트 ID는 채널ID의 'UC'를 'UU'로 바꾼 값이다.
    playlistItems는 호출당 쿼터 1 (search.list는 100)이라 훨씬 저렴하다."""
    playlist_id = "UU" + channel_id[2:]
    url = "https://www.googleapis.com/youtube/v3/playlistItems"
    out, page_token = [], None
    stale_streak = 0        # 연속으로 나온 '기간 밖' 항목 수
    STALE_LIMIT = 25        # 이만큼 연속되면 진짜 기간 밖으로 판단

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
            usage_tracker.youtube("playlistItems.list", 1)
        except Exception as e:
            print(f"[채널 조회 오류] {channel_name}: {e}")
            break

        if "error" in data:
            print(f"[채널 API 오류] {channel_name}: "
                  f"{data['error'].get('message', '')[:80]}")
            break

        for it in data.get("items", []):
            pub = it["contentDetails"].get("videoPublishedAt", "")
            if not pub:
                continue
            if pub < cutoff_iso:
                # 즉시 중단하지 않는다. 업로드 플레이리스트는 '플레이리스트에
                # 추가된 순서'라서 videoPublishedAt 내림차순이 아니다.
                # 라이브·프리미어·예약발행·비공개→공개 전환이 섞이면 순서가
                # 뒤집히는데, 여기서 break하면 그 뒤 항목 전체를 못 본다.
                stale_streak += 1
                continue
            stale_streak = 0
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

        # 기간 밖 항목이 충분히 연속으로 나왔을 때만 진짜 끝으로 본다
        if stale_streak >= STALE_LIMIT or not data.get("nextPageToken"):
            break
        page_token = data["nextPageToken"]

    print(f"[채널] {channel_name}: {len(out)}건")
    return out


def collect_videos():
    """키워드 검색 + 채널 순회를 합치고 videoId 기준으로 중복 제거.

    두 경로는 서로를 보완한다.
    - 검색: 모르는 채널까지 닿지만, 색인 지연과 관련성 점수로 임의로 잘려 신뢰도가 낮다.
    - 채널 순회: 등록된 채널만 보지만 그 채널 업로드는 100% 가져온다. 지연도 없다.
    조회 범위를 다르게 두는 이유가 여기 있다.
    """
    channel_cutoff = (
        datetime.now(timezone.utc) - timedelta(days=CHANNEL_LOOKBACK_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    merged, seen_vids = [], set()

    for item in search_youtube():
        vid = item["id"]["videoId"]
        if vid not in seen_vids:
            seen_vids.add(vid)
            merged.append(item)
    search_count = len(merged)

    def _absorb(items):
        added = 0
        for item in items:
            vid = item["id"]["videoId"]
            if vid not in seen_vids:
                seen_vids.add(vid)
                merged.append(item)
                added += 1
        return added

    channels = load_channels()
    print(f"[채널 목록] 정식 {len(channels)}개")

    # 정식 채널: RSS(빠름·무료)를 먼저, playlistItems로 보완
    for cid, cname in channels.items():
        _absorb(fetch_channel_rss(cid, cname, channel_cutoff))
        _absorb(fetch_channel_uploads(cid, cname, channel_cutoff))
    channel_count = len(merged) - search_count

    # 후보 채널: 아직 출연 확정은 아니지만 이름이 등장한 적 있는 채널.
    # RSS만 쓰므로 쿼터를 전혀 소모하지 않는다.
    candidates = load_candidate_channels()
    for cid, cname in candidates.items():
        if cid in channels:
            continue
        _absorb(fetch_channel_rss(cid, cname, channel_cutoff))
    cand_count = len(merged) - search_count - channel_count
    if candidates:
        print(f"[후보 채널] {len(candidates)}개 → {cand_count}건")

    print(f"[수집 완료] 총 {len(merged)}건 "
          f"(검색 {search_count} + 채널 {channel_count} + 후보 {cand_count})")
    return merged


def get_full_description(vid_id):
    """videos API로 영상의 전체 설명란을 가져온다.
    (search API의 snippet.description은 앞부분만 잘려서 오기 때문에,
    출연자 목록이 설명 뒷부분에 있는 경우를 놓치지 않기 위함)"""
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {"part": "snippet", "id": vid_id, "key": YOUTUBE_API_KEY}
    try:
        res = requests.get(url, params=params, timeout=30)
        usage_tracker.youtube("videos.list", 1, video=vid_id)
        items = res.json().get("items", [])
        if not items:
            return ""
        return items[0]["snippet"].get("description", "")
    except Exception as e:
        print(f"[설명 조회 오류] {vid_id}: {e}")
        return ""


GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)


def call_gemini(prompt, tag="Gemini", timeout=90, retries=3):
    """Gemini 호출 공통 함수. 429는 지수 백오프로 재시도한다.

    반환: (텍스트, None) 성공 / (None, 사유) 실패
    사유가 있다는 것은 '모델이 답을 못 줬다'는 뜻이지
    '아니다'라는 판정이 아니다. 호출자가 이 둘을 구분해야 한다.
    """
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    data = None

    for attempt in range(retries):
        try:
            res = requests.post(GEMINI_URL, json=body, timeout=timeout)
            data = res.json()
        except Exception as e:
            print(f"[{tag} 요청 오류] {type(e).__name__}")
            return None, f"요청 오류: {type(e).__name__}"

        # 재시도마다 토큰이 따로 나가므로 루프 안에서 기록한다.
        usage_tracker.gemini(data, model=GEMINI_MODEL, action=f"call:{tag}")

        err = data.get("error", {})
        code = err.get("code")
        msg = err.get("message", "")

        # 일일 쿼터 소진은 기다려도 소용없으므로 즉시 포기
        if code == 429 and "per day" not in msg.lower():
            wait = 20 * (attempt + 1)
            print(f"[{tag} 429] rate limit, {wait}초 대기 후 재시도 "
                  f"({attempt + 1}/{retries})")
            time.sleep(wait)
            continue
        break

    if data and "error" in data:
        msg = data["error"].get("message", "알 수 없는 오류")
        print(f"[{tag} API 오류] {msg[:80]}")
        return None, msg[:80]

    try:
        return data["candidates"][0]["content"]["parts"][0]["text"], None
    except Exception as e:
        # candidates 없음 = 429 잔여 응답이거나 safety block.
        # 여기서 통과 처리하면 필터가 통째로 무력화된다.
        print(f"[{tag} 파싱 실패] {type(e).__name__}: {e}")
        return None, f"파싱 실패: {e}"


def load_verdicts():
    """확정된 출연검증 결과를 불러온다. {video_id: true/false}

    False 판정은 VERDICT_FALSE_TTL_DAYS가 지나면 없는 것으로 취급한다.
    모델이 한 번 오판하면 그 영상이 영구히 복구되지 않는 문제를 막기 위함이다.
    True는 만료시키지 않는다 (한 번 출연이면 계속 출연이다).

    저장 형식: {"v": bool, "at": "YYYY-MM-DD"}
    구 형식(bool)도 그대로 읽는다.
    """
    if not os.path.exists(VERDICT_FILE):
        return {}
    try:
        with open(VERDICT_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"[검증캐시 로드 오류] {e}")
        return {}

    today = datetime.now(KST).date()
    out = {}
    for vid, val in raw.items():
        if isinstance(val, bool):          # 구 형식 호환
            out[vid] = val
            continue
        if not isinstance(val, dict):
            continue
        v, at = val.get("v"), val.get("at", "")
        if v is False and at:
            try:
                age = (today - datetime.strptime(at, "%Y-%m-%d").date()).days
                if age >= VERDICT_FALSE_TTL_DAYS:
                    continue               # 만료 → 재검토 대상
            except Exception:
                pass
        out[vid] = v
    return out


def save_verdict(vid_id, verdict):
    """확정 판정만 저장한다. 보류(None)는 저장하지 않는다.
    보류를 저장하면 다음 실행 때 재시도할 기회를 잃는다."""
    if verdict is None or not vid_id:
        return

    raw = {}
    if os.path.exists(VERDICT_FILE):
        try:
            with open(VERDICT_FILE, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = {}

    raw[vid_id] = {"v": bool(verdict),
                   "at": datetime.now(KST).strftime("%Y-%m-%d")}
    try:
        with open(VERDICT_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"[검증캐시 저장 오류] {e}")


def verify_appearance_with_gemini(title, description):
    """설명란에서만 '이선엽'이 발견된 애매한 경우, 실제 출연 영상인지 판별.
    설명란의 추천 영상 링크·해시태그에만 이름이 있는 가짜 매칭을 걸러낸다.

    반환: True(출연) / False(미출연) / None(판정 불가 → 보류)

    None을 True로 바꾸지 말 것. 예전에는 오류 시 True를 반환했는데,
    rate limit이 걸리면 모든 영상이 통과해 필터가 무력화됐다.
    보류로 두면 이번 실행에서 발송하지 않고 다음 실행에서 다시 시도한다.
    """
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
    text, err = call_gemini(prompt, tag="출연검증", timeout=60)

    if err or not text:
        print(f"[출연검증 보류] {title[:40]} ({err})")
        return None

    answer = text.strip().upper()
    if answer.startswith("YES"):
        print(f"[출연검증] {title[:40]} → YES")
        return True
    if answer.startswith("NO"):
        print(f"[출연검증] {title[:40]} → NO")
        return False

    # YES/NO 어느 쪽도 아닌 응답은 판정으로 인정하지 않는다
    print(f"[출연검증 보류] {title[:40]} (형식 이탈: {answer[:20]})")
    return None


def matches_keyword(title, snippet_desc, vid_id, desc_is_full=False,
                    channel_id="", channel_name=""):
    """제목 → 설명란 순으로 키워드를 검사한다.
    반환: (통과 여부, 사유코드)
      title          - 제목에서 발견 (확실)
      gemini-ok      - 설명란에서 발견 + Gemini가 출연 인정
      gemini-no      - 설명란에서 발견했으나 Gemini가 미출연 판정
      gemini-cached  - 캐시된 미출연 판정 (API 호출 없음)
      gemini-pending - 판정 불가. 이번엔 보류하고 다음 실행에서 재시도
      no-keyword     - 제목·설명란 어디에도 없음
    """
    if _has_keyword(title):
        return True, "title"

    # 이미 판정이 끝난 영상은 다시 묻지 않는다 (429 예방의 핵심)
    cached = load_verdicts().get(vid_id)
    if cached is True:
        return True, "gemini-cached-ok"
    if cached is False:
        return False, "gemini-cached"

    if desc_is_full:
        # 채널 순회 결과 → 이미 전체 설명란을 갖고 있어 추가 조회 불필요
        description = snippet_desc
    else:
        # 전체 설명란 확보 (search API의 snippet.description은 잘려서 와서
        # 링크 목록인지 출연자 소개인지 문맥 판단이 어려움 → 전체를 가져온다)
        description = get_full_description(vid_id) or snippet_desc

    if not _has_keyword(description):
        return False, "no-keyword"

    # 광고 상용구에만 이름이 있으면 Gemini를 부르지 않는다.
    # 이 채널들은 매일 수십 건을 올리므로 여기서 막지 않으면
    # 출연검증 호출이 폭증해 rate limit이 걸린다.
    if looks_like_boilerplate(description):
        save_verdict(vid_id, False)   # 캐시해서 다음 실행의 설명 조회까지 아낀다
        return False, "boilerplate"

    # 판정 '이전에' 채널을 후보로 기록한다.
    # Gemini가 NO를 내도, 판정에 실패해도 채널은 남긴다.
    # 여기서 기록하지 않으면 놓친 채널을 알게 될 방법이 없다.
    note_candidate_channel(channel_id, channel_name, vid_id)

    # 설명란에만 이름이 있는 애매한 경우 → 출연 여부 검증
    verdict = verify_appearance_with_gemini(title, description)

    if verdict is None:
        # 판정 불가: 통과도 탈락도 아님. seen에 기록하지 않고 다음 실행에서 재시도.
        return False, "gemini-pending"

    save_verdict(vid_id, verdict)
    return (True, "gemini-ok") if verdict else (False, "gemini-no")


def bump_transcript_retry(vid_id, kind="transcript"):
    """실패 횟수를 1 올리고 현재 횟수를 반환한다.

    kind로 실패 종류를 구분한다(transcript / summary).
    요약 실패도 같은 원장을 쓰되 키를 분리해, 자막은 됐는데 요약만
    실패한 경우가 자막 재시도 횟수를 갉아먹지 않게 한다.
    """
    key = vid_id if kind == "transcript" else f"{kind}:{vid_id}"
    data = {}
    if os.path.exists(TRANSCRIPT_RETRY_FILE):
        try:
            with open(TRANSCRIPT_RETRY_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    data[key] = int(data.get(key, 0)) + 1
    try:
        with open(TRANSCRIPT_RETRY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"[재시도 저장 오류] {e}")
    return data[key]


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


def get_duration_info(vid_id):
    """영상 길이를 (초, 표시문자열)로 한 번에 반환. 실패 시 (-1, '알 수 없음').

    예전에는 get_duration과 get_duration_seconds가 같은 영상에 대해
    videos.list를 각각 호출해 쿼터를 두 배로 썼다. 하나로 합쳤다.
    timeout과 try/except가 없어 여기서 터지면 실행 전체가 죽던 문제도 함께 고친다.
    """
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {"part": "contentDetails", "id": vid_id, "key": YOUTUBE_API_KEY}
    try:
        res = requests.get(url, params=params, timeout=30)
        usage_tracker.youtube("videos.list", 1, video=vid_id)
        items = res.json().get("items", [])
    except Exception as e:
        print(f"[길이 조회 오류] {vid_id}: {type(e).__name__}")
        return -1, "알 수 없음"

    if not items:
        return -1, "알 수 없음"

    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
                     items[0]["contentDetails"].get("duration", ""))
    if not match:
        return -1, "알 수 없음"

    hours   = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    total = hours * 3600 + minutes * 60 + seconds

    if hours > 0:
        return total, f"{hours}시간 {minutes:02d}분 {seconds:02d}초"
    if minutes > 0:
        return total, f"{minutes}분 {seconds:02d}초"
    return total, f"{seconds}초"


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

    # 206(자막 없음)도 1크레딧이 나가므로 상태코드와 무관하게 기록한다.
    usage_tracker.supadata(res, video=vid_id, mode="auto")

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
                usage_tracker.supadata(pr, video=vid_id, mode="auto:job")
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
        "다음 형식으로 한국어로 작성하세요. 중요한 부분은 **볼드**로 강조하고, 글머리 기호는 '•'를 사용하세요.\n\n"
        "**📄 3줄 요약**\n"
        "• 영상 핵심 내용을 3줄로\n\n"
        "**🧭 이선엽의 관점·논리**\n"
        "• 그가 시장을 어떤 프레임으로 보는지, 어떤 근거로 그렇게 판단하는지 2~3개\n\n"
        "**🎙 진행자 Q & 이선엽 A**\n"
        "• 이선엽 대표는 보통 게스트로 출연해 진행자의 질문에 답하지만, 진행자 없이 혼자 말하는 영상(숏폼 등)도 있습니다.\n"
        "• **중요: 자막에 실제로 진행자의 질문이 있을 때만** 'Q. 질문 / A. 답변' 형태로 정리하세요. 질문이 없는데 있는 것처럼 지어내지 마세요.\n"
        "• 자막에 진행자 질문이 전혀 없으면 이 섹션에는 '진행자 질문 없음 (단독 발언 영상)'이라고만 쓰세요.\n"
        "• 답변은 핵심만 요약하되, 중요한 문장은 **볼드** 처리하세요.\n\n"
        "**🎯 이선엽이 주목한 섹터·테마**\n"
        "• 이선엽 대표는 보통 개별 종목 추천은 하지 않습니다. 그가 실제로 긍정적으로 언급하거나 방향성을 제시한 섹터/테마만 뽑으세요.\n"
        "• 각 항목에 그렇게 본 근거(실제 발언 내용)를 짧게 붙이세요.\n"
        "• 만약 특정 종목명을 언급했다면 그가 말한 맥락 그대로만 적고, '매수하라'는 식으로 각색하지 마세요.\n"
        "• 자막에 섹터 언급이 전혀 없으면 이 항목은 '언급 없음'이라고만 쓰세요.\n\n"
        "**⚠️ 주의해야 할 리스크**\n"
        "• 그가 경계한 위험 요소 1~2개"
    )

    text, err = call_gemini(prompt, tag="요약", timeout=90)
    if err or not text:
        # 실패를 문자열로 돌려주면 호출부가 성공으로 착각한다.
        # 예전에는 '요약 실패 (...)' 문자열이 그대로 아카이브에 저장되고
        # seen_ids에까지 들어가, 모델이 잠깐 붐볐다는 이유로 요약을 영구히 잃었다.
        print(f"[요약 실패] {err}")
        return None
    return text


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
        lines.append("
