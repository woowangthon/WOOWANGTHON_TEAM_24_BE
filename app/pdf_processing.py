import logging
import sqlite3
import subprocess
import sys
from contextlib import closing

import pymupdf

from app.db import DB_PATH, mark_document_failed, save_document_analysis


def extract_document_pages(document_id: int) -> None:
    data_dir = DB_PATH.parent
    pdf_path = data_dir / "uploads" / f"{document_id}.pdf"
    image_dir = data_dir / "pages" / str(document_id)

    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        document = connection.execute(
            "SELECT processed_pages FROM documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        if document is None:
            raise ValueError(f"문서 {document_id}를 찾을 수 없습니다.")

        image_dir.mkdir(parents=True, exist_ok=True)
        with pymupdf.open(pdf_path) as pdf:
            for page_index in range(document[0], pdf.page_count):
                page_no = page_index + 1
                page = pdf[page_index]
                text = page.get_text()
                image_path = image_dir / f"{page_no}.png"
                page.get_pixmap(dpi=150).save(image_path)
                relative_path = image_path.relative_to(data_dir).as_posix()

                with connection:
                    connection.execute(
                        """
                        INSERT INTO pages (document_id, page_no, image_path, text)
                        VALUES (?, ?, ?, ?)
                        """,
                        (document_id, page_no, relative_path, text),
                    )
                    connection.execute(
                        "UPDATE documents SET processed_pages = ? WHERE id = ?",
                        (page_no, document_id),
                    )


def process_document_in_background(document_id: int) -> None:
    try:
        subprocess.run(
            [sys.executable, "-m", "app.pdf_processing", str(document_id)],
            cwd=DB_PATH.parent.parent,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        logging.getLogger(__name__).exception(
            "문서 %s의 PDF 추출 실패", document_id
        )
        mark_document_failed(document_id)


def analyze_document(document_id: int) -> None:
    """페이지마다 AI 분석(F3) → 검수 필요 판단(F4, R1~R4) → AI 교차 검토(R5) 후 한 번에 저장.

    R5는 R1~R4에 걸리지 않은 자동 통과 후보가 있는 쪽에서만 부른다(AI_REVIEW=off면 생략).
    AIQuotaExhausted·AIUnavailable는 잡지 않는다(교차 검토 호출 포함) → 하위 프로세스가
    실패로 끝나고 process_document_in_background가 문서를 failed로 바꾼다.
    AI_MODE=mock이면 AI 모듈이 키 없이 고정 결과를 돌려준다(교차 검토는 지적 없음).
    """
    from app.ai import analyze_page, apply_review, flag_elements, review_enabled, review_page

    data_dir = DB_PATH.parent
    with closing(sqlite3.connect(DB_PATH)) as connection:
        rows = connection.execute(
            "SELECT page_no, image_path, text FROM pages"
            " WHERE document_id = ? ORDER BY page_no",
            (document_id,),
        ).fetchall()

    use_review = review_enabled()
    page_results: dict[int, list[dict]] = {}
    for page_no, image_path, text in rows:
        image_png = (data_dir / image_path).read_bytes()
        page = analyze_page(image_png, page_no)
        page = flag_elements(page, text or "")
        if use_review and any(not el["flagged"] for el in page["elements"]):
            page = apply_review(page, review_page(image_png, page))
        page_results[page_no] = page["elements"]

    save_document_analysis(document_id, page_results)


def process_document(document_id: int) -> None:
    extract_document_pages(document_id)
    analyze_document(document_id)


if __name__ == "__main__":
    process_document(int(sys.argv[1]))