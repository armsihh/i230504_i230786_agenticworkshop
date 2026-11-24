"""Run tests.json (10 tests + 5 attacks) against a live DoubleAAds API and report pass rate, latency and cost.

    python benchmark/run_benchmark.py                       # live Render API
    python benchmark/run_benchmark.py --api http://127.0.0.1:8000

Standard library only. Every run uses fresh user ids, so runs never affect each other.
Writes benchmark/results.json and benchmark/RESULTS.md.
"""
import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRICE_IN, PRICE_OUT = 0.10 / 1e6, 0.50 / 1e6   # claude-haiku-5-5, USD per token


def post_chat(api: str, body: dict, timeout: int = 120) -> tuple[int, dict, float]:
    req = urllib.request.Request(f"{api}/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, data = r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            data = json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            data = {}
    return status, data, time.perf_counter() - start


def check(expect: dict, status: int, data: dict) -> list[str]:
    """Return a list of failed checks (empty = pass)."""
    fails = []
    reply = data.get("reply", "") or ""
    tools = [t.get("name") for t in data.get("tool_calls", [])]
    tokens = data.get("tokens", {})
    if not {"reply", "tool_calls", "tokens"} <= set(data):
        fails.append("response is not in the graded format")
    if "status" in expect and status != expect["status"]:
        fails.append(f"status {status} != {expect['status']}")
    for t in expect.get("tools_include", []):
        if t not in tools:
            fails.append(f"missing tool {t}")
    excl = expect.get("tools_exclude", [])
    if "*" in excl and tools:
        fails.append(f"expected no tools, got {tools}")
    for t in excl:
        if t != "*" and t in tools:
            fails.append(f"unexpected tool {t}")
    if expect.get("reply_contains_any") and not any(s.lower() in reply.lower() for s in expect["reply_contains_any"]):
        fails.append(f"reply has none of {expect['reply_contains_any']}")
    for s in expect.get("reply_not_contains", []):
        if s.lower() in reply.lower():
            fails.append(f"reply contains forbidden '{s}'")
    if expect.get("zero_tokens") and (tokens.get("in") or tokens.get("out")):
        fails.append(f"expected 0 tokens, used {tokens}")
    return fails


def run_case(api: str, case: dict, run_id: str) -> dict:
    steps = []
    for step in case["steps"]:
        body = {k: step[k].replace("{run}", run_id) for k in ("user_id", "thread_id", "message")}
        status, data, secs = post_chat(api, body)
        if status == 429:                                   # rate limit: wait once and retry
            time.sleep(61)
            status, data, secs = post_chat(api, body)
        fails = check(step.get("expect", {}), status, data)
        steps.append({"message": body["message"][:80], "status": status, "latency_s": round(secs, 2),
                      "tokens": data.get("tokens", {}), "tools": [t.get("name") for t in data.get("tool_calls", [])],
                      "reply": (data.get("reply") or "")[:300], "fails": fails})
        mark = "ok " if not fails else "FAIL"
        print(f"  [{mark}] {case['id']} {secs:5.1f}s  tools={steps[-1]['tools']}  {'; '.join(fails)}")
    return {"id": case["id"], "name": case["name"], "category": case["category"],
            "passed": all(not s["fails"] for s in steps), "steps": steps}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="https://doubleaads-api.onrender.com")
    ap.add_argument("--tests", default=str(ROOT / "tests.json"))
    args = ap.parse_args()
    api = args.api.rstrip("/")
    spec = json.loads(Path(args.tests).read_text(encoding="utf-8"))
    run_id = uuid.uuid4().hex[:6]

    print(f"Waking {api} ...")
    for _ in range(10):   # a free Render instance may be asleep (about a minute to wake)
        try:
            with urllib.request.urlopen(f"{api}/health", timeout=90) as r:
                if r.read().decode().strip() == "ok":
                    break
        except Exception:
            time.sleep(6)
    else:
        sys.exit("API did not answer /health")

    results = {"tests": [], "attacks": []}
    for group in ("tests", "attacks"):
        print(f"\n{group.upper()}")
        for case in spec[group]:
            results[group].append(run_case(api, case, run_id))

    steps = [s for g in results.values() for c in g for s in c["steps"] if s["status"] == 200]
    lat = sorted(s["latency_s"] for s in steps)
    model_steps = [s for s in steps if s["tokens"].get("in")]
    tok_in = sum(s["tokens"].get("in", 0) for s in steps)
    tok_out = sum(s["tokens"].get("out", 0) for s in steps)
    summary = {
        "api": api, "run_id": run_id, "at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "tests_passed": sum(c["passed"] for c in results["tests"]), "tests_total": len(results["tests"]),
        "attacks_passed": sum(c["passed"] for c in results["attacks"]), "attacks_total": len(results["attacks"]),
        "requests": len(steps), "latency_avg_s": round(statistics.mean(lat), 2) if lat else None,
        "latency_p50_s": round(statistics.median(lat), 2) if lat else None,
        "latency_p95_s": round(lat[max(0, int(len(lat) * 0.95) - 1)], 2) if lat else None,
        "tokens_in": tok_in, "tokens_out": tok_out,
        "avg_tokens_per_model_turn": {"in": round(tok_in / max(1, len(model_steps))),
                                      "out": round(tok_out / max(1, len(model_steps)))},
        "zero_token_requests": len(steps) - len(model_steps),
        "cost_usd": round(tok_in * PRICE_IN + tok_out * PRICE_OUT, 4),
    }
    out = {"summary": summary, **results}
    (ROOT / "benchmark" / "results.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    md = render_md(out)
    (ROOT / "benchmark" / "RESULTS.md").write_text(md, encoding="utf-8")
    readme = ROOT / "README.md"   # keep the README's table in step with the latest run
    text = readme.read_text(encoding="utf-8")
    start, end = "<!-- BENCHMARK:START -->", "<!-- BENCHMARK:END -->"
    if start in text and end in text:
        body = md.split("\n", 2)[2].strip()   # drop the "# Benchmark results" heading
        text = text[:text.index(start) + len(start)] + "\n" + body + "\n" + text[text.index(end):]
        readme.write_text(text, encoding="utf-8")
    print(f"\nTests {summary['tests_passed']}/{summary['tests_total']} · attacks {summary['attacks_passed']}/"
          f"{summary['attacks_total']} · avg {summary['latency_avg_s']}s · p95 {summary['latency_p95_s']}s · "
          f"${summary['cost_usd']}")


def render_md(out: dict) -> str:
    s = out["summary"]
    rows = []
    for group in ("tests", "attacks"):
        for c in out[group]:
            lat = sum(st["latency_s"] for st in c["steps"])
            tin = sum(st["tokens"].get("in", 0) for st in c["steps"])
            tout = sum(st["tokens"].get("out", 0) for st in c["steps"])
            tools = ", ".join(sorted({t for st in c["steps"] for t in st["tools"] if not t.startswith("transfer_")})) or "–"
            rows.append(f"| {c['id']} | {c['category'].replace('_', ' ')} | {c['name']} | "
                        f"{'✅ pass' if c['passed'] else '❌ fail'} | {len(c['steps'])} | {lat:.1f} s | {tin:,} / {tout:,} | {tools} |")
    return (f"# Benchmark results\n\nRun `{s['run_id']}` against `{s['api']}` on {s['at']}.\n\n"
            f"| Metric | Value |\n|---|---|\n"
            f"| Own tests passed | **{s['tests_passed']}/{s['tests_total']}** |\n"
            f"| Attack tests passed | **{s['attacks_passed']}/{s['attacks_total']}** |\n"
            f"| Requests | {s['requests']} ({s['zero_token_requests']} answered at 0 tokens) |\n"
            f"| Latency avg / p50 / p95 | {s['latency_avg_s']} s / {s['latency_p50_s']} s / {s['latency_p95_s']} s |\n"
            f"| Avg tokens per model turn (in / out) | {s['avg_tokens_per_model_turn']['in']:,} / "
            f"{s['avg_tokens_per_model_turn']['out']:,} |\n"
            f"| Total tokens (in / out) | {s['tokens_in']:,} / {s['tokens_out']:,} |\n"
            f"| Cost of the whole run | ${s['cost_usd']} |\n\n"
            "| ID | Category | Test | Result | Steps | Time | Tokens in / out | Tools used |\n"
            "|---|---|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n")


if __name__ == "__main__":
    main()
