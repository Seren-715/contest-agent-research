"""Replay the platform's own "start evaluation" request until a slot is free.

The platform endpoints are not documented here and this repo cannot reach the
platform, so nothing is guessed: the user copies the real request from the
browser (DevTools -> Network -> the request sent when clicking the evaluate
button -> Copy -> Copy as cURL (bash)) into a local file, and this script
replays exactly that request.

* Credentials stay in that file; they are passed to curl through stdin
  (``curl -K -``), never on the command line, and are never printed.
* Only a response carrying an evaluation id (``eval-YYYYMMDD-hex``, the form
  the platform returned on 2026-10-26) counts as success. A response that looks
  busy is retried after an interval; anything unrecognised stops the loop,
  because the request has side effects and must not be replayed blindly.
* The tar upload itself is still done in the web page: DevTools strips the
  file bytes from a copied multipart upload, so it cannot be replayed.

Usage:
    python scripts/submit_eval.py ~/eval.curl --dry-run
    python scripts/submit_eval.py ~/eval.curl --interval 60 --max-minutes 120
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shlex
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

MIN_INTERVAL = 20.0
DEFAULT_OK = r"eval-\d{8}-[0-9a-f]{6,}"
DEFAULT_BUSY = (
    r"排队|队列|已有.{0,12}评测|评测中|进行中|正在评测|繁忙|稍后|频繁|限流|"
    r"busy|in progress|already running|too many|rate limit|try again"
)
SENSITIVE_KEY = re.compile(
    r'("?(?:token|access_token|refresh_token|api_key|apikey|key|secret|password|'
    r'cookie|authorization|session)"?\s*[:=]\s*)("[^"]*"|[^,\s}]+)',
    re.IGNORECASE,
)

# curl options copied by browsers that take a value, mapped to config names.
VALUE_OPTS = {
    "-X": "request",
    "--request": "request",
    "-H": "header",
    "--header": "header",
    "-b": "cookie",
    "--cookie": "cookie",
    "-d": "data",
    "--data": "data",
    "--data-raw": "data-raw",
    "--data-binary": "data-binary",
    "--data-urlencode": "data-urlencode",
    "-u": "user",
    "--user": "user",
    "-A": "user-agent",
    "--user-agent": "user-agent",
    "-e": "referer",
    "--referer": "referer",
    "--url": "url",
}
FLAG_OPTS = {"--compressed": "compressed", "-k": "insecure", "--insecure": "insecure",
             "-L": "location", "--location": "location", "-s": None, "--silent": None,
             "-i": None, "--include": None, "-v": None}


class CurlParseError(ValueError):
    pass


def parse_curl(text: str) -> List[Tuple[str, Optional[str]]]:
    """Turn a bash 'Copy as cURL' command into curl config (name, value) pairs."""
    text = re.sub(r"\\\r?\n", " ", text.strip())
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise CurlParseError("cannot split command: %s" % exc)
    if not tokens or tokens[0] not in ("curl", "curl.exe"):
        raise CurlParseError("file must contain one command starting with curl "
                             "(use 'Copy as cURL (bash)', not the cmd/PowerShell variant)")
    pairs: List[Tuple[str, Optional[str]]] = []
    url = None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in ("-F", "--form"):
            raise CurlParseError("multipart upload (-F) cannot be replayed; upload the tar "
                                 "in the web page and copy the evaluate request instead")
        if token in VALUE_OPTS:
            if index + 1 >= len(tokens):
                raise CurlParseError("option %s has no value" % token)
            name = VALUE_OPTS[token]
            value = tokens[index + 1]
            if name == "url":
                url = value
            else:
                pairs.append((name, value))
            index += 2
            continue
        if token in FLAG_OPTS:
            if FLAG_OPTS[token]:
                pairs.append((FLAG_OPTS[token], None))
            index += 1
            continue
        if token.startswith("-"):
            raise CurlParseError("unsupported curl option %s" % token)
        if url is not None:
            raise CurlParseError("more than one URL in the command")
        url = token
        index += 1
    if not url or not re.match(r"https?://", url):
        raise CurlParseError("no http(s) URL found")
    return [("url", url)] + pairs


def _quote(value: str) -> str:
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return '"%s"' % escaped


def to_config(pairs: List[Tuple[str, Optional[str]]]) -> str:
    lines = []
    for name, value in pairs:
        lines.append(name if value is None else "%s = %s" % (name, _quote(value)))
    return "\n".join(lines) + "\n"


def describe(pairs: List[Tuple[str, Optional[str]]]) -> str:
    """Safe summary: method, URL without query, header names only."""
    url = next(value for name, value in pairs if name == "url") or ""
    method = next((value for name, value in pairs if name == "request"), None)
    has_body = any(name.startswith("data") for name, _ in pairs)
    method = method or ("POST" if has_body else "GET")
    headers = [value.split(":", 1)[0].strip() for name, value in pairs
               if name == "header" and value]
    if any(name == "cookie" for name, _ in pairs):
        headers.append("Cookie")
    return "%s %s\nheaders: %s\nbody: %s" % (
        method, url.split("?", 1)[0], ", ".join(headers) or "-", "yes" if has_body else "no")


def redact(text: str, limit: int = 300) -> str:
    text = SENSITIVE_KEY.sub(lambda m: m.group(1) + '"***"', text)
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer ***", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def classify(status: int, body: str, ok_re: str = DEFAULT_OK,
             busy_re: str = DEFAULT_BUSY) -> str:
    """Return success / busy / auth / stop for one response."""
    if status in (401, 403):
        return "auth"
    if status in (409, 423, 429) or 500 <= status < 600 or status == 0:
        return "busy"
    if 200 <= status < 300:
        ok = re.search(ok_re, body) is not None
        busy = re.search(busy_re, body, re.IGNORECASE) is not None
        if ok and not busy:
            return "success"
        if busy and not ok:
            return "busy"
        # Neither, or both (e.g. "evaluation eval-... is already running"): ambiguous.
        return "stop"
    if 400 <= status < 500 and re.search(busy_re, body, re.IGNORECASE):
        return "busy"
    return "stop"


def next_wait(interval: float, retry_after: Optional[str], rng: random.Random) -> float:
    base = max(MIN_INTERVAL, interval)
    if retry_after and retry_after.strip().isdigit():
        base = max(base, float(retry_after.strip()))
    return base * rng.uniform(0.9, 1.2)


def send(config: str, timeout: float) -> Tuple[int, Dict[str, str], str]:
    marker = "\n__SUBMIT_EVAL_STATUS__%{http_code}"
    cmd = ["curl", "-sS", "-K", "-", "-D", "-", "--max-time", str(int(timeout)),
           "-w", marker]
    proc = subprocess.run(cmd, input=config.encode("utf-8"), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout + 15)
    out = proc.stdout.decode("utf-8", "replace")
    text, _, code = out.rpartition("__SUBMIT_EVAL_STATUS__")
    status = int(code) if code.strip().isdigit() else 0
    headers: Dict[str, str] = {}
    body = text
    blocks = re.split(r"\r?\n\r?\n", text)
    while blocks and re.match(r"HTTP/\S+ \d{3}", blocks[0]):
        headers = {}
        for line in blocks.pop(0).splitlines()[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        body = "\n\n".join(blocks)
    if proc.returncode and not body.strip():
        body = proc.stderr.decode("utf-8", "replace")
    return status, headers, body


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("curl_file", help="file holding the copied 'Copy as cURL (bash)' command")
    parser.add_argument("--interval", type=float, default=60.0,
                        help="seconds between attempts (minimum %d)" % MIN_INTERVAL)
    parser.add_argument("--max-minutes", type=float, default=120.0)
    parser.add_argument("--timeout", type=float, default=60.0, help="per request seconds")
    parser.add_argument("--ok-pattern", default=DEFAULT_OK,
                        help="regex in a 2xx body that means the evaluation started")
    parser.add_argument("--busy-pattern", default=DEFAULT_BUSY,
                        help="regex in a body that means 'not now, retry later'")
    parser.add_argument("--once", action="store_true", help="send a single attempt")
    parser.add_argument("--dry-run", action="store_true", help="parse only, send nothing")
    args = parser.parse_args(argv)

    try:
        with open(args.curl_file, encoding="utf-8") as handle:
            pairs = parse_curl(handle.read())
    except (OSError, CurlParseError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    print(describe(pairs), file=sys.stderr)
    if args.dry_run:
        return 0

    config = to_config(pairs)
    rng = random.Random()
    deadline = time.monotonic() + args.max_minutes * 60
    attempt = 0
    while True:
        attempt += 1
        stamp = time.strftime("%H:%M:%S")
        try:
            status, headers, body = send(config, args.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            status, headers, body = 0, {}, "transport error: %s" % exc
        verdict = classify(status, body, args.ok_pattern, args.busy_pattern)
        print("[%s] #%d HTTP %s -> %s | %s" % (stamp, attempt, status or "-", verdict,
                                               redact(body)), file=sys.stderr)
        if verdict == "success":
            match = re.search(args.ok_pattern, body)
            print(match.group(0) if match else "started")
            return 0
        if verdict == "auth":
            print("login expired or no permission: copy a fresh request from the browser",
                  file=sys.stderr)
            return 3
        if verdict == "stop":
            print("unrecognised response; stopped instead of replaying a side-effect request. "
                  "If this means success, pass --ok-pattern; if it means busy, "
                  "pass --busy-pattern.", file=sys.stderr)
            return 4
        if args.once:
            return 5
        wait = next_wait(args.interval, headers.get("retry-after"), rng)
        if time.monotonic() + wait > deadline:
            print("gave up after %.0f minutes" % args.max_minutes, file=sys.stderr)
            return 5
        time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main())
