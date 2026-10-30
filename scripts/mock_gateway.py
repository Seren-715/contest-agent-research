"""Local OpenAI-compatible mock gateway for integration testing.

It is intentionally tiny: enough for the official AgentHack base image to make a
real OpenAI chat.completions call. It never sees or stores a real API key.
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _paper_names(prompt: str) -> list[str]:
    names: list[str] = []
    for match in re.finditer(r"完整文件名：([^\n\\]+)", prompt):
        name = match.group(1).strip()
        if name.lower().endswith(".pdf") and name not in names:
            names.append(name)
    return names


def _report(names: list[str], idea: bool) -> str:
    title = "# 研究 Idea 报告\n\n" if idea else "# 文献综述\n\n"
    body = title + ("本文比较指定论文的研究问题、方法、数据、指标和结论。" * 30) + "\n"
    for index, name in enumerate(names, start=1):
        body += f"\n论文 [{index}] 用于说明材料范围和核验维度。\n"
    references = "\n## 参考文献\n" + "\n".join(
        f"[{index}] {name}" for index, name in enumerate(names, start=1)
    )
    return body + references


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/models"):
            data = json.dumps({"object": "list", "data": [{"id": "dsv4", "object": "model"}]}).encode()
            self._send(200, "application/json", data)
            return
        self._send(404, "application/json", b'{"error":"not found"}')

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            request = json.loads(raw.decode("utf-8"))
        except Exception:
            request = {}
        prompt = json.dumps(request, ensure_ascii=False)
        names = _paper_names(prompt)
        if "英文关键词" in prompt or "keywords" in prompt:
            content = json.dumps(
                {"keywords": ["urban", "green", "surface", "temperature", "remote", "sensing"]},
                ensure_ascii=False,
            )
        elif "综述任务" in prompt or "请写一篇至少 800" in prompt:
            content = _report(names, idea=False)
        elif "研究灵感任务" in prompt:
            content = _report(names, idea=True)
        elif "候选论文" in prompt:
            candidates = re.findall(r"\d+\.\s+([^\\\s]+\.pdf)", prompt)
            papers = [{"filename": candidates[0], "reason": "mock 命中"}] if candidates else []
            content = json.dumps({"papers": papers}, ensure_ascii=False)
        else:
            content = json.dumps(
                {
                    "answer": "来自本地 mock 网关的回答。",
                    "clarifying_question": "你想研究哪一种植物、哪个生长阶段？",
                },
                ensure_ascii=False,
            )
        response = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 0,
            "model": request.get("model", "dsv4"),
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        data = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self._send(200, "application/json", data)

    def _send(self, status: int, content_type: str, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18090)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"mock gateway listening on 0.0.0.0:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
