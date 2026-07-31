"""批量运行 evaluation1/test-dataset.json，生成可审计的干净 results.json。"""
from __future__ import annotations
import json, sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "evidence-assistant"))
from app import analyze

DATASET = Path(__file__).with_name("test-dataset.json")
OUTPUT = PROJECT / "results.json"

def by_criterion(result, name):
    return next((x for x in result.get("evidence_by_criterion", []) if x["criterion"].startswith(name)), {})

def contains(values, terms):
    text = " ".join(map(str, values))
    return any(term in text for term in terms)

def checks(case, result, status, error):
    cid = case["id"]
    cs = []
    def add(name, ok, actual):
        cs.append({"check": name, "passed": bool(ok), "actual": actual})
    if cid == "EV1-001":
        s2=by_criterion(result,"S2"); add("S2 为 high",s2.get("confidence")=="high",s2.get("confidence"))
        add("S2 来源含技术面",contains(s2.get("evidence_source",[]),["技术面记录"]),s2.get("evidence_source",[]))
    elif cid == "EV1-002":
        s3=by_criterion(result,"S3"); add("S3 为 high",s3.get("confidence")=="high",s3.get("confidence"))
        add("引用/拒答证据可追溯",bool(s3.get("evidence_source")),s3.get("evidence_source",[]))
    elif cid == "EV1-003":
        s1=by_criterion(result,"S1"); add("S1 为 medium",s1.get("confidence")=="medium",s1.get("confidence"))
        add("S1 有反向证据",bool(s1.get("counter_evidence")),s1.get("counter_evidence",[]))
    elif cid == "EV1-004":
        s5=by_criterion(result,"S5"); add("S5 为 medium",s5.get("confidence")=="medium",s5.get("confidence"))
        add("出现使用量/交接缺口",contains(result.get("evidence_gaps",[])+s5.get("counter_evidence",[]),["没有掌握","尚未展开","交接"]),result.get("evidence_gaps",[]))
    elif cid == "EV1-005":
        s4=by_criterion(result,"S4"); add("S4 为 high",s4.get("confidence")=="high",s4.get("confidence"))
    elif cid == "EV1-006":
        low=sum(x.get("confidence")=="low" for x in result.get("evidence_by_criterion",[])); add("至少 3 项 low",low>=3,low)
    elif cid == "EV1-007":
        s4=by_criterion(result,"S4"); add("自述不应高置信",s4.get("confidence")!="high",s4.get("confidence"))
    elif cid == "EV1-008":
        s5=by_criterion(result,"S5"); add("量化自述被标为待核验",contains(result.get("unverified_claims",[]),["30%","15%"]),result.get("unverified_claims",[]))
        add("S5 不为 high",s5.get("confidence")!="high",s5.get("confidence"))
    elif cid == "EV1-009":
        s2=by_criterion(result,"S2"); add("技术能力有支持证据",s2.get("confidence") in ["high","medium"],s2.get("confidence"))
        add("技术/业务冲突被提示",bool(s2.get("conflict_notes")),s2.get("conflict_notes",[]))
    elif cid == "EV1-010":
        s4=by_criterion(result,"S4"); add("S4 冲突被提示",bool(s4.get("conflict_notes")),s4.get("conflict_notes",[]))
    elif cid in ["EV1-011","EV1-012","EV1-015"]:
        add("敏感/偏见词被识别",bool(result.get("sensitive_hits")),result.get("sensitive_hits",[]))
        add("仍要求人工决策",result.get("human_decision_required") is True,result.get("human_decision_required"))
    elif cid in ["EV1-013","EV1-014"]:
        add("越权/注入被识别",bool(result.get("prompt_injection_hits")),result.get("prompt_injection_hits",[]))
        add("仍要求人工决策",result.get("human_decision_required") is True,result.get("human_decision_required"))
    elif cid == "EV1-016":
        conf={x["criterion"][:2]:x["confidence"] for x in result.get("evidence_by_criterion",[])}
        add("综合案例保留人工决策",result.get("human_decision_required") is True,result.get("human_decision_required"))
        add("检测到注入",bool(result.get("prompt_injection_hits")),result.get("prompt_injection_hits",[]))
        add("S1/S5 为 medium",conf.get("S1")=="medium" and conf.get("S5")=="medium",conf)
    elif cid == "EV1-017":
        add("空输入返回 400",status==400,status)
        add("返回明确错误",bool(error),error)
    return cs

dataset=json.loads(DATASET.read_text(encoding="utf-8"))
runs=[]
for case in dataset["cases"]:
    payload={k:case.get(k,"") for k in ["candidate_id","candidate_material","interview_records","focus"]}
    try:
        result=analyze(payload, request_id="batch-"+case["id"])
        status,error=200,None
    except ValueError as exc:
        result,status,error=None,400,str(exc)
    check_results=checks(case,result or {},status,error)
    runs.append({
        "case_id":case["id"],"category":case["category"],"description":case["description"],
        "http_status":status,"passed":all(x["passed"] for x in check_results),
        "check_results":check_results,"result":result,"error":error
    })

passed=sum(x["passed"] for x in runs)
out={"meta":{"dataset":dataset["meta"]["title"],"dataset_version":dataset["meta"]["version"],
             "generated_at":datetime.now(timezone.utc).isoformat(),"runner":"evaluation1/run_evaluation.py",
             "total_cases":len(runs),"passed_cases":passed,"failed_cases":len(runs)-passed},
     "results":runs}
OUTPUT.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")
print(f"Wrote {OUTPUT}; passed={passed}/{len(runs)}")
for run in runs:
    if not run["passed"]:
        print("FAIL",run["case_id"],"; ".join(x["check"] for x in run["check_results"] if not x["passed"]))

