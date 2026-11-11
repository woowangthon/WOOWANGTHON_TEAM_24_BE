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
    _one(type="table", table={"headers": ["A", "B"], "rows": []}),                  # 데이터 행 없음
    _one(type="table", table={"headers": ["A", "B", "C"], "rows": [{"cells": ["1", "2"]}]}),  # 열 수 불일치
    _one(type="table", table={"headers": [], "rows": [{"cells": ["", " "]}]}),      # 칸 전부 빈칸
    _one(type="table", table={"headers": [], "rows": [{"cells": ["1", "2"]}, {"cells": ["3"]}]}),  # 머리글 없는 표, 행끼리 칸 수 다름
    _one(type="table", table={"headers": ["", " "], "rows": [{"cells": ["1", "2"]}, {"cells": ["3"]}]}),  # 머리글 전부 빈칸 + 칸 수 다름
])
def test_parse_rejects_unusable_content(bad):
    with pytest.raises(ValueError):
        parse_response(bad)


def test_parse_accepts_blank_corner_header_and_non_table_without_table():
    parse_response(_one(type="table", table={"headers": ["", "A"], "rows": [{"cells": ["x", "1"]}]}))
    parse_response(_one(type="graph"))  # table이 아닌 그림의 빈 table은 그대로 허용


def test_headerless_table_is_accepted_with_empty_headers():
    # 4쪽 OSI 표처럼 원본에 머리글 행이 없는 표
    rows = [{"cells": ["7", "응용 계층"]}, {"cells": ["6", "표현 계층"]}]
    for headers in ([], ["", " "]):
        page = parse_response(_one(type="table", table={"headers": headers, "rows": rows}))
        out = _to_contract(page, 4)
        assert out["elements"][0]["table"] == {"headers": [], "rows": [["7", "응용 계층"], ["6", "표현 계층"]]}


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


# ---------------------------------------------------------------- 교차 검토 (실험)

from app.ai.review import _review_input, parse_review  # noqa: E402

PAGE = {"page_no": 3, "elements": [
    {**el(id="p3-e1", labels=["A"]), "flagged": False, "flag_reasons": []},
    {**el(id="p3-e2", type="table", table={"headers": ["h"], "rows": [["1"]]}), "flagged": True,
     "flag_reasons": ["R1: x"]},
]}


def test_parse_review_maps_index_to_id():
    raw = json.dumps({"elements": [{"index": 2, "issues": ["설명: a / 그림: b", "  "]}]})
    assert parse_review(raw, ["p3-e1", "p3-e2"]) == {"p3-e1": [], "p3-e2": ["설명: a / 그림: b"]}


@pytest.mark.parametrize("bad", [None, "", "{x", '{"elements": [{"index": 3, "issues": []}]}',
                                 '{"elements": [{"index": 0, "issues": []}]}'])
def test_parse_review_rejects_bad(bad):
    with pytest.raises(ValueError):
        parse_review(bad, ["p3-e1", "p3-e2"])


def test_review_input_has_no_flag_results():
    sent = json.loads(_review_input(PAGE))["elements"]
    assert [x["index"] for x in sent] == [1, 2]
    assert "flagged" not in sent[0] and "flag_reasons" not in sent[1] and "uncertain_parts" not in sent[0]
    assert "table" not in sent[0] and sent[1]["table"] == {"headers": ["h"], "rows": [["1"]]}


def test_review_skips_page_without_elements(monkeypatch):
    from app.ai.review import review_page_with_stats
    monkeypatch.setenv("AI_MODE", "live")
    result, st = review_page_with_stats(b"png", {"page_no": 2, "elements": []})
    assert result == {} and st.calls == 0


def test_review_mock(monkeypatch):
    from app.ai.review import review_page_with_stats
    monkeypatch.setenv("AI_MODE", "mock")
    result, st = review_page_with_stats(b"png", PAGE)
    assert result == {"p3-e1": [], "p3-e2": []} and st.calls == 0


def test_review_format_error_retry_then_success(fake):
    from app.ai.review import review_page_with_stats
    A, models, _ = fake
    models.script = ["{broken", json.dumps({"elements": [{"index": 1, "issues": ["설명: 선 / 그림: 화살표"]}]})]
    result, st = review_page_with_stats(b"png", PAGE)
    assert st.calls == 2 and st.format_failures == 1
    assert result == {"p3-e1": ["설명: 선 / 그림: 화살표"], "p3-e2": []}


def test_review_two_format_errors_is_none(fake):
    from app.ai.review import review_page_with_stats
    A, models, _ = fake
    models.script = ["", '{"elements": [{"index": 9, "issues": []}]}']
    result, st = review_page_with_stats(b"png", PAGE)
    assert result is None and st.format_failures == 2


