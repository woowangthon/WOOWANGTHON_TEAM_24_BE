"""샘플 PDF 몇 쪽으로 analyze_page + flag_elements를 돌려 보는 실험 스크립트.

사용 예 (BE 저장소 루트에서):
    # 1) 프롬프트 다듬기: Lite로 여러 번
    python scripts/try_pages.py sample.pdf --pages 1-5

    # 2) 프롬프트가 정해지면 3.8 Flash로 한 번 (무료 하루 20회라 아껴 쓸 것)
    python scripts/try_pages.py sample.pdf --pages 1-5 --models gemini-3.5-flash-lite,gemini-3.8-flash

    # 키 없이 흐름만 확인
    AI_MODE=mock python scripts/try_pages.py sample.pdf --pages 1-5

결과: results/<시각>/
    page_N.png              페이지 이미지 (150dpi, F2와 같은 조건)
    <모델>/page_N.json      flag_elements까지 거친 결과
    report.md               페이지별 원문·모델별 설명·라벨 대조표·소요 시간

완료 기준(명세 8장) 확인법: report.md 맨 위 요약에서 '형식 실패 0'인지,
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
from app.ai.prompt import PROMPT_VERSION  # noqa: E402


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
                  + (f", 형식 실패 {st.format_failures}" if st.format_failures else ""))

    write_report(out, args.pdf, pages, models, texts, results, stats, stopped, mode)
    print(f"\n리포트: {out / 'report.md'}")
    return 0


def write_report(out, pdf, pages, models, texts, results, stats, stopped, mode) -> None:
    L: list[str] = []
    L.append(f"# analyze_page 실험 — {Path(pdf).name}")
    L.append("")
    L.append(f"- 실행: {dt.datetime.now():%Y-%m-%d %H:%M} / 프롬프트 {PROMPT_VERSION} / AI_MODE={mode}")
    if mode == "mock":
        L.append("- **모킹 결과입니다. 실제 AI 출력이 아닙니다.**")
    L.append("")
    L.append("| 모델 | 페이지 | 그림 | 검수 필요 | 형식 실패 페이지 | 평균 초/쪽 | 호출 수 | 비고 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for m in models:
        st = list(stats[m].values())
        els = [e for r in results[m].values() for e in r["elements"]]
        failed_pages = sum(1 for s in st if s.format_failures >= 2)
        avg = sum(s.seconds for s in st) / len(st) if st else 0
        calls = sum(s.calls for s in st)
        note = stopped.get(m, "")
        L.append(f"| {m} | {len(st)} | {len(els)} | {sum(e['flagged'] for e in els)} | "
                 f"{failed_pages} | {avg:.1f} | {calls} | {note} |")
    L.append("")
    L.append("평균 초/쪽에는 호출 간격 대기가 들어 있다. 시연 시간 계산은 이 값 × 페이지 수로 한다.")
    L.append("")
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
                if e["labels"] and any(x.startswith("R3") for x in e["flag_reasons"]):
                    L.append("- 라벨 대조: 원문 글자가 없어 대조하지 않음(R3)")
                elif e["labels"]:
                    checks = check_labels(e["labels"], texts[p])
                    marks = ", ".join(f"{lab} {'O' if ok else 'X'}" for lab, ok in checks)
                    L.append(f"- 라벨 대조(O=원문에 있음): {marks}")
                for reason in e["flag_reasons"]:
                    L.append(f"- 사유: {reason}")
                L.append("")
    (out / "report.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
