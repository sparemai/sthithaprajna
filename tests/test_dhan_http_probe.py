import json

import httpx

from sthitaprajna.data.dhan_http_probe import probe_httpx, redact, report


def test_redaction_precedes_shortening_and_html_cleanup():
    token = 'secretpart' * 60
    result = redact('<html>' + token + ' account=12345678 bad request</html>', '12345678', token)
    assert 'secretpart' not in result and '12345678' not in result
    assert 'bad request' in result and '[hidden]' in result


def test_error_report_shows_safe_response_and_metadata(capsys):
    report('test', 400, {'content-type': 'text/html', 'server': 'gateway'},
           b'<title>Bad Request</title> token=secret client=12345678', '12345678', 'secret')
    out = capsys.readouterr().out
    assert 'Bad Request' in out and 'HTTP 400' in out and 'gateway' in out
    assert 'secret' not in out and '12345678' not in out


def test_three_payloads_use_only_the_quote_endpoint(monkeypatch, capsys):
    monkeypatch.setattr('sthitaprajna.data.dhan_http_probe.time.sleep', lambda _: None)
    payloads, raw = [], []
    def handler(req):
        assert str(req.url) == 'https://api.dhan.co/v2/marketfeed/quote'
        assert req.headers['access-token'] == 'secret'
        payloads.append(json.loads(req.content)); raw.append(req.content)
        return httpx.Response(400, text='Bad Request')
    with httpx.Client(transport=httpx.MockTransport(handler), headers={'access-token': 'secret', 'client-id': '12345678'}) as client:
        probe_httpx(client, '12345678', 'secret')
    assert payloads[:2] == [{'NSE_EQ': [2885]}] * 2
    assert payloads[2] == {'NSE_EQ': [2885], 'dhanClientId': '12345678'}
    assert raw[0] != raw[1]
    assert '12345678' not in capsys.readouterr().out


def test_rate_limit_stops_further_requests(capsys):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(429, json={'message': 'Rate limited'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        probe_httpx(client, '12345678', 'secret')
    assert len(calls) == 1
    assert 'stopping' in capsys.readouterr().out


def test_success_reports_quote_presence_without_dumping_body(capsys):
    report('test', 200, {}, b'{"data":{"NSE_EQ":{"2885":{"last_price":1234}}}}', '12345678', 'secret')
    out = capsys.readouterr().out
    assert 'returned: True' in out and 'last_price' not in out
