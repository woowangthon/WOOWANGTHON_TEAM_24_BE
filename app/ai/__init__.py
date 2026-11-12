"""보이는 강의 AI 모듈

    from app.ai import analyze_page, flag_elements, AIQuotaExhausted, AIUnavailable

    result = analyze_page(png_bytes, page_no=3)      # F3
    result = flag_elements(result, page_text)          # F4, 그림마다 flagged·flag_reasons 추가

    # R5 (자동 통과 후보가 있을 때만, AI_REVIEW=off면 생략)
    if review_enabled() and any(not el["flagged"] for el in result["elements"]):
        result = apply_review(result, review_page(png_bytes, result))
"""
from .analyze import AIQuotaExhausted, AIUnavailable, analyze_page, analyze_page_with_stats
from .flags import apply_review, flag_elements
from .review import review_enabled, review_page

__all__ = [
    "analyze_page",
    "analyze_page_with_stats",
    "flag_elements",
    "apply_review",
    "review_enabled",
    "review_page",
    "AIQuotaExhausted",
    "AIUnavailable",
]
