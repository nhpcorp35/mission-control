"""Mission Control cloud API."""
from contextlib import asynccontextmanager
from datetime import datetime
import logging
import os
import time
from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from app.auth import require_api_key
from app.cursor_cli import (
    augment_path,
    check_cursor_cli_status,
    preflight_for_execution,
)
from mission_control.executor import (
    execute_cursor_agent,
    run_cursor_agent,
)
from mission_control.recursion import (
    RECURSIVE_SUBMISSION_ERROR,
    execution_scope,
    is_recursive_submission,
)
from mission_control.run_queue import RunQueue
from mission_control.run_registry import (
    RunRecord,
    RunRegistry,
    RunStatus,
    is_terminal_status,
)
from mission_control.run_result import StructuredRunResult
from mission_control.workspace import execute_registered_run
from mission_control.validator import (
    load_mission_yaml,
    validate_mission_for_execute,
    validate_mission_for_run,
)
logger = logging.getLogger(__name__)
run_registry = RunRegistry()
run_queue = RunQueue()

# Bounds for POST /runs/{run_id}/wait (and the MCP wait_for_run tool).
WAIT_MIN_TIMEOUT_SECONDS = 0.1
WAIT_MAX_TIMEOUT_SECONDS = 3600.0
WAIT_MIN_POLL_INTERVAL_SECONDS = 0.05
WAIT_MAX_POLL_INTERVAL_SECONDS = 60.0
WAIT_DEFAULT_TIMEOUT_SECONDS = 300.0
WAIT_DEFAULT_POLL_INTERVAL_SECONDS = 1.0


def _execute_queued_run(run_id: str, mission: dict, registry: RunRegistry) -> None:
    """Run one queued mission with lifecycle logging (no secrets)."""
    count, keys = registry.diagnostic_state()
    logger.info(
        (
            "lifecycle run_id=%s event=started api_pid=%s "
            "registry_id=%s registry_count=%s registry_keys=%s"
        ),
        run_id,
        os.getpid(),
        id(registry),
        count,
        keys,
    )
    with execution_scope():
        try:
            execute_registered_run(run_id, mission, registry)
        except Exception:
            logger.exception(
                (
                    "lifecycle run_id=%s event=exception "
                    "api_pid=%s registry_id=%s stage=queued_run"
                ),
                run_id,
                os.getpid(),
                id(registry),
            )
            raise
        finally:
            record = registry.get_run(run_id)
            status = record.status.value if record is not None else "unknown"
            error = record.error if record is not None else None
            count, keys = registry.diagnostic_state()
            # Log failure presence without dumping full stderr/YAML secrets.
            logger.info(
                (
                    "lifecycle run_id=%s event=finished status=%s has_error=%s "
                    "api_pid=%s registry_id=%s registry_count=%s "
                    "registry_keys=%s"
                ),
                run_id,
                status,
                bool(error),
                os.getpid(),
                id(registry),
                count,
                keys,
            )


run_queue.configure(_execute_queued_run)


@asynccontextmanager
async def lifespan(_: FastAPI):
    os.environ["PATH"] = augment_path()
    status = check_cursor_cli_status()
    logger.info(
        "Cursor CLI startup check: installed=%s authenticated=%s binary=%s",
        status.installed,
        status.authenticated,
        status.binary_path or "not found",
    )
    recovered = run_registry.recover_interrupted_runs()
    if recovered:
        logger.info(
            "Marked %s interrupted run(s) failed on startup",
            recovered,
        )
    yield
app = FastAPI(
    title="Mission Control API",
    version="1.0.0",
    lifespan=lifespan,
)
class MissionYamlRequest(BaseModel):
    mission_yaml: str = Field(..., min_length=1)
class ValidateResponse(BaseModel):
    ok: bool
    error: str | None = None
class ErrorDetail(BaseModel):
    code: str
    message: str
    stage: str
class RunResponse(BaseModel):
    ok: bool
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    error_detail: ErrorDetail | None = None
class RunAcceptedResponse(BaseModel):
    run_id: str
    status: str


class CommandEvidenceModel(BaseModel):
    argv: list[str]
    exit_code: int | None = None
    passed: bool | None = None
    kind: str


class DeliverableEvidenceModel(BaseModel):
    verified: bool
    passed: bool | None = None
    checked_paths: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class PersistenceEvidenceModel(BaseModel):
    mode: str | None = None
    attempted: bool
    ok: bool | None = None
    commit_sha: str | None = None


