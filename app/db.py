import json
import sqlite3
from contextlib import closing
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "app.db"


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                course TEXT NOT NULL,
                filename TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'processing',
                total_pages INTEGER NOT NULL DEFAULT 0,
                processed_pages INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY,
                document_id INTEGER NOT NULL REFERENCES documents(id),
                page_no INTEGER NOT NULL,
                image_path TEXT NOT NULL,
                text TEXT NOT NULL,
                UNIQUE (document_id, page_no)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS elements (
                id INTEGER PRIMARY KEY,
                page_id INTEGER NOT NULL REFERENCES pages(id),
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                alt TEXT NOT NULL,
                description TEXT NOT NULL,
                labels TEXT NOT NULL,
                table_data TEXT,
                uncertain_parts TEXT NOT NULL,
                flagged INTEGER NOT NULL,
                flag_reasons TEXT NOT NULL,
                review_status TEXT NOT NULL,
                ai_original TEXT NOT NULL,
                reviewed_at TEXT
            )
            """
        )
        connection.commit()


def create_document(
    title: str,
    course: str,
    filename: str,
    total_pages: int,
    pdf_bytes: bytes,
) -> int:
    upload_dir = DB_PATH.parent / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(DB_PATH)) as connection:
        cursor = connection.execute(
            """
            INSERT INTO documents (title, course, filename, total_pages)
            VALUES (?, ?, ?, ?)
            """,
            (title, course, filename, total_pages),
        )
        document_id = cursor.lastrowid
        pdf_path = upload_dir / f"{document_id}.pdf"

        try:
            pdf_path.write_bytes(pdf_bytes)
            connection.commit()
        except (OSError, sqlite3.Error):
            pdf_path.unlink(missing_ok=True)
            raise

    return document_id


def get_document_progress(document_id: int) -> dict | None:
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT status, processed_pages, total_pages
            FROM documents
            WHERE id = ?
            """,
            (document_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def mark_document_failed(document_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as connection, connection:
        connection.execute(
            "UPDATE documents SET status = 'failed' WHERE id = ?",
            (document_id,),
        )


def save_document_analysis(
    document_id: int,
    page_results: dict[int, list[dict]],
) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        updated = connection.execute(
            """
            UPDATE documents
            SET status = 'review_pending'
            WHERE id = ? AND status = 'processing'
            AND total_pages > 0 AND processed_pages = total_pages
            """,
            (document_id,),
        )
        if updated.rowcount != 1:
            return False

        page_ids = dict(connection.execute(
            "SELECT page_no, id FROM pages WHERE document_id = ?",
            (document_id,),
        ))

        for page_no, elements in page_results.items():
            page_id = page_ids[page_no]
            for element in elements:
                original = {
                    key: element[key]
                    for key in (
                        "type", "title", "alt", "description",
                        "labels", "table", "uncertain_parts",
                    )
                }
                flagged = element["flagged"]
                table = element["table"]

                connection.execute(
                    """
                    INSERT INTO elements (
                        page_id, type, title, alt, description, labels,
                        table_data, uncertain_parts, flagged, flag_reasons,
                        review_status, ai_original
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        page_id,
                        element["type"],
                        element["title"],
                        element["alt"],
                        element["description"],
                        json.dumps(element["labels"], ensure_ascii=False),
                        json.dumps(table, ensure_ascii=False) if table is not None else None,
                        json.dumps(element["uncertain_parts"], ensure_ascii=False),
                        int(flagged),
                        json.dumps(element["flag_reasons"], ensure_ascii=False),
                        "needs_review" if flagged else "auto_passed",
                        json.dumps(original, ensure_ascii=False),
                    ),
                )

    return True


def get_document_detail(document_id: int) -> dict | None:
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")

        document = connection.execute(
            """
            SELECT id, title, course, status, total_pages, processed_pages
            FROM documents
            WHERE id = ?
            """,
            (document_id,),
        ).fetchone()

        if document is None:
            return None

        page_rows = connection.execute(
            """
            SELECT id, page_no, text
            FROM pages
            WHERE document_id = ?
            ORDER BY page_no
            """,
            (document_id,),
        ).fetchall()

        pages = {row["id"]: dict(row, elements=[]) for row in page_rows}

        element_rows = connection.execute(
            """
            SELECT e.id, e.page_id, e.type, e.title, e.alt, e.description,
                   e.labels, e.table_data, e.uncertain_parts, e.flagged,
                   e.flag_reasons, e.review_status, e.ai_original
            FROM elements AS e
            JOIN pages AS p ON p.id = e.page_id
            WHERE p.document_id = ?
            ORDER BY p.page_no, e.id
            """,
            (document_id,),
        )

        for row in element_rows:
            element = dict(row)
            page_id = element.pop("page_id")
            table_data = element.pop("table_data")
            original = json.loads(element["ai_original"])
            element["ai_original"] = {
                "alt": original["alt"],
                "description": original["description"],
            }

            element["table"] = json.loads(table_data) if table_data is not None else None
            element["flagged"] = bool(element["flagged"])
            for field in ("labels", "uncertain_parts", "flag_reasons"):
                element[field] = json.loads(element[field])

            pages[page_id]["elements"].append(element)

        return dict(document, pages=list(pages.values()))