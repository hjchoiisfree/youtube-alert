"""archive/review/ 4건 최종 처리 (1회용).

"방송 전체보기" 4건을 직접 확인한 결과 이선엽 미출연으로 판정됨.
review/ 에서 rejected/ 로 옮기고 verdicts.json 에 false 로 기록한다.

- 삭제하지 않고 이동한다 (되돌릴 수 있게)
- seen_ids.txt 는 건드리지 않는다 (지우면 다시 수집·발송된다)
"""
import os
import json
import shutil

REVIEW_DIR = "archive/review"
REJECT_DIR = "archive/rejected"
VERDICT_FILE = "verdicts.json"

# (파일명, video ID) — 사용자가 영상을 직접 확인해 미출연으로 판정
CONFIRMED_NO = [
    ("2026-07-13_[26.07.13-오후-방송-전체보기]-SK하이닉스--15%,-역대-최대.md",
     "HrYMtRG5Ank"),
    ("2026-07-16_[26.07.16-오후-방송-전체보기]-멈추지-않는-롤러코스피,-단타와-.md",
     "SLoZrUChBQQ"),
    ("2026-07-30_[26.07.30-오후-방송-전체보기]-삼성전자-확정-실적에도-반도체는-.md",
     "18d4SRbPszs"),
    ("2026-08-03_[26.08.03-오후-방송-전체보기]-'폭등-후유증'-겪는-코스피,-'.md",
     "ZZ09tf_FNr8"),
]


def load_verdicts():
    if not os.path.exists(VERDICT_FILE):
        return {}
    try:
        with open(VERDICT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    os.makedirs(REJECT_DIR, exist_ok=True)
    verdicts = load_verdicts()
    before = len(verdicts)

    moved, missing = 0, []

    # 파일명이 정확히 일치하지 않을 수 있으므로,
    # 실제 폴더 내용을 기준으로 대조한다.
    actual = set(os.listdir(REVIEW_DIR)) if os.path.isdir(REVIEW_DIR) else set()

    for fname, vid in CONFIRMED_NO:
        src = os.path.join(REVIEW_DIR, fname)

        if not os.path.exists(src):
            # 파일명이 살짝 다른 경우 날짜 접두어로 찾아본다
            prefix = fname[:10]
            cand = [a for a in actual if a.startswith(prefix)]
            if len(cand) == 1:
                src = os.path.join(REVIEW_DIR, cand[0])
                fname = cand[0]
            else:
                missing.append(fname)
                print(f"  [없음] {fname[:55]}")
                continue

        shutil.move(src, os.path.join(REJECT_DIR, fname))
        verdicts[vid] = False
        moved += 1
        print(f"  [이동] {fname[:60]}")

    with open(VERDICT_FILE, "w", encoding="utf-8") as f:
        json.dump(verdicts, f, ensure_ascii=False, indent=1)

    print()
    print("=" * 58)
    print(f"이동 {moved}건 / 누락 {len(missing)}건")
    print(f"판정 캐시 {before} → {len(verdicts)}건")

    left = ([x for x in os.listdir(REVIEW_DIR) if x.endswith(".md")]
            if os.path.isdir(REVIEW_DIR) else [])
    print(f"review/ 잔여 {len(left)}개")
    for x in left:
        print(f"  - {x[:60]}")

    n_arch = len([f for f in os.listdir("archive") if f.endswith(".md")])
    n_rej = len([f for f in os.listdir(REJECT_DIR) if f.endswith(".md")])
    print(f"archive/ {n_arch}개, rejected/ {n_rej}개")


if __name__ == "__main__":
    main()
