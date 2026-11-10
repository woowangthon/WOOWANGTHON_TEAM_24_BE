import json
import logging
import os
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


def process_document(document_id: int) -> None:
    extract_document_pages(document_id)

    if os.getenv("AI_MODE") == "mock":
        example_path = DB_PATH.parent.parent / "examples" / "document-detail.json"
        example = json.loads(example_path.read_text(encoding="utf-8"))
        first_page_elements = example["pages"][0]["elements"]
        save_document_analysis(document_id, {1: first_page_elements})


if __name__ == "__main__":
    process_document(int(sys.argv[1]))