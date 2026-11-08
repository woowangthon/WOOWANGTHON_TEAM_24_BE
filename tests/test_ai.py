"""AI 모듈 테스트. 키 없이 돈다: python -m pytest tests/test_ai.py -q"""
import json

import pytest

from app.ai import analyze_page, flag_elements
from app.ai.analyze import _is_daily_quota, _retry_delay, _to_contract, failed_page, parse_response
from app.ai.flags import normalize


def el(**kw):
    base = {"id": "p1-e1", "type": "diagram", "title": "t", "alt": "a", "description": "d",
            "labels": [], "table": None, "uncertain_parts": []}
    base.update(kw)
    return base


def flags_of(element, text):
    return flag_elements({"page_no": 1, "elements": [element]}, text)["elements"][0]


TEXT = "TCP 연결 설정\nClient Server\nSYN SYN-\nACK ACK"


def test_auto_pass_when_labels_found():
    r = flags_of(el(labels=["Client", "SYN-ACK", "ACK"]), TEXT)
    assert r["flagged"] is False and r["flag_reasons"] == []


def test_r2_normalizes_case_space_symbols():
    # 줄바꿈으로 갈린 "SYN-\nACK", 대소문자 차이, 전각 문자
    r = flags_of(el(labels=["syn-ack", "ＣＬＩＥＮＴ"]), TEXT)
    assert r["flagged"] is False


def test_r2_missing_label_and_translation_trap():
    r = flags_of(el(labels=["Client", "라우터", "FIN"]), TEXT)
    assert r["flagged"] is True
    assert r["flag_reasons"] == ['R2: 그림 속 글자 "라우터", "FIN"를 원문에서 찾지 못함']


def test_r1_each_uncertain_part():
    r = flags_of(el(uncertain_parts=["화살표 방향 불분명", "글자 흐림"]), TEXT)
    assert [x[:3] for x in r["flag_reasons"]] == ["R1:", "R1:"]


def test_r3_when_no_text_layer():
    r = flags_of(el(labels=["Client"]), "   \n ")
    assert r["flag_reasons"] == ["R3: 원문 글자가 없어 자동 확인 불가"]


def test_r3_photo_with_labels_on_text_page():
    r = flags_of(el(type="photo", labels=["WAN"]), TEXT)
    assert r["flag_reasons"] == ["R3: 원문 글자가 없어 자동 확인 불가"]


def test_photo_without_labels_passes():
    assert flags_of(el(type="photo"), TEXT)["flagged"] is False


def test_r4_graph_with_numbers():
    r = flags_of(el(type="graph", labels=["cwnd"], description="16에서 8로 줄어듭니다"), "cwnd")
    assert r["flag_reasons"] == ["R4: 숫자가 들어간 그래프"]


def test_r4_ignores_step_numbers():
    d = "1) 꺾은선 그래프입니다.\n2) 가로축은 시간입니다. 3) 처리량이 늘어납니다."
    assert flags_of(el(type="graph", labels=["처리량"], description=d), "처리량")["flagged"] is False


def test_r4_keeps_numbers_in_parentheses_and_values():
    assert flags_of(el(type="graph", description="1) 최댓값(40)입니다."), "x")["flagged"] is True
    assert flags_of(el(type="graph", description="1) 약 40까지 오릅니다."), "x")["flagged"] is True


def test_graph_without_numbers_not_r4():
    assert flags_of(el(type="graph", labels=["cwnd"], description="늘다가 줄어듭니다"), "cwnd")["flagged"] is False


def test_flag_elements_does_not_mutate_input():
    page = {"page_no": 1, "elements": [el(labels=["X"])]}
    flag_elements(page, TEXT)
    assert "flagged" not in page["elements"][0]


def test_symbol_only_label_ignored():
    assert flags_of(el(labels=["→", "Client"]), TEXT)["flagged"] is False


def test_normalize():
    assert normalize(" SYN-ACK\n") == "synack"
    assert normalize("목적지 네트워크") == "목적지네트워크"


# ---------------------------------------------------------------- analyze 쪽

def test_parse_and_contract_shape():
    raw = json.dumps({"elements": [
        {"type": "table", "title": " 표 ", "alt": "a", "description": "d",
         "labels": ["A", " "], "table": {"headers": ["h1", "h2"], "rows": [{"cells": ["1", "2"]}]},
         "uncertain_parts": []},
        {"type": "diagram", "title": "그림", "alt": "a", "description": "d",
         "labels": [], "table": {"headers": [], "rows": []}, "uncertain_parts": [""]},
    ]})
    out = _to_contract(parse_response(raw), 3)
    assert out["page_no"] == 3
    assert [e["id"] for e in out["elements"]] == ["p3-e1", "p3-e2"]
    assert out["elements"][0]["table"] == {"headers": ["h1", "h2"], "rows": [["1", "2"]]}
    assert out["elements"][0]["title"] == "표" and out["elements"][0]["labels"] == ["A"]
    assert out["elements"][1]["table"] is None and out["elements"][1]["uncertain_parts"] == []


@pytest.mark.parametrize("bad", [None, "", "{not json", '{"elements": [{"type": "chart"}]}'])
def test_parse_rejects_bad_responses(bad):
    with pytest.raises(ValueError):  # pydantic ValidationError도 ValueError 하위 클래스
        parse_response(bad)


