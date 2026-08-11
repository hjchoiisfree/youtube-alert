"""삼프로TV 오염 아카이브 격리 스크립트 (1회용).

출연검증 fail-open 버그로 이선엽이 출연하지 않은 영상이
이선엽 발언으로 요약되어 아카이브에 저장된 건들을 격리한다.

- 삭제하지 않고 archive/rejected/ 로 이동한다 (되돌릴 수 있게)
- 해당 video ID를 verdicts.json 에 false 로 기록해 재발을 막는다
- seen_ids.txt 는 건드리지 않는다 (지우면 다시 수집·발송된다)
"""
import os, json, shutil

REJECT_DIR = "archive/rejected"
REVIEW_DIR = "archive/review"
VERDICT_FILE = "verdicts.json"

# 제목에 다른 출연자가 명시된 확정 오염 건
CONFIRMED = [
    ("archive/2026-07-08_[7월-8일-마감시황]-공포에-파는-순간-늦습니다...지금은-'기다려야-.md", 'aPab3HBg-vc'),
    ("archive/2026-07-08_반도체-비중-축소!-7월-'이것'-확인하세요--이권희-위즈웨이브-대표-[.md", '2fWUdyV7EFs'),
    ('archive/2026-07-13_[7월-13일-마감시황]-매도가-매도를-부른-투심-붕괴...지금-손절하면.md', 'z-sK4sBd4Ic'),
    ('archive/2026-07-13_중요-가격대마저-무너진-SK하이닉스···이번주-반등이-향후-흐름을-결정한.md', 'wHxWiXJGY2E'),
    ('archive/2026-07-14_ETF-규제가-만든-폭락-지금-꼭-확인해야-할-변수--박병창-MP파트너스.md', 'cgAYeXmICfk'),
    ('archive/2026-07-14_반도체,-어쩌면-기회일까-단기와-장기는-다르다--여도은,-허재무,-박지훈.md', 'aDfx-Tt2NUs'),
    ('archive/2026-07-14_반도체,-지금은-섣부른-판단보다-이번-주를-봐야-합니다--한상희-한화투자.md', 'XfqSYZt2RgM'),
    ('archive/2026-07-15_더-먹으려다-계좌가-무너진다...하반기-삼전닉스-박스권-대비해야-합니다ㅣ.md', 'S_Oait-Qnm4'),
    ("archive/2026-07-16_[7월-16일-마감시황]-추세-전환은-아닙니다,-그렇다면-지금-구간의-'.md", 'iKNo0yTxJBM'),
    ('archive/2026-07-16_노이즈는-이제-다-왔다,-비반도체-회복의-시그널-포착!-하반기에는ㅣ명민준.md', 'HNPFSW8Bago'),
    ("archive/2026-07-16_비정상적-투매-끝났다-'이-조건'-맞춰-투자했다면-무조건-버티세요ㅣ이재규.md", 'fZ5IqDWDD-0'),
    ('archive/2026-07-17_진짜-절박한-상황입니다-대한민국의-황금시대를-놓칠-수-없다--김민석-전-.md', 'q2kSA1fpuc8'),
    ('archive/2026-07-18_미국에서-조용히-통과-중인-역대급-법안…-10년-만의-금융-대격변이-시작.md', 'xF3NtaTR0Hg'),
    ('archive/2026-07-19_“반도체-닷컴버블처럼-끝난다-하락장-공포에-속으면-안-되는-진짜-이유--.md', 'qmRj9vXv0wA'),
    ('archive/2026-07-19_폭등과-폭락-사이,-요동치는-반도체-시장에서-반드시-확인해야-합니다--박.md', 'c3uAzrtUR9o'),
    ('archive/2026-07-20_[7월-20일-마감시황]-4%-하락,-그나마-다행…이성을-잃은-시장,-손.md', '-pt-Fa6rhPI'),
    ("archive/2026-07-20_반도체-진짜-바닥일까-이번-주-핵심은-'반등의-힘'--박병창-MP파트너스.md", 'cgkSZwL7B9c'),
    ('archive/2026-07-20_반도체만-오르던-시장,-결국-균열이-시작됐다ㅣ홍선애,-김한진-삼프로TV-.md', 'NIr8nJs7gEg'),
    ('archive/2026-07-27_반도체-호재-터져도-못-오르는-이유…하반기-주식-판도-싹-바뀝니다ㅣ이재규.md', 'L4pjFmIuuA8'),
    ('archive/2026-07-27_중국-반도체-성장의-역설…삼전닉스-메모리-수요는-더-커진다ㅣ명민준,-박가.md', 'J9rOW46geb4'),
    ("archive/2026-07-28_7월의-시장-하락,-8월엔-나아질까-반도체-하락,-'사이클-정점'-우려해.md", '8FzDGBZUwjk'),
    ('archive/2026-07-28_비이성적인-급락장…대응-전략은--김종문,-이창휘,-여도은,-허재무-[아침.md', '1EXYsVlV1Rc'),
    ("archive/2026-07-28_엔비디아,-네이버-'3대-주주'…순환거래-'불똥'에-시총-2위-추락--중.md", 'DSoSTfu46n0'),
    ('archive/2026-07-29_[7월-29일-마감시황]-실적보다-먼저-반응한-시장…투자자들이-놓친-신호.md', 'tBS413fg7s4'),
    ('archive/2026-07-29_온체인-주식에-SK하이닉스도-입성…한국-주식-토큰화,-글로벌-시장에서-벌.md', 'iGG4upmyLPM'),
    ('archive/2026-07-29_코스피-박스권-붕괴…지금부터-봐야-할-‘저점-확인’-신호--박병창-MP파.md', 'ZqvRMn4cz1A'),
    ('archive/2026-07-31_반가운-시장의-급반등!-반도체-외에-빅테크도-꼭-담아야-하는-이유는--서.md', 'u8rk3a6yuXk'),
    ('archive/2026-07-31_저점은-확인됐다-반등-목표는-6500!--박병창-MP파트너스-대표-[마켓.md', 'SbRuaZT-lNs'),
    ("archive/2026-08-01_'SK하이닉스-상한가-돌파'-가-의미하는-것---빈센트-하나증권-애널리스.md", '2g9ib8BFdOo'),
    ('archive/2026-08-03_8월-전강후강-갭하락-출발이-오히려-대형-호재!-지금-도망치지-마세요ㅣ명.md', 'Nvpu5RUjEWo'),
    ("archive/2026-08-03_반도체-다-팔라는-게-아닙니다-하반기-대형주-'이곳'으로-균형-잡으세요ㅣ.md", 'Pfu8rWMGwoQ'),
    ('archive/2026-08-04_미국증시는-강세장-진행-중...메모리-다음은-비메모리--한상희-한화투자증.md', 'rKt6k8GejzU'),
    ("archive/2026-08-05_8월,-변동성-잦아들까…'AI랠리'-재개는-어떤-조건이-필요한가--장재영.md", 'QGIBeRNPFRI'),
    ("archive/2026-08-05_한국-반도체-기업의-주가-향방은-'이것'에-달렸다.-무엇을-증명해야-할까.md", 'Esdy2Wum9Ts'),
    ("archive/2026-08-06_[8월-6일-마감시황]-'투기판'-된-코스피,-이제-펀더멘털도-안-통한다.md", '3v1Avz9Ijmk'),
    ("archive/2026-08-06_반등-이어질지-결정될-'하락폭'-체크포인트는--박병창-MP파트너스-대표-.md", 'B2J1-4A2l9o'),
    ("archive/2026-08-06_변동성-극심한-시장-속에서는-버티는-'구조'의-포트폴리오를-만들어야--박.md", 'n-bd2z9emo4'),
    ('archive/2026-08-06_본전만-찾고-팔겠다-많이-오른-코스닥조차-여전히-마이너스라면--홍선애,-.md', '8H20N3eXxRg'),
    ('archive/2026-08-07_[8월-7일-마감시황]-약해진-증시-체력…불안-심리가-만든-과도한-비관론.md', 'liU3Jn0wAQM'),
    ('archive/2026-08-08_집값-잡으려다-전월세-폭등-2026-세제개편안-후폭풍--김학렬-소장-[주.md', 'KRdGVQVCbD4'),
    ('archive/2026-08-09_“반도체-끝났다”는-공포,-외국인은-왜-지금-흔들고-있을까ㅣ문홍철-DB증.md', 'OWO0DOhSuxw'),
]