class StructuredRunResultModel(BaseModel):
    """Objective Mission Control evidence (not agent-authored stdout)."""

    files_changed: list[str] = Field(default_factory=list)
    commands: list[CommandEvidenceModel] = Field(default_factory=list)
    test_counts: dict[str, int] | None = None
    deliverables: DeliverableEvidenceModel | None = None
    persistence: PersistenceEvidenceModel | None = None
    warnings: list[str] = Field(default_factory=list)


class RunStatusResponse(BaseModel):
    run_id: str
    status: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    elapsed_seconds: float | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    return_code: int | None = None
    commit_sha: str | None = None
    result: StructuredRunResultModel | None = None


class WaitForRunRequest(BaseModel):
    timeout_seconds: float = Field(
        default=WAIT_DEFAULT_TIMEOUT_SECONDS,
        ge=WAIT_MIN_TIMEOUT_SECONDS,
        le=WAIT_MAX_TIMEOUT_SECONDS,
    )
    poll_interval_seconds: float = Field(
        default=WAIT_DEFAULT_POLL_INTERVAL_SECONDS,
        ge=WAIT_MIN_POLL_INTERVAL_SECONDS,
        le=WAIT_MAX_POLL_INTERVAL_SECONDS,
    )


class WaitForRunResponse(RunStatusResponse):
    reached_terminal: bool
    wait_expired: bool


def _structured_result_model(
    result: StructuredRunResult | None,
) -> StructuredRunResultModel | None:
    if result is None:
        return None
    return StructuredRunResultModel.model_validate(result.to_dict())


def _run_status_response(record: RunRecord) -> RunStatusResponse:
    return RunStatusResponse(
        run_id=record.run_id,
        status=record.status.value,
        created_at=record.created_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
        elapsed_seconds=record.elapsed_seconds,
        stdout=record.stdout,
        stderr=record.stderr,
        error=record.error,
        return_code=record.return_code,
        commit_sha=record.commit_sha,
        result=_structured_result_model(record.result),
    )


def _wait_for_run_response(
    record: RunRecord,
    *,
    reached_terminal: bool,
    wait_expired: bool,
) -> WaitForRunResponse:
    base = _run_status_response(record)
    return WaitForRunResponse(
        **base.model_dump(),
        reached_terminal=reached_terminal,
        wait_expired=wait_expired,
    )
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
@app.post("/validate", response_model=ValidateResponse)
def validate_mission_endpoint(
    request: MissionYamlRequest,
) -> ValidateResponse:
    result, _ = load_mission_yaml(request.mission_yaml)
    return ValidateResponse(
        ok=result.ok,
        error=result.error,
    )
@app.post("/run", response_model=RunResponse)
def run_mission_endpoint(
    request: MissionYamlRequest,
    _auth: None = Depends(require_api_key),
) -> RunResponse:
    structural_result, mission = load_mission_yaml(
        request.mission_yaml
    )
    if not structural_result.ok:
        return RunResponse(
            ok=False,
            error=structural_result.error,
        )
    run_result = validate_mission_for_run(mission)
    if not run_result.ok:
        return RunResponse(
            ok=False,
            error=run_result.error,
        )
    preflight_error = preflight_for_execution()
    if preflight_error is not None:
        return RunResponse(
            ok=False,
            error=preflight_error.message,
            error_detail=ErrorDetail(
                **preflight_error.to_dict()
            ),
        )
    execution_result = run_cursor_agent(mission)
    if not execution_result.ok:
        return RunResponse(
            ok=False,
            stdout=execution_result.stdout,
            stderr=execution_result.stderr,
            error=execution_result.error,
        )
    return RunResponse(
        ok=True,
        stdout=execution_result.stdout,
        stderr=execution_result.stderr,
    )