def _one(**kw):
    e = {"type": "diagram", "title": "t", "alt": "a", "description": "d", "labels": [],
         "table": {"headers": [], "rows": []}, "uncertain_parts": []}
    e.update(kw)
    return json.dumps({"elements": [e]})


@pytest.mark.parametrize("bad", [
    _one(title="  "),
    _one(alt="   "),
    _one(description=" \n "),
    _one(type="table", table={"headers": [], "rows": []}),                          # 빈 표
    _one(type="table", table={"headers": ["", " "], "rows": [{"cells": ["1", "2"]}]}),  # 머리글 전부 빈칸
    _one(type="table", table={"headers": ["A", "B"], "rows": []}),                  # 데이터 행 없음
    _one(type="table", table={"headers": ["A", "B", "C"], "rows": [{"cells": ["1", "2"]}]}),  # 열 수 불일치
])
def test_parse_rejects_unusable_content(bad):
    with pytest.raises(ValueError):
        parse_response(bad)


def test_parse_accepts_blank_corner_header_and_non_table_without_table():
    parse_response(_one(type="table", table={"headers": ["", "A"], "rows": [{"cells": ["x", "1"]}]}))
    parse_response(_one(type="graph"))  # table이 아닌 그림의 빈 table은 그대로 허용


def test_failed_page_becomes_r1():
    r = flag_elements(failed_page(4, "응답 형식 오류 2회"), "아무 글자")["elements"][0]
    assert r["id"] == "p4-e1" and r["flagged"] and r["flag_reasons"][0].startswith("R1:")


def test_429_parsing():
    per_min = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17s"}]}}
    per_day = json.loads(json.dumps(per_min).replace("PerMinute", "PerDay"))
    assert not _is_daily_quota(per_min) and _is_daily_quota(per_day)
    assert _retry_delay(per_min) == 18.0
    assert _retry_delay({"error": {}}) == 15.0


def test_mock_mode(monkeypatch):
    monkeypatch.setenv("AI_MODE", "mock")
    monkeypatch.setenv("AI_MOCK_DELAY", "0")
    for p in range(1, 7):
        r = analyze_page(b"", p)
        assert r["page_no"] == p
        assert all(e["id"].startswith(f"p{p}-e") for e in r["elements"])


# ---------------------------------------------------------------- 재시도 흐름 (가짜 클라이언트)

class _Resp:
    def __init__(self, text):
        self.text = text


GOOD = json.dumps({"elements": [{"type": "diagram", "title": "그림", "alt": "a", "description": "d",
                                 "labels": ["Client"], "table": {"headers": [], "rows": []},
                                 "uncertain_parts": []}]})


def _err429(quota_id, delay="3s"):
    from google.genai import errors
    return errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota_id}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]}})


@pytest.fixture
def fake(monkeypatch):
    import app.ai.analyze as A
    sleeps = []
    monkeypatch.setenv("AI_MODE", "live")
    monkeypatch.setenv("AI_RPM", "60000")
    monkeypatch.setattr(A.time, "sleep", lambda s: sleeps.append(s))

    class Models:
        def __init__(self):
            self.script = []

        def generate_content(self, **kw):
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return _Resp(item)

    class Client:
        models = Models()

    client = Client()
    monkeypatch.setattr(A, "_get_client", lambda: client)
    return A, client.models, sleeps


def test_per_minute_429_waits_then_succeeds(fake):
    A, models, sleeps = fake
    models.script = [_err429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "7s"), GOOD]
    result, st = A.analyze_page_with_stats(b"png", 2)
    assert result["elements"][0]["id"] == "p2-e1"
    assert st.calls == 2 and st.rate_waits == 1 and st.format_failures == 0
    assert 8.0 in sleeps


def test_daily_429_raises(fake):
    A, models, _ = fake
    models.script = [_err429("GenerateRequestsPerDayPerProjectPerModel-FreeTier")]
    with pytest.raises(A.AIQuotaExhausted):
        A.analyze_page(b"png", 1)


def test_format_error_retry_once_then_failed_page(fake):
    A, models, _ = fake
    models.script = ["{broken", '{"elements": [{"type": "x"}]}']
    result, st = A.analyze_page_with_stats(b"png", 5)
    assert st.calls == 2 and st.format_failures == 2
    assert result["elements"][0]["title"] == "분석 실패"


def test_format_error_then_success(fake):
    A, models, _ = fake
    models.script = ["", GOOD]
    result, st = A.analyze_page_with_stats(b"png", 1)
    assert st.format_failures == 1 and result["elements"][0]["title"] == "그림"


def test_blank_description_goes_through_retry(fake):
    A, models, _ = fake
    blank = json.loads(GOOD)
    blank["elements"][0]["description"] = "  "
    models.script = [json.dumps(blank), GOOD]
    result, st = A.analyze_page_with_stats(b"png", 1)
    assert st.format_failures == 1 and result["elements"][0]["description"] == "d"


def test_bad_request_is_raised(fake):
    from google.genai import errors
    A, models, _ = fake
    models.script = [errors.ClientError(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "model"}})]
    with pytest.raises(errors.ClientError):
        A.analyze_page(b"png", 1)
