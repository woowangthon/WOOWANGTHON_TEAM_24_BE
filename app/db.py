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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY,
                element_id INTEGER NOT NULL REFERENCES elements(id),
                message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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

        page_ids = dict(
            connection.execute(
                "SELECT page_no, id FROM pages WHERE document_id = ?",
                (document_id,),
            )
        )

        for page_no, elements in page_results.items():
            page_id = page_ids[page_no]
            for element in elements:
                original = {
                    key: element[key]
                    for key in (
                        "type",
                        "title",
                        "alt",
                        "description",
                        "labels",
                        "table",
                        "uncertain_parts",
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
                        (
                            json.dumps(table, ensure_ascii=False)
                            if table is not None
                            else None
                        ),
                        json.dumps(
                            element["uncertain_parts"], ensure_ascii=False
                        ),
                        int(flagged),
                        json.dumps(
                            element["flag_reasons"], ensure_ascii=False
                        ),
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

            element["table"] = (
                json.loads(table_data) if table_data is not None else None
            )
            element["flagged"] = bool(element["flagged"])
            for field in ("labels", "uncertain_parts", "flag_reasons"):
                element[field] = json.loads(element[field])

            pages[page_id]["elements"].append(element)

        return dict(document, pages=list(pages.values()))


def get_page_image_path(page_id: int) -> str | None:
    with closing(sqlite3.connect(DB_PATH)) as connection:
        row = connection.execute(
            "SELECT image_path FROM pages WHERE id = ?",
            (page_id,),
        ).fetchone()
        return row[0] if row is not None else None


def _element_to_response(row: sqlite3.Row) -> dict:
    element = dict(row)
    table_data = element.pop("table_data")
    original = json.loads(element["ai_original"])

    element["table"] = (
        json.loads(table_data) if table_data is not None else None
    )
    element["flagged"] = bool(element["flagged"])
    element["ai_original"] = {
        "alt": original["alt"],
        "description": original["description"],
    }
    for field in ("labels", "uncertain_parts", "flag_reasons"):
        element[field] = json.loads(element[field])

    return element


def approve_element(element_id: int) -> dict | None:
    with closing(sqlite3.connect(DB_PATH)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT alt, description FROM elements WHERE id = ?",
            (element_id,),
        ).fetchone()
        if existing is None:
            return None
        if not existing["alt"].strip() or not existing["description"].strip():
            raise ValueError(
                "한 줄 설명과 상세 설명이 비어 있으면 승인할 수 없습니다. 설명을 작성한 뒤 수정 후 승인해 주세요."
            )

        updated = connection.execute(
            """
            UPDATE elements
            SET review_status = CASE
                    WHEN review_status = 'edited' THEN 'edited'
                    ELSE 'approved'
                END,
                reviewed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (element_id,),
        )
        if updated.rowcount == 0:
            return None

        row = connection.execute(
            """
            SELECT id, type, title, alt, description, labels, table_data,
                   uncertain_parts, flagged, flag_reasons, review_status,
                   ai_original
            FROM elements
            WHERE id = ?
            """,
            (element_id,),
        ).fetchone()
        return _element_to_response(row)


def edit_element(
    element_id: int,
    alt: str,
    description: str,
) -> dict | None:
    with closing(sqlite3.connect(DB_PATH)) as connection, connection:
        connection.row_factory = sqlite3.Row
        updated = connection.execute(
            """
            UPDATE elements
            SET alt = ?,
                description = ?,
                review_status = 'edited',
                reviewed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (alt, description, element_id),
        )
        if updated.rowcount == 0:
            return None

        row = connection.execute(
            """
            SELECT id, type, title, alt, description, labels, table_data,
                   uncertain_parts, flagged, flag_reasons, review_status,
                   ai_original
            FROM elements
            WHERE id = ?
            """,
            (element_id,),
        ).fetchone()
        return _element_to_response(row)


def publish_document(document_id: int) -> str | None:
    with closing(sqlite3.connect(DB_PATH)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        document = connection.execute(
            "SELECT status FROM documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        if document is None:
            return None

        if document[0] == "published":
            raise ValueError("이미 공개된 문서입니다.")
        if document[0] != "review_pending":
            raise ValueError("분석이 끝난 문서만 공개할 수 있어요.")

        remaining = connection.execute(
            """
            SELECT COUNT(*)
            FROM elements AS e
            JOIN pages AS p ON p.id = e.page_id
            WHERE p.document_id = ? AND e.review_status = 'needs_review'
            """,
            (document_id,),
        ).fetchone()[0]
        if remaining > 0:
            raise ValueError(f"검수 필요 그림이 {remaining}건 남아 있어요.")

        connection.execute(
            "UPDATE documents SET status = 'published' WHERE id = ?",
            (document_id,),
        )
        return "published"


def get_documents() -> list[dict]:
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT d.id, d.title, d.course, d.status,
                   d.total_pages, d.processed_pages,
                   COUNT(DISTINCT CASE
                       WHEN e.review_status = 'needs_review' THEN e.id
                   END) AS needs_review_count,
                   COUNT(DISTINCT CASE
                       WHEN e.review_status = 'needs_review'
                            AND r.id IS NOT NULL THEN e.id
                   END) AS report_count,
                   strftime(
                       '%Y-%m-%dT%H:%M:%S', d.created_at, '+9 hours'
                   ) || '+09:00' AS created_at
            FROM documents AS d
            LEFT JOIN pages AS p ON p.document_id = d.id
            LEFT JOIN elements AS e ON e.page_id = p.id
            LEFT JOIN reports AS r ON r.element_id = e.id
            GROUP BY d.id
            ORDER BY d.created_at DESC, d.id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def get_student_documents() -> list[dict]:
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, title, course, total_pages,
                   strftime(
                       '%Y-%m-%dT%H:%M:%S', created_at, '+9 hours'
                   ) || '+09:00' AS created_at
            FROM documents
            WHERE status = 'published'
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def get_student_document_detail(document_id: int) -> dict | None:
    document = get_document_detail(document_id)
    if document is None or document["status"] != "published":
        return None

    element_fields = (
        "id", "type", "title", "alt", "description",
        "table", "review_status",
    )
    return {
        "id": document["id"],
        "title": document["title"],
        "course": document["course"],
        "pages": [
            {
                "page_no": page["page_no"],
                "text": page["text"],
                "elements": [
                    {field: element[field] for field in element_fields}
                    for element in page["elements"]
                ],
            }
            for page in document["pages"]
        ],
    }