@app.post(
    "/execute",
    response_model=RunResponse,
    operation_id="execute_mission_legacy",
    summary="Execute mission synchronously (legacy)",
    description=(
        "Legacy synchronous endpoint. Validates and executes a mission "
        "inline against repository.path and returns the result in the same "
        "request. Prefer POST /runs for asynchronous execution with isolated "
        "workspace handling and Git persistence."
    ),
)
def execute_mission_endpoint(
    request: MissionYamlRequest,
    _auth: None = Depends(require_api_key),
) -> RunResponse:
    structural_result, mission = load_mission_yaml(
        request.mission_yaml
    )
    if not structural_result.ok:
        return RunResponse(
            ok=False,
            error=structural_result.error,
        )
    execute_result = validate_mission_for_execute(mission)
    if not execute_result.ok:
        return RunResponse(
            ok=False,
            error=execute_result.error,
        )
    preflight_error = preflight_for_execution()
    if preflight_error is not None:
        return RunResponse(
            ok=False,
            error=preflight_error.message,
            error_detail=ErrorDetail(
                **preflight_error.to_dict()
            ),
        )
    execution_result = execute_cursor_agent(mission)
    if not execution_result.ok:
        return RunResponse(
            ok=False,
            stdout=execution_result.stdout,
            stderr=execution_result.stderr,
            error=execution_result.error,
        )
    return RunResponse(
        ok=True,
        stdout=execution_result.stdout,
        stderr=execution_result.stderr,
    )
@app.post(
    "/runs",
    status_code=202,
    operation_id="submit_run",
    summary="Submit asynchronous mission run",
    description=(
        "Validate an execute-mode mission and queue it for asynchronous "
        "execution in an isolated workspace. Only one Cursor execution is "
        "active at a time; additional runs wait in FIFO order. Poll "
        "GET /runs/{run_id} for status, output, and commit SHA. Run records "
        "are persisted in SQLite and survive process restarts."
    ),
    response_model=RunAcceptedResponse,
    responses={
        200: {
            "model": RunResponse,
            "description": (
                "Structural validation, execute eligibility, Cursor CLI "
                "preflight failure, or recursive submission rejection."
            ),
        },
        202: {
            "model": RunAcceptedResponse,
            "description": "Run accepted and queued for background execution.",
        },
    },
)
def submit_run_endpoint(
    request: MissionYamlRequest,
    raw_request: Request,
    _auth: None = Depends(require_api_key),
) -> RunAcceptedResponse:
    if is_recursive_submission(dict(raw_request.headers)):
        logger.info(
            "lifecycle event=recursive_submission_rejected"
        )
        return JSONResponse(
            status_code=200,
            content=RunResponse(
                ok=False,
                error=RECURSIVE_SUBMISSION_ERROR,
                error_detail=ErrorDetail(
                    code="RECURSIVE_SUBMISSION",
                    message=RECURSIVE_SUBMISSION_ERROR,
                    stage="submit",
                ),
            ).model_dump(),
        )
    structural_result, mission = load_mission_yaml(
        request.mission_yaml
    )
    if not structural_result.ok:
        return JSONResponse(
            status_code=200,
            content=RunResponse(
                ok=False,
                error=structural_result.error,
            ).model_dump(),
        )
    execute_result = validate_mission_for_execute(mission)
    if not execute_result.ok:
        return JSONResponse(
            status_code=200,
            content=RunResponse(
                ok=False,
                error=execute_result.error,
            ).model_dump(),
        )
    preflight_error = preflight_for_execution()
    if preflight_error is not None:
        return JSONResponse(
            status_code=200,
            content=RunResponse(
                ok=False,
                error=preflight_error.message,
                error_detail=ErrorDetail(
                    **preflight_error.to_dict()
                ),
            ).model_dump(),
        )
    record = run_registry.create_run()
    count, keys = run_registry.diagnostic_state()
    logger.info(
        (
            "lifecycle run_id=%s event=accepted status=%s pending=%s "
            "active=%s api_pid=%s registry_id=%s registry_count=%s "
            "registry_keys=%s"
        ),
        record.run_id,
        RunStatus.QUEUED.value,
        run_queue.pending_count(),
        run_queue.active_run_id,
        os.getpid(),
        id(run_registry),
        count,
        keys,
    )
    run_queue.enqueue(record.run_id, mission, run_registry)
    return RunAcceptedResponse(
        run_id=record.run_id,
        status=RunStatus.QUEUED.value,
    )