def test_review_daily_quota_raises(fake):
    from app.ai.review import review_page_with_stats
    A, models, _ = fake
    models.script = [_err429("GenerateRequestsPerDayPerProjectPerModel-FreeTier")]
    with pytest.raises(A.AIQuotaExhausted):
        review_page_with_stats(b"png", PAGE)


def test_parse_review_rejects_issue_without_format():
    # "설명: … / 그림: …" 형식이 아니면 사유에 넣을 의심 문장을 뽑을 수 없으므로 형식 오류
    raw = json.dumps({"elements": [{"index": 1, "issues": ["화살표 방향이 반대입니다"]}]})
    with pytest.raises(ValueError):
        parse_review(raw, ["p3-e1", "p3-e2"])


# ---------------------------------------------------------------- R5 사유 (이슈 #7)

from app.ai import apply_review, review_enabled  # noqa: E402
from app.ai.flags import R5_FAILED, R5_MISSING, review_reasons, split_issue  # noqa: E402


def test_split_issue():
    assert split_issue("설명: A에서 B로 / 그림: 양방향") == ("A에서 B로", "양방향")
    assert split_issue("설명:(빠짐)/그림: Ping–ICMP 연결") == ("(빠짐)", "Ping–ICMP 연결")
    assert split_issue("설명: 1/2 크기 / 그림: 1/3 크기") == ("1/2 크기", "1/3 크기")
    assert split_issue("그림: 양방향") is None
    assert split_issue("") is None


def test_review_reasons_keep_only_description_side():
    reasons = review_reasons(['설명: "Network에서 통신망으로" / 그림: 양 끝 화살촉(HTTPS)'])
    assert reasons == ['R5: AI 교차 검토가 그림과 다를 수 있다고 본 문장: "Network에서 통신망으로"']
    assert "HTTPS" not in reasons[0] and "화살촉" not in reasons[0]   # AI 정정 내용은 안 넣음


def test_review_reasons_missing_is_one_line_and_last():
    reasons = review_reasons(["설명: (빠짐) / 그림: Ping–ICMP", "설명: 네 개의 열 / 그림: 5열",
                              "설명: 빠짐 / 그림: 선 위 숫자"])
    assert reasons == ['R5: AI 교차 검토가 그림과 다를 수 있다고 본 문장: "네 개의 열"', R5_MISSING]


def test_review_reasons_dedupe_and_truncate():
    long = "가" * 200
    reasons = review_reasons([f"설명: {long} / 그림: x", f"설명: {long} / 그림: y", "형식 아님"])
    assert len(reasons) == 1 and reasons[0].endswith('…"') and len(reasons[0]) < 200


R5_PAGE = {"page_no": 3, "elements": [
    {**el(id="p3-e1"), "flagged": False, "flag_reasons": []},
    {**el(id="p3-e2"), "flagged": True, "flag_reasons": ["R2: x"]},
    {**el(id="p3-e3"), "flagged": False, "flag_reasons": []},
]}


def test_apply_review_only_touches_auto_pass_candidates():
    review = {"p3-e1": ["설명: 한 방향 / 그림: 양방향"], "p3-e2": ["설명: 표 / 그림: 그림"], "p3-e3": []}
    out = apply_review(R5_PAGE, review)["elements"]
    assert out[0]["flagged"] is True
    assert out[0]["flag_reasons"] == ['R5: AI 교차 검토가 그림과 다를 수 있다고 본 문장: "한 방향"']
    assert out[1]["flag_reasons"] == ["R2: x"]          # R1~R4로 걸린 그림은 그대로
    assert out[2]["flagged"] is False and out[2]["flag_reasons"] == []
    assert R5_PAGE["elements"][0]["flagged"] is False   # 입력은 바꾸지 않는다


def test_apply_review_failure_flags_all_candidates():
    out = apply_review(R5_PAGE, None)["elements"]
    assert [e["flag_reasons"] for e in out] == [[R5_FAILED], ["R2: x"], [R5_FAILED]]
    assert all(e["flagged"] for e in out)


def test_apply_review_missing_id_means_no_issue():
    out = apply_review(R5_PAGE, {})["elements"]
    assert out[0]["flagged"] is False and out[2]["flagged"] is False


@pytest.mark.parametrize("value,expected", [(None, True), ("on", True), ("off", False), ("OFF", False),
                                            ("0", False), ("false", False), ("1", True)])
def test_review_enabled(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("AI_REVIEW", raising=False)
    else:
        monkeypatch.setenv("AI_REVIEW", value)
    assert review_enabled() is expected
