from kamandal_v2.intelligence.llm_client import _extract_json_object


def test_extract_json_repairs_control_characters_and_truncated_containers() -> None:
    payload = _extract_json_object('{"ideas":[{"notes":"line one\nline two","symbol":"AMZN"}')

    assert payload["ideas"][0]["notes"] == "line one\nline two"
    assert payload["ideas"][0]["symbol"] == "AMZN"


def test_extract_json_accepts_fenced_object() -> None:
    assert _extract_json_object('```json\n{"ideas": []}\n```') == {"ideas": []}
