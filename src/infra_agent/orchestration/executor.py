"""실행 계획 실행기 (architecture.md 8절).

- 선행 작업이 없는 작업은 병렬로 실행하되, 동시에 실행되는 에이전트 수를 제한합니다.
- 선행 작업이 있는 작업은 선행 작업이 끝난 뒤 그 결과(`upstream`)와 함께 실행합니다.
- 제한 시간: 에이전트별 제한 시간과 요청 마감 시각 중 먼저 오는 것을 적용합니다.
- 부분 실패: 에이전트 예외·타임아웃은 `failed`, 선행 작업이 실패했거나 마감이 지난 작업은
  `skipped`로 기록하고 나머지 결과는 그대로 종합에 넘깁니다. 오류 메시지는 마스킹합니다.
  단, 선행 결과 없이도 분석할 수 있는 에이전트(`plan.optional_upstream`)는 선행 작업이 실패해도
  실행합니다.
- 재시도: 데이터 소스 일시 오류는 각 클라이언트가 재시도합니다. 에이전트 단위 재시도는 하지 않습니다
  (같은 조회·모델 호출을 반복해 예산을 두 번 쓰고, 타임아웃의 대부분은 재시도로 해결되지 않기 때문).
- 조회 예산·모델 호출 예산은 에이전트 생성 시 요청 단위 객체를 공유해 적용합니다.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from infra_agent.agents.base import Agent, agent_deadline
from infra_agent.orchestration.plan import ExecutionPlan
from infra_agent.schemas import (
    AgentName,
    AgentResult,
    AgentStatus,
    AgentTask,
    AnalysisContext,
    ErrorInfo,
)
from infra_agent.security import redact
from infra_agent.timeutil import utc_now

_BLOCKING = (AgentStatus.FAILED, AgentStatus.SKIPPED)


@dataclass(frozen=True)
class TaskRun:
    """작업 하나의 실행 기록 (답변의 실행 정보, `--json` 출력용)."""

    task_id: str
    agent: str
    depends_on: tuple[str, ...]
    status: AgentStatus
    elapsed_ms: int
    reason: str | None = None


@dataclass(frozen=True)
class ExecutionReport:
    results: tuple[AgentResult, ...]
    """계획 순서대로 정렬한 결과."""
    runs: tuple[TaskRun, ...]
    peak_concurrency: int = 0


def _not_run(task: AgentTask, status: AgentStatus, code: str, message: str) -> AgentResult:
    return AgentResult(
        task_id=task.task_id,
        agent=task.agent,
        status=status,
        errors=(ErrorInfo(code=code, message=redact(message)),),
    )


class Executor:
    def __init__(
        self,
        *,
        max_concurrency: int,
        agent_timeout_seconds: float,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency는 1 이상이어야 합니다")
        self._max = max_concurrency
        self._agent_timeout = agent_timeout_seconds
        self._clock = clock

    async def run(
        self, plan: ExecutionPlan, agents: Mapping[AgentName, Agent], ctx: AnalysisContext
    ) -> ExecutionReport:
        loop = asyncio.get_running_loop()
        done: dict[str, asyncio.Future[AgentResult]] = {
            t.task_id: loop.create_future() for t in plan.tasks
        }
        runs: dict[str, TaskRun] = {}
        semaphore = asyncio.Semaphore(self._max)
        active = 0
        peak = 0

        async def execute(task: AgentTask) -> None:
            nonlocal active, peak
            started = time.monotonic()
            result: AgentResult | None = None
            try:
                upstream = {dep: await done[dep] for dep in task.depends_on}
                started = time.monotonic()  # 선행 작업 대기 시간은 제외
                blocked = (
                    []
                    if task.agent in plan.optional_upstream
                    else [d for d, r in upstream.items() if r.status in _BLOCKING]
                )
                agent = agents.get(task.agent)
                if blocked:
                    result = _not_run(
                        task,
                        AgentStatus.SKIPPED,
                        "dependency_not_completed",
                        f"선행 작업({', '.join(blocked)})이 완료되지 않아 실행하지 않음",
                    )
                elif agent is None:
                    result = _not_run(
                        task,
                        AgentStatus.FAILED,
                        "agent_unavailable",
                        f"{task.agent.value} 에이전트를 사용할 수 없음",
                    )
                else:
                    async with semaphore:
                        started = time.monotonic()
                        remaining = (ctx.budget.deadline - self._clock()).total_seconds()
                        if remaining <= 0:
                            result = _not_run(
                                task,
                                AgentStatus.SKIPPED,
                                "request_deadline",
                                "요청 제한 시간이 지나 실행하지 않음",
                            )
                        else:
                            active += 1
                            peak = max(peak, active)
                            try:
                                result = await self._invoke(agent, task, ctx, upstream, remaining)
                            finally:
                                active -= 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 실행기 자체 오류도 요청 전체를 멈추지 않게 함
                result = _not_run(
                    task,
                    AgentStatus.FAILED,
                    "executor_error",
                    f"실행 중 오류({type(exc).__name__}): {exc}",
                )
            finally:
                if result is None:
                    result = _not_run(
                        task, AgentStatus.FAILED, "cancelled", "실행이 취소되어 결과가 없음"
                    )
                elapsed = int((time.monotonic() - started) * 1000)
                result = result.model_copy(
                    update={"usage": result.usage.model_copy(update={"elapsed_ms": elapsed})}
                )
                runs[task.task_id] = TaskRun(
                    task_id=task.task_id,
                    agent=task.agent.value,
                    depends_on=task.depends_on,
                    status=result.status,
                    elapsed_ms=elapsed,
                    reason=result.errors[0].message
                    if result.status in _BLOCKING and result.errors
                    else None,
                )
                if not done[task.task_id].done():
                    done[task.task_id].set_result(result)

        await asyncio.gather(*(execute(t) for t in plan.tasks))
        return ExecutionReport(
            results=tuple(done[t.task_id].result() for t in plan.tasks),
            runs=tuple(runs[t.task_id] for t in plan.tasks),
            peak_concurrency=peak,
        )

    async def _invoke(
        self,
        agent: Agent,
        task: AgentTask,
        ctx: AnalysisContext,
        upstream: Mapping[str, AgentResult],
        remaining: float,
    ) -> AgentResult:
        timeout = min(self._agent_timeout, remaining)
        try:
            with agent_deadline(timeout):  # wait_for가 만드는 작업이 마감 시각을 이어받음
                result = await asyncio.wait_for(agent.run(task, ctx, upstream), timeout=timeout)
        except TimeoutError:
            reason = "요청 제한 시간" if remaining < self._agent_timeout else "에이전트 제한 시간"
            return _not_run(
                task, AgentStatus.FAILED, "agent_timeout", f"{reason}({timeout:.0f}초) 초과"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _not_run(
                task,
                AgentStatus.FAILED,
                "agent_error",
                f"에이전트 오류({type(exc).__name__}): {exc}",
            )
        if result.task_id != task.task_id or result.agent is not task.agent:
            return _not_run(
                task,
                AgentStatus.FAILED,
                "agent_contract",
                "에이전트가 다른 작업의 결과를 반환해 사용하지 않음",
            )
        return result