@app.get(
    "/runs/{run_id}",
    response_model=RunStatusResponse,
    operation_id="get_run",
    summary="Get asynchronous run status",
    description=(
        "Return the lifecycle status, execution output, error, commit SHA, "
        "and structured result evidence for a run previously submitted via "
        "POST /runs. The `result` object is objective evidence collected by "
        "Mission Control (changed files, commands Mission Control executed, "
        "deliverable verification, persistence outcome). Agent-authored "
        "`stdout` / `stderr` remain available for diagnostics but are not "
        "verified structured evidence. Completed and failed runs remain "
        "available in the SQLite-backed run registry."
    ),
    responses={
        200: {
            "description": "Run record found.",
            "content": {
                "application/json": {
                    "examples": {
                        "completed": {
                            "summary": "Completed run with structured result",
                            "value": {
                                "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                                "status": "completed",
                                "created_at": "2026-07-23T17:00:00+00:00",
                                "started_at": "2026-07-23T17:00:01+00:00",
                                "completed_at": "2026-07-23T17:01:30+00:00",
                                "elapsed_seconds": 89.0,
                                "stdout": "Agent prose summary (diagnostic only)\n",
                                "stderr": "",
                                "error": None,
                                "return_code": 0,
                                "commit_sha": "abc123def456",
                                "result": {
                                    "files_changed": [
                                        "docs/HAL_OPERATOR_LOG.md",
                                        "mission_control/run_result.py",
                                    ],
                                    "commands": [
                                        {
                                            "argv": [
                                                "cursor-agent",
                                                "--print",
                                                "--force",
                                                "--output-format",
                                                "text",
                                                "--workspace",
                                                "/tmp/mission-control-run-xyz",
                                                "--trust",
                                                "<instruction>",
                                            ],
                                            "exit_code": 0,
                                            "passed": True,
                                            "kind": "cursor_agent",
                                        }
                                    ],
                                    "test_counts": None,
                                    "deliverables": {
                                        "verified": True,
                                        "passed": True,
                                        "checked_paths": [
                                            "docs/HAL_OPERATOR_LOG.md"
                                        ],
                                        "missing": [],
                                    },
                                    "persistence": {
                                        "mode": "commit",
                                        "attempted": True,
                                        "ok": True,
                                        "commit_sha": "abc123def456",
                                    },
                                    "warnings": [
                                        (
                                            "Aggregate test counts are "
                                            "unavailable; Mission Control "
                                            "does not parse agent stdout for "
                                            "test results."
                                        ),
                                        (
                                            "No separate Mission Control "
                                            "verification shell commands "
                                            "were executed; only the Cursor "
                                            "agent subprocess and platform "
                                            "checks are recorded."
                                        ),
                                    ],
                                },
                            },
                        }
                    }
                }
            },
        },
        404: {"description": "Unknown run_id."},
    },
)
def get_run_endpoint(
    run_id: str,
    _auth: None = Depends(require_api_key),
) -> RunStatusResponse:
    record = run_registry.get_run(run_id)
    if record is None:
        logger.info(
            "lifecycle run_id=%s event=lookup_miss",
            run_id,
        )
        raise HTTPException(
            status_code=404,
            detail="Run not found",
        )
    logger.info(
        "lifecycle run_id=%s event=lookup status=%s",
        run_id,
        record.status.value,
    )
    return _run_status_response(record)


@app.post(
    "/runs/{run_id}/wait",
    response_model=WaitForRunResponse,
    operation_id="wait_for_run",
    summary="Wait for an asynchronous run to reach a terminal status",
    description=(
        "Poll the existing run lookup path until the run reaches a terminal "
        "status (completed, failed, or timed_out) or timeout_seconds elapses. "
        "Returns immediately when the run is already terminal. Wait timeout "
        "does not mutate run state. Intended HAL flow: submit_run, then "
        "wait_for_run, then inspect status/output/commit_sha."
    ),
)
def wait_for_run_endpoint(
    run_id: str,
    request: WaitForRunRequest = Body(default_factory=WaitForRunRequest),
    _auth: None = Depends(require_api_key),
) -> WaitForRunResponse:
    timeout_seconds = request.timeout_seconds
    poll_interval_seconds = request.poll_interval_seconds
    deadline = time.monotonic() + timeout_seconds

    while True:
        # get_run acquires and releases the registry lock per lookup so the
        # wait loop never holds SQLite locks while sleeping.
        record = run_registry.get_run(run_id)
        if record is None:
            logger.info(
                "lifecycle run_id=%s event=wait_lookup_miss",
                run_id,
            )
            raise HTTPException(
                status_code=404,
                detail="Run not found",
            )

        if is_terminal_status(record.status):
            logger.info(
                "lifecycle run_id=%s event=wait_terminal status=%s",
                run_id,
                record.status.value,
            )
            return _wait_for_run_response(
                record,
                reached_terminal=True,
                wait_expired=False,
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.info(
                "lifecycle run_id=%s event=wait_expired status=%s",
                run_id,
                record.status.value,
            )
            return _wait_for_run_response(
                record,
                reached_terminal=False,
                wait_expired=True,
            )

        time.sleep(min(poll_interval_seconds, remaining))
