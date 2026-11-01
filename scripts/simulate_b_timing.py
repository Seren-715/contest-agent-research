"""B end-to-end timing simulation: real PDFs, the real pipeline, a fake model.

Usage: python scripts/simulate_b_timing.py SIM_DIR LATENCY_FACTOR TAG
SIM_DIR/data_ro/论文库/ holds the PDF library (any PDFs; nothing is downloaded).
The fake model only sleeps for a modelled latency and returns well-formed JSON;
no gateway is called. Writes SIM_DIR/out_TAG/ and SIM_DIR/log_TAG.json.
Timing is an estimate for this machine and latency model, not a platform result.
"""
import json, os, re, sys, threading, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
SIM = Path(sys.argv[1]); FACTOR = float(sys.argv[2]); TAG = sys.argv[3]
os.environ["DATA_PATH"] = str(SIM / "data_ro")
os.environ["CONTEST_FAILURE_TIMING"] = "0"

import core.diagnostics as diagnostics
import core.solve as solve
import handlers.b_retrieval as b
from core.config import Settings
from core.execution import ExecutionControl, current_execution, execution_context
from core.llm import ModelError

T0 = [0.0]
LOG = []

def log(event, **kw):
    LOG.append({"t": round(time.monotonic() - T0[0], 1), "event": event, **kw})
    print(f"[{LOG[-1]['t']:6.1f}s] {event} {kw}", flush=True)

orig_record = diagnostics.record_stage
def timed_record(settings, case_id, phase, **details):
    log("stage", phase=phase, **{k: v for k, v in details.items() if k != "warning_counts"})
    return orig_record(settings, case_id, phase, **details)
for module in (b, solve):
    module.record_stage = timed_record

NAME = re.compile(r"^\d+\. (.+?\.pdf)", re.M)

class FakeDeepSeek:
    """Latency = (6 s + 1.5 ms per input token + output tokens / 40 tok/s) * FACTOR.

    Calibrated loosely on platform readings (C: ~58 s per ~16k-token call; A:
    ~15 s per tiny call). Output size is modelled per call type.
    """
    def complete(self, messages, temperature=0.2, max_tokens=None, timeout=None):
        prompt = messages[-1]["content"]
        in_tokens = sum(len(m["content"]) for m in messages) / 3.0
        names = NAME.findall(prompt)
        if "keywords" in prompt:
            kind, out = "expand", 500
            reply = json.dumps({"keywords": ["speculative decoding", "LLM serving", "latency", "throughput",
                                             "draft model", "verification", "batching", "inference"]})
        elif "宽松初筛" in prompt:
            kind, out = "screen", 1500 + 25 * len(names)
            reply = json.dumps({"papers": [{"filename": n} for n in names[::3]]})
        else:
            kind, out = "rerank", 1200 + 60 * len(names)
            reply = json.dumps({"papers": [{"filename": n, "reason": "模拟理由：主题相关。"} for n in names[::4]]},
                               ensure_ascii=False)
        latency = (6 + 0.0015 * in_tokens + out / 40) * FACTOR
        log("call_start", kind=kind, candidates=len(names), in_tokens=int(in_tokens),
            latency=round(latency, 1), timeout=None if timeout is None else round(timeout, 1))
        limit = latency if timeout is None else min(latency, timeout)
        end = time.monotonic() + limit
        control = current_execution()
        while time.monotonic() < end:
            if control is not None:
                control.check()
            time.sleep(0.5)
        if timeout is not None and latency > timeout:
            log("call_timeout", kind=kind)
            raise ModelError("simulated request timeout")
        log("call_end", kind=kind)
        return reply

out = SIM / f"out_{TAG}"
settings = Settings(team="sim-team", workspace_root=out, data_dir=out / "data", team_dir=out / "data" / "sim-team",
                    expert_dir=out / "data" / "expert", figure_dir=out / "data" / "figure" / "sim-team",
                    llm_base_url=None, llm_api_key=None, llm_model=None, mock_llm=False,
                    request_timeout=300.0, case_budget=900.0,
                    d_client=out / "run_experiment.py", d_materials_root=out / "experiments")
solve.model_client = lambda s: FakeDeepSeek()
question = {"case_id": "B-01", "function": "Literature_Retrieval",
            "input": "检索与大语言模型推理服务中的推测解码（speculative decoding）相关的论文，关注延迟与吞吐。",
            "materials": ["本地论文库中的全部论文"], "constraints": [], "output_schema_ref": "Literature_Retrieval"}
result = {}
def run():
    control = ExecutionControl(seconds=540)
    with execution_context(control):
        try:
            result["answer"] = solve.solve_task(question, settings)
        except BaseException as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
T0[0] = time.monotonic()
thread = threading.Thread(target=run); thread.start(); thread.join(600)
total = time.monotonic() - T0[0]
papers = result.get("answer", {}).get("papers", [])
print(json.dumps({"tag": TAG, "factor": FACTOR, "total_seconds": round(total, 1), "still_running": thread.is_alive(),
                  "error": result.get("error"), "returned_papers": len(papers)}, ensure_ascii=False))
(SIM / f"log_{TAG}.json").write_text(json.dumps(LOG, ensure_ascii=False, indent=1))
