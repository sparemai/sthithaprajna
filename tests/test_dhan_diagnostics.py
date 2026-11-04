"""Preserve useful provider errors without exposing credentials or guessing their cause."""
import json

import httpx
import pytest

from sthitaprajna.data.dhan_live import DhanClient, DhanError, error_from_response
from sthitaprajna.data.dhan_check import run_check
from fake_dhan import CLIENT_ID, TOKEN, FakeClock, FakeDhan, ist


def response(body, status=400, token=TOKEN):
    request = httpx.Request('POST', 'https://api.dhan.co/v2/marketfeed/quote',
                            headers={'access-token': token, 'client-id': CLIENT_ID})
    return httpx.Response(status, json=body, request=request)


def test_numeric_data_error_is_visible_without_assuming_websocket_meaning():
    error = error_from_response(response({'status': 'failed', 'data': {'999': 'Provider explanation'}}))
    assert error.code == '999'
    assert 'Provider explanation' in str(error)
    assert 'bug in' not in error.fix


@pytest.mark.parametrize('field', ['remarks', 'error', 'data'])
def test_string_error_fields_are_visible(field):
    assert 'Provider explanation' in str(error_from_response(response({field: 'Provider explanation'})))


def test_redacts_long_token_and_client_id_before_truncating():
    token = 'secret-token-' * 60
    error = error_from_response(response({'errorMessage': token + ' client=' + CLIENT_ID}, token=token), token)
    assert 'secret-token-' not in str(error)
    assert CLIENT_ID not in str(error)
    assert '***' in str(error)


def test_non_json_body_is_not_printed():
    r = httpx.Response(400, text=TOKEN, request=response({}).request)
    error = error_from_response(r, TOKEN)
    assert 'non-JSON' in str(error) and TOKEN not in str(error)


@pytest.mark.parametrize('status', [200, 400])
def test_unknown_failed_response_does_not_trigger_ohlc_fallback(status):
    requests = []
    def handler(req):
        requests.append(req.url.path)
        return httpx.Response(status, json={'status': 'failed', 'data': {'999': 'Provider explanation'}})
    c = DhanClient(CLIENT_ID, TOKEN, transport=httpx.MockTransport(handler), sleep=lambda _: None)
    try:
        with pytest.raises(DhanError, match='Provider explanation'):
            c.quotes({'IDX_I': [13]})
        assert requests == ['/v2/marketfeed/quote']
    finally:
        c.close()


def test_check_isolates_segments_and_keeps_failure_visible(tmp_path):
    day = ist(2026, 9, 27, 11, 0)
    fake = FakeDhan(FakeClock(day))
    calls = []
    def handler(req):
        if req.url.path.endswith('/marketfeed/quote'):
            payload = json.loads(req.content)
            calls.append(payload)
            if 'IDX_I' in payload:
                return httpx.Response(400, json={'status': 'failed', 'data': {'999': 'Index request rejected'}})
        return fake.handler(req)
    client = DhanClient(CLIENT_ID, TOKEN, transport=httpx.MockTransport(handler), sleep=lambda _: None)
    lines = []
    assert run_check(lines.append, client=client, cache_dir=tmp_path, today=day.date()) == 1
    output = '\n'.join(lines)
    assert 'Index request rejected' in output and 'quotes NSE_EQ' in output
    assert 'Result: NOT READY' in output
    assert TOKEN not in output and CLIENT_ID not in output
    assert len(calls) == 3
    assert list(calls[1]) == ['IDX_I'] and list(calls[2]) == ['NSE_EQ']
