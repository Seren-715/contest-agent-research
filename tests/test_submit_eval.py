import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import submit_eval as se  # noqa: E402

CHROME = r"""curl 'https://example.test/api/v1/evaluations?x=1' \
  -H 'accept: application/json' \
  -H 'authorization: Bearer SECRET-TOKEN' \
  -H 'content-type: application/json' \
  -b 'sid=COOKIE-VALUE' \
  --data-raw '{"submission_id":"sub-20261026-0e295474","note":"a \"q\""}' \
  --compressed"""


def test_parse_copy_as_curl_bash():
    pairs = se.parse_curl(CHROME)
    assert pairs[0] == ("url", "https://example.test/api/v1/evaluations?x=1")
    assert ("cookie", "sid=COOKIE-VALUE") in pairs
    assert ("data-raw", '{"submission_id":"sub-20261026-0e295474","note":"a \\"q\\""}') in pairs
    assert ("compressed", None) in pairs


def test_describe_hides_secrets():
    text = se.describe(se.parse_curl(CHROME))
    assert "SECRET-TOKEN" not in text and "COOKIE-VALUE" not in text and "x=1" not in text
    assert text.startswith("POST https://example.test/api/v1/evaluations")
    assert "authorization" in text and "Cookie" in text


def test_config_quotes_values():
    config = se.to_config([("url", "https://a.test/"), ("data-raw", 'a"b\\c\nd'), ("compressed", None)])
    assert 'data-raw = "a\\"b\\\\c\\nd"' in config
    assert config.splitlines()[-1] == "compressed"


@pytest.mark.parametrize("text", [
    "wget https://a.test/",
    "curl 'https://a.test/' -F 'file=@x.tar'",
    "curl ^\"https://a.test/^\"",
    "curl 'https://a.test/' --weird 1",
])
def test_parse_rejects(text):
    with pytest.raises(se.CurlParseError):
        se.parse_curl(text)


@pytest.mark.parametrize("status,body,expected", [
    (200, '{"id":"eval-20261102-3c044536","status":"QUEUED_FOR_IMPORT"}', "success"),
    (200, '{"code":1,"msg":"当前队伍已有评测正在进行"}', "busy"),
    (200, '{"msg":"评测 eval-20261102-3c044536 进行中，请稍后"}', "stop"),
    (200, '{"ok":true}', "stop"),
    (429, "", "busy"),
    (503, "bad gateway", "busy"),
    (0, "transport error", "busy"),
    (401, "", "auth"),
    (403, "", "auth"),
    (400, '{"detail":"排队中"}', "busy"),
    (400, '{"detail":"invalid submission"}', "stop"),
])
def test_classify(status, body, expected):
    assert se.classify(status, body) == expected


def test_redact_and_wait():
    out = se.redact('{"token":"abc","msg":"x"} Bearer eyJhb.c-d')
    assert "abc" not in out and "eyJhb" not in out
    rng = se.random.Random(0)
    assert se.next_wait(1, None, rng) >= se.MIN_INTERVAL * 0.9
    assert se.next_wait(60, "300", rng) >= 270


def _fake_curl(tmp_path, responses):
    state = tmp_path / "n"
    state.write_text("0")
    script = tmp_path / "curl"
    script.write_text(
        "#!%s\n" % sys.executable
        + "import sys,json\n"
        + "cfg=sys.stdin.read()\n"
        + "assert '-K' in sys.argv and 'SECRET-TOKEN' not in ' '.join(sys.argv)\n"
        + "open(%r,'a').write(cfg)\n" % str(tmp_path / "seen")
        + "n=int(open(%r).read()); open(%r,'w').write(str(n+1))\n" % (str(state), str(state))
        + "code,body=json.loads(%r)[n]\n" % __import__("json").dumps(responses)
        + "sys.stdout.write('HTTP/1.1 %d X\\r\\nContent-Type: application/json\\r\\n\\r\\n'%code+body+'\\n__SUBMIT_EVAL_STATUS__%d'%code)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return state


def test_main_retries_busy_then_succeeds(tmp_path, monkeypatch, capsys):
    curl_file = tmp_path / "eval.curl"
    curl_file.write_text(CHROME)
    state = _fake_curl(tmp_path, [[200, '{"msg":"排队中"}'], [200, '{"id":"eval-20261102-abcdef12"}']])
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(se.time, "sleep", lambda s: None)
    assert se.main([str(curl_file), "--interval", "20"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "eval-20261102-abcdef12"
    assert "SECRET-TOKEN" not in captured.err and "COOKIE-VALUE" not in captured.err
    assert state.read_text() == "2"
    assert 'cookie = "sid=COOKIE-VALUE"' in (tmp_path / "seen").read_text()


def test_main_stops_on_unknown_and_dry_run_sends_nothing(tmp_path, monkeypatch):
    curl_file = tmp_path / "eval.curl"
    curl_file.write_text(CHROME)
    state = _fake_curl(tmp_path, [[200, '{"ok":true}'], [200, '{"ok":true}']])
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(se.time, "sleep", lambda s: None)
    assert se.main([str(curl_file), "--dry-run"]) == 0
    assert state.read_text() == "0"
    assert se.main([str(curl_file)]) == 4
    assert state.read_text() == "1"
