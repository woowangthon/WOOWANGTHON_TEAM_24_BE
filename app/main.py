from contextlib import asynccontextmanager

import pymupdf
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.db import (
    DB_PATH,
    create_document,
    get_document_detail,
    get_document_progress,
    get_page_image_path,
    get_student_documents,
    init_db,
)
from app.pdf_processing import process_document_in_background

MAX_PDF_BYTES = 20_000_000
MAX_PDF_PAGES = 30


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="보이는 강의 API", lifespan=lifespan)


@app.get("/")
def read_root():
    return {"status": "ok"}


@app.post("/api/documents", status_code=201)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    title: str = Form(),
    course: str = Form(),
):
    title = title.strip()
    course = course.strip()
    if not title or not course:
        raise HTTPException(422, "강의 제목과 과목명을 입력해 주세요.")

    pdf_bytes = await file.read(MAX_PDF_BYTES + 1)
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise HTTPException(413, "PDF는 20MB 이하여야 합니다.")

    try:
        with pymupdf.open(stream=pdf_bytes) as pdf:
            if not pdf.is_pdf or pdf.needs_pass:
                raise HTTPException(400, "암호가 없는 PDF 파일을 올려 주세요.")
            total_pages = pdf.page_count
    except pymupdf.FileDataError as error:
        raise HTTPException(400, "PDF 파일을 열 수 없습니다.") from error

    if not 1 <= total_pages <= MAX_PDF_PAGES:
        raise HTTPException(400, "PDF는 1~30페이지여야 합니다.")

    document_id = create_document(
        title, course, file.filename or "upload.pdf", total_pages, pdf_bytes
    )
    background_tasks.add_task(process_document_in_background, document_id)
    return {"id": document_id}


@app.get("/api/documents/{document_id}/progress")
def read_document_progress(document_id: int):
    progress = get_document_progress(document_id)
    if progress is None:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    return progress


@app.get("/api/documents/{document_id}")
def read_document_detail(document_id: int):
    document = get_document_detail(document_id)
    if document is None:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    return document


@app.get("/api/pages/{page_id}/image")
def read_page_image(page_id: int):
    stored_path = get_page_image_path(page_id)
    if stored_path is None:
        raise HTTPException(404, "페이지를 찾을 수 없습니다.")

    data_dir = DB_PATH.parent
    images_dir = (data_dir / "pages").resolve()
    image_path = (data_dir / stored_path).resolve()

    if not image_path.is_relative_to(images_dir) or not image_path.is_file():
        raise HTTPException(404, "페이지 이미지를 찾을 수 없습니다.")

    return FileResponse(image_path, media_type="image/png")


@app.get("/api/student/documents")
def read_student_documents():
    return get_student_documents()