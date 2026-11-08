"""보이는 강의 AI 모듈

    from app.ai import analyze_page, flag_elements, AIQuotaExhausted, AIUnavailable

    result = analyze_page(png_bytes, page_no=3)      # F3
    result = flag_elements(result, page_text)          # F4, 그림마다 flagged·flag_reasons 추가
"""
from .analyze import AIQuotaExhausted, AIUnavailable, analyze_page, analyze_page_with_stats
from .flags import flag_elements

__all__ = [
    "analyze_page",
    "analyze_page_with_stats",
    "flag_elements",
    "AIQuotaExhausted",
    "AIUnavailable",
]
