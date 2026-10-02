"""답변 묶음을 JSON으로 내보낼 형태로 만듭니다 (CLI `ask --json`과 HTTP API 공통)."""

from __future__ import annotations

from typing import Any

from infra_agent.orchestration.runner import AnswerBundle


def answer_payload(bundle: AnswerBundle) -> dict[str, Any]:
    return {
        "request_id": bundle.context.request_id,
        "intent": bundle.context.intent.value,
        "assumptions": list(bundle.interpretation.assumptions),
        "interpretation_method": bundle.interpretation.method,
        "interpretation_note": bundle.interpretation.method_note,
        "llm": {
            "provider": bundle.llm_name,
            "calls": bundle.llm_calls,
            "data_policy": bundle.data_policy,
        },
        "answer": bundle.answer.model_dump(mode="json"),
        "execution": [
            {
                "task_id": r.task_id,
                "agent": r.agent,
                "depends_on": list(r.depends_on),
                "status": r.status.value,
                "elapsed_ms": r.elapsed_ms,
                "reason": r.reason,
            }
            for r in bundle.runs
        ],
        "rejected_hypotheses": [
            {"agent": r.agent.value, **x.model_dump(mode="json")}
            for r in bundle.results
            for x in r.rejected_hypotheses
        ],
    }
