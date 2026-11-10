"""샘플 PDF 몇 쪽으로 analyze_page + flag_elements를 돌려 보는 실험 스크립트.

사용 예 (BE 저장소 루트에서):
    # 1) 프롬프트 다듬기: Lite로 여러 번
    python scripts/try_pages.py sample.pdf --pages 1-5

    # 2) 프롬프트가 정해지면 3.8 Flash로 한 번 (무료 하루 20회라 아껴 쓸 것)
    python scripts/try_pages.py sample.pdf --pages 1-5 --models gemini-3.5-flash-lite,gemini-3.8-flash

    # 키 없이 흐름만 확인
    AI_MODE=mock python scripts/try_pages.py sample.pdf --pages 1-5

    # 교차 검토(실험): 설명을 만든 뒤 이미지와 다시 대조. 쪽마다 호출 1번 더 든다
    python scripts/try_pages.py sample.pdf --pages 2,3,9-13,15,22,26 --review

결과: results/<시각>/
    page_N.png              페이지 이미지 (150dpi, F2와 같은 조건)
    <모델>/page_N.json      flag_elements까지 거친 결과
    <모델>/page_N.review.json  교차 검토 지적 (--review일 때)
    report.md               페이지별 원문·모델별 설명·라벨 대조표·소요 시간

완료 기준(명세 8장) 확인법: report.md 맨 위 요약에서 '완료/요청 쪽'이 다 찼는지('형식 최종 실패 0'),
라벨 대조표의 O/X가 이미지를 보고 손으로 대조한 결과와 맞는지 본다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:  # .env가 있으면 읽는다 (python-dotenv가 없으면 건너뜀)
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

import pymupdf  # noqa: E402

from app.ai import AIQuotaExhausted, analyze_page_with_stats, flag_elements  # noqa: E402
from app.ai.analyze import DEFAULT_MODEL  # noqa: E402
from app.ai.flags import check_labels  # noqa: E402
from app.ai.prompt import PROMPT_VERSION, REVIEW_PROMPT_VERSION  # noqa: E402
from app.ai.review import review_page_with_stats  # noqa: E402


def parse_pages(spec: str, total: int) -> list[int]:
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            pages.extend(range(int(a), int(b) + 1))
        elif part:
            pages.append(int(part))
    return [p for p in pages if 1 <= p <= total]


def render(doc, page_no: int, dpi: int) -> tuple[bytes, str]:
    page = doc[page_no - 1]
    png = page.get_pixmap(dpi=dpi).tobytes("png")
    return png, page.get_text()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--pages", default="1-5")
    ap.add_argument("--models", default=os.getenv("AI_MODEL", DEFAULT_MODEL),
                    help="쉼표로 여러 개. 예: gemini-3.5-flash-lite,gemini-3.8-flash")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--out", default=str(ROOT / "results"))
    ap.add_argument("--review", action="store_true", help="교차 검토(실험)도 돌린다")
    ap.add_argument("--review-model", default=None,
                    help="교차 검토 모델. 비우면 AI_REVIEW_MODEL, 그것도 없으면 분석 모델과 같음")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="  ↳ %(message)s")
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    mode = os.getenv("AI_MODE", "live").lower()
    if mode != "mock" and not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        print("GEMINI_API_KEY가 없습니다. .env에 넣거나 AI_MODE=mock으로 실행하세요.", file=sys.stderr)
        return 1

    doc = pymupdf.open(args.pdf)
    pages = parse_pages(args.pages, doc.page_count)
    out = Path(args.out) / dt.datetime.now().strftime("%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)

    texts: dict[int, str] = {}
    results: dict[str, dict[int, dict]] = {m: {} for m in models}
    stats: dict[str, dict[int, object]] = {m: {} for m in models}
    stopped: dict[str, str] = {}
    reviews: dict[str, dict[int, dict | None]] = {m: {} for m in models}
    rstats: dict[str, dict[int, object]] = {m: {} for m in models}
    review_stopped: dict[str, str] = {}

    for p in pages:
        png, text = render(doc, p, args.dpi)
        texts[p] = text
        (out / f"page_{p}.png").write_bytes(png)
        for m in models:
            if m in stopped:
                continue
            print(f"[{m}] p{p} 분석 중... ({time.strftime('%H:%M:%S')} 시작)", flush=True)
            try:
                raw, st = analyze_page_with_stats(png, p, model=m)
            except AIQuotaExhausted as e:
                stopped[m] = str(e)
                print(f"[{m}] {e} — 이 모델은 여기서 멈춤", file=sys.stderr)
                continue
            except Exception as e:  # 모델 ID 오류(404), 키 오류 등. 다른 모델은 계속 돌린다
                stopped[m] = f"{type(e).__name__}: {str(e)[:150]}"
                print(f"[{m}] 오류로 멈춤 — {stopped[m]}", file=sys.stderr)
                continue
            flagged = flag_elements(raw, text)
            results[m][p] = flagged
            stats[m][p] = st
            (out / m).mkdir(exist_ok=True)
            (out / m / f"page_{p}.json").write_text(
                json.dumps(flagged, ensure_ascii=False, indent=2), encoding="utf-8")
            n = len(flagged["elements"])
            nf = sum(e["flagged"] for e in flagged["elements"])
            print(f"[{m}] p{p}: 그림 {n}개, 검수 필요 {nf}개, {st.seconds:.1f}초, 호출 {st.calls}회"
                  + (", 형식 오류 2회 → 최종 실패" if st.format_failures >= 2
                     else ", 형식 오류 1회 → 복구" if st.format_failures else "")
                  + (f", 토큰 입력 {st.prompt_tokens}·출력 {st.output_tokens}·생각 {st.thought_tokens}"
                     if st.prompt_tokens else ""))

            if args.review and m not in review_stopped:
                try:
                    rv, rst = review_page_with_stats(png, flagged, model=args.review_model or m)
                except AIQuotaExhausted as e:
                    review_stopped[m] = str(e)
                    print(f"[{m}] 교차 검토 {e} — 검토는 여기서 멈춤", file=sys.stderr)
                    continue
                except Exception as e:
                    review_stopped[m] = f"{type(e).__name__}: {str(e)[:150]}"
                    print(f"[{m}] 교차 검토 오류로 멈춤 — {review_stopped[m]}", file=sys.stderr)
                    continue
                reviews[m][p] = rv
                rstats[m][p] = rst
                (out / m / f"page_{p}.review.json").write_text(
                    json.dumps(rv, ensure_ascii=False, indent=2), encoding="utf-8")
                if rv is None:
                    print(f"[{m}] p{p} 교차 검토 실패(형식 오류 2회)")
                elif rst.calls:
                    hit = sum(bool(v) for v in rv.values())
                    print(f"[{m}] p{p} 교차 검토: 지적 받은 그림 {hit}개, {rst.seconds:.1f}초, 호출 {rst.calls}회")

    review = {"on": args.review, "model": args.review_model, "reviews": reviews, "stats": rstats,
              "stopped": review_stopped}
    write_report(out, args.pdf, pages, models, texts, results, stats, stopped, mode, review)
    print(f"\n리포트: {out / 'report.md'}")
    return 0


def write_report(out, pdf, pages, models, texts, results, stats, stopped, mode, review=None) -> None:
    review = review or {"on": False}
    L: list[str] = []
    L.append(f"# analyze_page 실험 — {Path(pdf).name}")
    L.append("")
    res = os.getenv("AI_MEDIA_RESOLUTION") or "기본"
    think = os.getenv("AI_THINKING") or "기본"
    L.append(f"- 실행: {dt.datetime.now():%Y-%m-%d %H:%M} / 프롬프트 {PROMPT_VERSION} / AI_MODE={mode} "
             f"/ 이미지 해상도 {res} / 생각 수준 {think}"
             + (f" / 교차 검토 {REVIEW_PROMPT_VERSION}" if review["on"] else ""))
    if mode == "mock":
        L.append("- **모킹 결과입니다. 실제 AI 출력이 아닙니다.**")
    L.append("")
    L.append("| 모델 | 완료/요청 쪽 | 그림 | 검수 필요 | 형식 오류 후 복구 | 형식 최종 실패 | 평균 초/쪽 | 호출 수 "
             "| 재시도(429/5xx·네트워크) | 입력 토큰/쪽 | 출력+생각 토큰/쪽 | 비고 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for m in models:
        st = list(stats[m].values())
        # 최종 실패 쪽의 '분석 실패' 그림은 실제 그림이 아니므로 그림·검수 필요 수에서 뺀다
        els = [e for p, r in results[m].items() if stats[m][p].format_failures < 2 for e in r["elements"]]
        recovered = sum(1 for s in st if s.format_failures == 1)
        failed_pages = sum(1 for s in st if s.format_failures >= 2)
        done = len(st) - failed_pages
        avg = sum(s.seconds for s in st) / len(st) if st else 0
        calls = sum(s.calls for s in st)
        note = stopped.get(m, "")
        n = len(st) or 1
        inp = sum(s.prompt_tokens for s in st) / n
        outp = sum(s.output_tokens + s.thought_tokens for s in st) / n
        waits = sum(s.rate_waits for s in st)
        trans = sum(s.transient_errors for s in st)
        L.append(f"| {m} | {done}/{len(pages)} | {len(els)} | {sum(e['flagged'] for e in els)} | "
                 f"{recovered} | {failed_pages} | {avg:.1f} | {calls} | {waits}/{trans} | {inp:.0f} | {outp:.0f} | {note} |")
    L.append("")
    L.append("- 완료 = 설명까지 받은 쪽. 형식 최종 실패 쪽과, 한도·오류로 멈춰 실행 안 된 쪽은 빠진다.")
    L.append("- 그림·검수 필요 수에는 최종 실패 쪽의 '분석 실패' 그림을 넣지 않는다.")
    L.append("- 평균 초/쪽에는 호출 간격 대기가 들어 있다. 시연 시간 계산은 이 값 × 페이지 수로 한다.")
    L.append("")
    if review["on"]:
        _review_summary(L, models, results, stats, review)
    no_text = [p for p in pages if not texts[p].strip()]
    if no_text:
        L.append(f"> **텍스트 레이어가 없는 쪽: {', '.join(map(str, no_text))}** — 이 쪽의 그림은 전부 R3로 걸리고 "
                 "R2 대조는 하지 않는다. 글자가 선택되는 PDF로 다시 내보내야 R2를 시험할 수 있다.")
        L.append("")

    for p in pages:
        L.append(f"## {p}쪽")
        L.append("")
        L.append(f"![{p}쪽](page_{p}.png)")
        L.append("")
        txt = texts[p].strip()
        L.append("<details><summary>PDF 텍스트 레이어 (한글이 깨져 있으면 R2가 전부 걸림)</summary>")
        L.append("")
        L.append("```")
        L.append(txt[:1500] if txt else "(비어 있음 → R3)")
        L.append("```")
        L.append("</details>")
        L.append("")
        for m in models:
            r = results[m].get(p)
            L.append(f"### {m}")
            L.append("")
            if r is None:
                L.append("(실행 안 됨)")
                L.append("")
                continue
            ff = stats[m][p].format_failures
            if ff >= 2:
                L.append("> 형식 오류 2회 → **최종 실패** ('분석 실패' 그림으로 채움)")
                L.append("")
            elif ff == 1:
                L.append("> 형식 오류 1회 → 다시 시도해서 복구")
                L.append("")
            if not r["elements"]:
                L.append("그림 없음으로 판단 (elements 빈 배열)")
                L.append("")
            for e in r["elements"]:
                flag = "⚠ 검수 필요" if e["flagged"] else "자동 통과"
                L.append(f"**{e['id']} · {e['type']} · {e['title']}** — {flag}")
                L.append("")
                L.append(f"- alt: {e['alt']}")
                L.append(f"- 설명: {e['description']}")
                if e.get("table"):
                    t = e["table"]
                    L.append(f"- 표: 머리글 {t['headers']} / {len(t['rows'])}행")
                    for row in t["rows"]:
                        L.append(f"  - {row}")
                if e["labels"] and any(x.startswith("R3") for x in e["flag_reasons"]):
                    L.append("- 라벨 대조: 원문 글자가 없어 대조하지 않음(R3)")
                elif e["labels"]:
                    checks = check_labels(e["labels"], texts[p])
                    marks = ", ".join(f"{lab} {'O' if ok else 'X'}" for lab, ok in checks)
                    L.append(f"- 라벨 대조(O=원문에 있음): {marks}")
                for reason in e["flag_reasons"]:
                    L.append(f"- 사유: {reason}")
                if review["on"]:
                    L.extend(_review_lines(review, m, p, e["id"]))
                L.append("")
    if review["on"]:
        _eval_table(L, models, pages, results, stats, review)
    (out / "report.md").write_text("\n".join(L), encoding="utf-8")


def _review_lines(review, m, p, eid) -> list[str]:
    if p not in review["reviews"][m]:
        return ["- 교차 검토: (실행 안 됨)"]
    rv = review["reviews"][m][p]
    if rv is None:
        return ["- 교차 검토: 실패(형식 오류 2회)"]
    issues = rv.get(eid, [])
    if not issues:
        return ["- 교차 검토: 지적 없음"]
    return [f"- 교차 검토 지적 {len(issues)}건:"] + [f"  - {x}" for x in issues]


def _real_elements(results, stats, m):
    """(쪽, 그림) 목록. 형식 최종 실패 쪽의 '분석 실패' 그림은 뺀다."""
    return [(p, e) for p, r in results[m].items() if stats[m][p].format_failures < 2 for e in r["elements"]]


def _review_summary(L, models, results, stats, review) -> None:
    L.append("### 교차 검토 (실험)")
    L.append("")
    L.append("| 모델 | 검토 모델 | 검토한 쪽 | 검토 실패 쪽 | 지적 받은 그림 | 그중 자동 통과였던 그림 | 검토 호출 | 검토 평균 초/쪽 | 비고 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for m in models:
        rv = review["reviews"][m]
        rst = [s for s in review["stats"][m].values() if s.calls]
        hit = auto_hit = 0
        for p, e in _real_elements(results, stats, m):
            issues = (rv.get(p) or {}).get(e["id"], [])
            if issues:
                hit += 1
                auto_hit += not e["flagged"]
        failed = sum(1 for v in rv.values() if v is None)
        rmodel = (rst[0].model if rst else (review["model"] or "-"))
        avg = sum(s.seconds for s in rst) / len(rst) if rst else 0
        L.append(f"| {m} | {rmodel} | {len(rst)} | {failed} | {hit} | {auto_hit} | "
                 f"{sum(s.calls for s in rst)} | {avg:.1f} | {review['stopped'].get(m, '')} |")
    L.append("")
    L.append("- 그림이 없는 쪽은 검토를 부르지 않는다(검토한 쪽에 안 셈).")
    L.append("- 효과 판단은 맨 아래 평가표의 '원본 대조' 칸을 채워서 한다. 지적이 맞았는지는 사람이 원본과 대조해야 안다.")
    L.append("")


def _eval_table(L, models, pages, results, stats, review) -> None:
    L.append("## 평가표 (원본 대조 칸은 손으로 채운다)")
    L.append("")
    L.append("원본 대조: O = 설명이 맞음 / X = 설명에 오류·중요한 누락 있음. "
             "지적 판정: 맞는 지적 / 틀린 지적(과잉) / 놓침(X인데 지적 없음).")
    L.append("")
    for m in models:
        L.append(f"### {m}")
        L.append("")
        L.append("| 쪽 | 그림 | 판정 | 교차 검토 | 원본 대조 | 지적 판정 |")
        L.append("|---|---|---|---|---|---|")
        for p, e in sorted(_real_elements(results, stats, m), key=lambda x: x[0]):
            rv = review["reviews"][m].get(p, "x")
            if rv == "x":
                cell = "실행 안 됨"
            elif rv is None:
                cell = "실패"
            else:
                n = len(rv.get(e["id"], []))
                cell = f"지적 {n}건" if n else "지적 없음"
            judge = "검수 필요" if e["flagged"] else "자동 통과"
            L.append(f"| {p} | {e['id']} {e['title']} | {judge} | {cell} |  |  |")
        L.append("")


if __name__ == "__main__":
    raise SystemExit(main())
