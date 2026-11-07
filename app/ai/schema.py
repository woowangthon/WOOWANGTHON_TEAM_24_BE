"""LLM 응답 스키마.

Gemini에 response_schema로 넘기는 모델이다. 6장 데이터 계약과 모양이 조금 다르다.
- id, page_no는 모델이 아니라 코드가 붙인다(모델이 번호를 지어내지 않게).
- 표의 rows는 계약상 [[...]]이지만, 중첩 배열 스키마 지원이 모델마다 확실하지 않아
  [{"cells": [...]}] 로 받고 analyze.py에서 [[...]]로 바꾼다.
- table은 항상 받고(비어 있을 수 있음), table 타입이 아니면 계약에서는 null로 바꾼다.
"""
from typing import Literal

from pydantic import BaseModel, Field

ElementType = Literal["diagram", "table", "graph", "photo", "other"]


class TableRow(BaseModel):
    cells: list[str] = Field(default_factory=list)


class TableOut(BaseModel):
    headers: list[str] = Field(default_factory=list)
    rows: list[TableRow] = Field(default_factory=list)


class ElementOut(BaseModel):
    type: ElementType
    title: str
    alt: str
    description: str
    # 기본값 없이 필수로 둔다. 모델이 칸을 빼먹으면 형식 오류로 보고 다시 시도한다.
    # 특히 uncertain_parts가 빠지면 R1이 조용히 꺼진다.
    labels: list[str]
    table: TableOut
    uncertain_parts: list[str]


class PageOut(BaseModel):
    elements: list[ElementOut]
