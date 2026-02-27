from codegen_agent.utils import find_json_in_text


def test_find_json_in_text_plain_json():
    payload = find_json_in_text('{"ok": true, "n": 2}')
    assert payload == {"ok": True, "n": 2}


def test_find_json_in_text_skips_invalid_leading_brace():
    text = 'prefix {not json}\nthen valid {"status":"ok","count":3}'
    payload = find_json_in_text(text)
    assert payload == {"status": "ok", "count": 3}


def test_find_json_in_text_handles_arrays():
    payload = find_json_in_text("noise [1, 2, 3] trailing")
    assert payload == [1, 2, 3]


def test_find_json_in_text_returns_none_when_absent():
    assert find_json_in_text("no json here") is None