# "방송 전체보기" - 여러 출연자 통합 방송이라 직접 확인이 필요한 건
REVIEW = [
    ('archive/2026-07-13_[26.07.13-오후-방송-전체보기]-SK하이닉스--15%,-역대-최대.md', 'HrYMtRG5Ank'),
    ('archive/2026-07-16_[26.07.16-오후-방송-전체보기]-멈추지-않는-롤러코스피,-단타와-.md', 'SLoZrUChBQQ'),
    ('archive/2026-07-30_[26.07.30-오후-방송-전체보기]-삼성전자-확정-실적에도-반도체는-.md', '18d4SRbPszs'),
    ("archive/2026-08-03_[26.08.03-오후-방송-전체보기]-'폭등-후유증'-겪는-코스피,-'.md", 'ZZ09tf_FNr8'),
]


def load_verdicts():
    if not os.path.exists(VERDICT_FILE):
        return {}
    try:
        with open(VERDICT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def move_batch(items, dest, mark_false):
    """items를 dest로 옮기고, mark_false면 판정 캐시에 false로 기록."""
    os.makedirs(dest, exist_ok=True)
    verdicts = load_verdicts()
    moved, missing = 0, 0

    for path, vid in items:
        if not os.path.exists(path):
            print(f"  [없음] {os.path.basename(path)}")
            missing += 1
            continue
        target = os.path.join(dest, os.path.basename(path))
        shutil.move(path, target)
        print(f"  [이동] {os.path.basename(path)[:60]}")
        moved += 1
        if mark_false and vid:
            verdicts[vid] = False

    if mark_false:
        with open(VERDICT_FILE, "w", encoding="utf-8") as f:
            json.dump(verdicts, f, ensure_ascii=False, indent=1)

    return moved, missing


def main():
    print("=" * 60)
    print(f"확정 오염 {len(CONFIRMED)}건 → {REJECT_DIR}")
    print("=" * 60)
    m1, x1 = move_batch(CONFIRMED, REJECT_DIR, mark_false=True)

    print()
    print("=" * 60)
    print(f"검토 필요 {len(REVIEW)}건 → {REVIEW_DIR}")
    print("(여러 출연자 통합 방송. 직접 확인 후 판단하세요)")
    print("=" * 60)
    # 검토 건은 판정 캐시에 기록하지 않는다.
    # false로 박아두면 나중에 이선엽 출연으로 확인돼도 다시 안 잡힌다.
    m2, x2 = move_batch(REVIEW, REVIEW_DIR, mark_false=False)

    print()
    print("=" * 60)
    print(f"격리 완료: 확정 {m1}건, 검토 {m2}건 (누락 {x1 + x2}건)")
    print(f"판정 캐시 총 {len(load_verdicts())}건")
    print()
    print("남은 아카이브:", len([f for f in os.listdir("archive")
                              if f.endswith(".md")]), "개")
    print()
    print("※ 되돌리려면 archive/rejected/ 안의 파일을 archive/ 로 옮기고")
    print("   verdicts.json 에서 해당 ID를 지우면 됩니다.")


if __name__ == "__main__":
    main()
