"""Run inside the official image with /fixtures and writable /output mounts.

The HTTP model stand-in checks real SDK/protocol behavior, not answer quality.
Fixtures: papers in /data/论文库 and /output/data/papers (same content), reports.json
in /fixtures with 'all' and 'selected' Markdown test answers. No paid calls.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

calls = []
reports = json.loads(Path('/fixtures/reports.json').read_text())
names = [p.name for p in sorted(Path('/data/论文库').glob('*.pdf'))]
selected = names[-5:]
assert len(names) == 8


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        system = data['messages'][0]['content']
        prompt = data['messages'][-1]['content']
        if 'INVALID_GATEWAY_PROBE' in prompt:
            calls.append('upstream_failure')
            self.send_response(503)
            self.end_headers()
            return
        if '核验相关性' in system:
            calls.append('selection')
            assert all(name in prompt for name in names)
            content = json.dumps({'papers': [{'filename': name, 'reason': '原文比较绿地覆盖、遥感数据与城市地表温度。'}
                                             for name in selected]}, ensure_ascii=False)
        elif '综述作者' in system or '科研方案设计者' in system:
            calls.append('survey' if '综述作者' in system else 'idea')
            assert 'p.10' in prompt
            content = reports['all' if names[0] in prompt else 'selected']
        else:
            calls.append('retrieval')
            batch = re.findall(r'^\d+\. (\S+\.pdf)', prompt, re.M)
            assert len(batch) == 8
            content = json.dumps({'papers': [{'filename': name, 'reason': '原文研究城市绿地覆盖与遥感地表温度的联系。'}
                                             for name in batch]}, ensure_ascii=False)
        response = json.dumps({'id': 'local-fixture', 'object': 'chat.completion', 'created': 0,
                               'model': 'smoke-model', 'choices': [{'index': 0, 'finish_reason': 'stop',
                               'message': {'role': 'assistant', 'content': content}}],
                               'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def main():
    import agenthack  # fail if the stand-in compatibility entrypoint is used
    from core.schemas import validate_answer
    server = ThreadingHTTPServer(('127.0.0.1', 19001), Gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = dict(os.environ, PLATFORM_LLM_URL='http://127.0.0.1:19001/v1', PLATFORM_API_KEY='local-fixture-only',
               AGENT_MODEL_NAME='smoke-model', RESEARCH_WORKSPACE_TEAM='test-team',
               AGENTHACK_FLUSH_ON_SOLVE='false', CONTEST_WORKSPACE_ROOT='/output')
    log = open('/output/recovery-smoke.log', 'w')
    process = subprocess.Popen([sys.executable, '/app/agent.py'], env=env, stdout=log, stderr=log)
    results = []
    baseline = os.environ.get('SMOKE_BASELINE') == '1'
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError('agent exited; inspect recovery-smoke.log')
            try:
                with urlopen('http://127.0.0.1:8080/health', timeout=2) as response:
                    assert response.status == 200
                break
            except (URLError, TimeoutError):
                time.sleep(.1)
        else:
            raise RuntimeError('agent startup timed out')
        concrete = ['data/论文库/' + name for name in names] + ['data/论文库/conditions.csv']
        cases = [('B-90', 'Literature_Retrieval', concrete),
                 ('B-91', 'Literature_Retrieval', ['本地论文文件夹内的 PDF']),
                 ('C-90', 'Survey', concrete),
                 ('C-91', 'Survey', ['本地论文文件夹内指定的城市气候 PDF']),
                 ('E-90', 'Idea', concrete),
                 ('E-91', 'Idea', ['本题指定的城市气候与步行规划论文 PDF']),
                 ('C-92', 'Survey', ['data/论文库/missing.pdf']),
                 ('B-92', 'Literature_Retrieval', concrete)]
        for case_id, function, materials in cases:
            question = {'case_id': case_id, 'function': function, 'input': 'urban green surface temperature remote sensing',
                        'materials': materials, 'constraints': [], 'output_schema_ref': function}
            if case_id == 'B-92':
                question['input'] = 'INVALID_GATEWAY_PROBE'
            before = len(calls)
            request = Request('http://127.0.0.1:8080/solve', data=json.dumps(question).encode(),
                              headers={'Content-Type': 'application/json'}, method='POST')
            try:
                with urlopen(request, timeout=60) as response:
                    payload = json.load(response)
                    status = response.status
            except HTTPError as error:
                status, payload = error.code, None
            expected_failure = case_id in ('C-92', 'B-92')
            if baseline:
                results.append({'case_id': case_id, 'status': status, 'model_calls': len(calls) - before,
                                'papers': len(payload['papers']) if payload and 'papers' in payload else None})
                continue
            assert status == (503 if expected_failure else 200), (case_id, status)
            if expected_failure:
                assert not Path('/output/data/test-team', case_id + '.json').exists()
                if case_id == 'C-92':
                    assert len(calls) == before
            else:
                validate_answer(function, payload)
                saved = json.loads(Path('/output/data/test-team', case_id + '.json').read_text())
                assert saved == payload
                if function == 'Literature_Retrieval':
                    assert len(payload['papers']) == 8
                else:
                    assert Path('/output', payload['path']).read_text() == payload['markdown']
                    if case_id in ('C-91', 'E-91'):
                        assert names[0] not in payload['markdown']
            results.append({'case_id': case_id, 'status': status, 'model_calls': len(calls) - before})
        result = {'scope': 'official SDK, offline loopback stand-in, protocol and filesystem only',
                  'baseline_observation': baseline, 'python': sys.version.split()[0],
                  'uid': os.getuid(), 'sdk': agenthack.__file__, 'cases': results}
        Path('/output/recovery-smoke.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))
    finally:
        process.terminate()
        process.wait(timeout=10)
        server.shutdown()
        log.close()


if __name__ == '__main__':
    main()
