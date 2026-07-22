"""Mission Control cloud API."""
from contextlib import asynccontextmanager
from datetime import datetime
import logging
import os
from fastapi import Depends, FastAPI, HTTPException, Request
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
)
from mission_control.workspace import execute_registered_run
from mission_control.validator import (
    load_mission_yaml,
    validate_mission_for_execute,
    validate_mission_for_run,
)
logger = logging.getLogger(__name__)
run_registry = RunRegistry()
run_queue = RunQueue()


def _execute_queued_run(run_id: str, mission: dict, registry: RunRegistry) -> None:
    """Run one queued mission with lifecycle logging (no secrets)."""
    logger.info("lifecycle run_id=%s event=started", run_id)
    with execution_scope():
        try:
            execute_registered_run(run_id, mission, registry)
        finally:
            record = registry.get_run(run_id)
            status = record.status.value if record is not None else "unknown"
            error = record.error if record is not None else None
            # Log failure presence without dumping full stderr/YAML secrets.
            logger.info(
                "lifecycle run_id=%s event=finished status=%s has_error=%s",
                run_id,
                status,
                bool(error),
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
        "are retained in process memory for the lifetime of the server "
        "process (including completed and failed runs)."
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
    logger.info(
        "lifecycle run_id=%s event=accepted status=%s pending=%s active=%s",
        record.run_id,
        RunStatus.QUEUED.value,
        run_queue.pending_count(),
        run_queue.active_run_id,
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
        "Return the lifecycle status, execution output, error, and commit "
        "SHA for a run previously submitted via POST /runs. Completed and "
        "failed runs remain available until the process restarts. Run state "
        "is process-local in-memory only and is not durable."
    ),
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
