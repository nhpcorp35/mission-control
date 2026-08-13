"""Mission Control cloud API."""
from contextlib import asynccontextmanager
from datetime import datetime
import logging
import os
import time
from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
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
from mission_control.command_runner import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    RepositoryCommandSpec,
    run_repository_command,
)
from mission_control.mission_builder import (
    DEFAULT_ALLOW_AUTOMATIC_PLATFORM_PUSH,
    DEFAULT_BASE_BRANCH,
    DEFAULT_PLATFORM_MAIN_WRITE_ACKNOWLEDGED,
    DEFAULT_PLATFORM_PUSH_APPROVED,
    DEFAULT_REPOSITORY_NAME,
    DEFAULT_REPOSITORY_PATH,
    DEFAULT_RUN_COMMANDS,
    render_mission_yaml,
)
from mission_control.openapi_actions import build_actions_openapi
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
            # Fall through to the terminal-status guarantee below instead of
            # re-raising: an uncaught exception must not leave the run stuck
            # in queued/running with empty stdout/stderr/summary.
        finally:
            record = registry.get_run(run_id)
            if record is not None and not is_terminal_status(record.status):
                error = record.error or (
                    "Run worker exited without reaching a terminal status."
                )
                try:
                    registry.store_result(run_id, error=error)
                    registry.update_status(run_id, RunStatus.FAILED)
                    record = registry.get_run(run_id)
                except Exception:
                    logger.exception(
                        (
                            "lifecycle run_id=%s event=exception "
                            "api_pid=%s registry_id=%s "
                            "stage=terminal_status_guarantee"
                        ),
                        run_id,
                        os.getpid(),
                        id(registry),
                    )
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
PRODUCTION_SERVER_URL = (
    "https://mission-control-production-76ff.up.railway.app"
)

app = FastAPI(
    title="Mission Control API",
    version="1.0.0",
    lifespan=lifespan,
    servers=[{"url": PRODUCTION_SERVER_URL}],
)
class MissionYamlRequest(BaseModel):
    mission_yaml: str = Field(..., min_length=1)


class StructuredApproval(BaseModel):
    """Canonical nested approval fields for structured submission."""

    platform_push_approved: bool | None = None
    platform_main_write_acknowledged: bool | None = None


class StructuredRunRequest(BaseModel):
    """Structured Mission Spec fields for POST /runs/structured (v1)."""

    mission_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    instructions: str = Field(..., min_length=1)
    deliverables: list = Field(...)
    create_files: bool
    modify_files: bool
    # None → mission builder infers push for create/modify, else none.
    # Explicit values (including "none") are never overridden.
    persistence_mode: str | None = None
    # Push destination is never inferred; callers must supply for mode=push.
    target_branch: str | None = None
    repository_name: str = DEFAULT_REPOSITORY_NAME
    repository_path: str = DEFAULT_REPOSITORY_PATH
    base_branch: str = DEFAULT_BASE_BRANCH
    run_commands: bool = DEFAULT_RUN_COMMANDS
    platform_push_approved: bool = DEFAULT_PLATFORM_PUSH_APPROVED
    approval: StructuredApproval | None = None
    allow_automatic_platform_push: bool = (
        DEFAULT_ALLOW_AUTOMATIC_PLATFORM_PUSH
    )
    platform_main_write_acknowledged: bool = (
        DEFAULT_PLATFORM_MAIN_WRITE_ACKNOWLEDGED
    )

    @model_validator(mode="after")
    def normalize_platform_push_approved(self) -> "StructuredRunRequest":
        """Accept flat and/or nested platform_push_approved; reject conflicts."""
        flat_provided = "platform_push_approved" in self.model_fields_set
        nested_provided = False
        nested_value: bool | None = None
        if self.approval is not None:
            nested_provided = (
                "platform_push_approved" in self.approval.model_fields_set
            )
            if nested_provided:
                nested_value = self.approval.platform_push_approved
                if nested_value is None:
                    raise ValueError(
                        "approval.platform_push_approved must be a boolean "
                        "when provided"
                    )

        if flat_provided and nested_provided:
            if self.platform_push_approved != nested_value:
                raise ValueError(
                    "Conflicting platform_push_approved values: "
                    f"flat platform_push_approved={self.platform_push_approved!r} "
                    "does not match "
                    f"approval.platform_push_approved={nested_value!r}"
                )
        elif nested_provided and not flat_provided:
            # Honor nested-only approval; do not silently drop it.
            self.platform_push_approved = bool(nested_value)

        flat_main_ack = "platform_main_write_acknowledged" in self.model_fields_set
        nested_main_ack = False
        nested_main_value: bool | None = None
        if self.approval is not None:
            nested_main_ack = (
                "platform_main_write_acknowledged"
                in self.approval.model_fields_set
            )
            if nested_main_ack:
                nested_main_value = self.approval.platform_main_write_acknowledged
                if nested_main_value is None:
                    raise ValueError(
                        "approval.platform_main_write_acknowledged must be a "
                        "boolean when provided"
                    )

        if flat_main_ack and nested_main_ack:
            if self.platform_main_write_acknowledged != nested_main_value:
                raise ValueError(
                    "Conflicting platform_main_write_acknowledged values: "
                    f"flat={self.platform_main_write_acknowledged!r} does not "
                    f"match nested={nested_main_value!r}"
                )
        elif nested_main_ack and not flat_main_ack:
            self.platform_main_write_acknowledged = bool(nested_main_value)

        return self


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
    pushed: bool | None = None


class DocumentationEvidenceModel(BaseModel):
    mode: str
    status: str


class StructuredRunResultModel(BaseModel):
    """Objective Mission Control evidence (not agent-authored stdout)."""

    files_changed: list[str] = Field(default_factory=list)
    commands: list[CommandEvidenceModel] = Field(default_factory=list)
    test_counts: dict[str, int] | None = None
    deliverables: DeliverableEvidenceModel | None = None
    persistence: PersistenceEvidenceModel | None = None
    documentation: DocumentationEvidenceModel | None = None
    warnings: list[str] = Field(default_factory=list)
    summary: str | None = None


class RunProgressModel(BaseModel):
    """Platform-authored live progress (bounded; never raw agent output)."""

    step: str
    detail: str


class RunStatusResponse(BaseModel):
    run_id: str
    status: str
    created_at: datetime
    queued_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    elapsed_seconds: float | None = None
    phase: str
    phase_started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    progress: RunProgressModel | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    return_code: int | None = None
    commit_sha: str | None = None
    result: StructuredRunResultModel | None = None
    summary: str | None = None
    retried_from: str | None = None


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


class SubmitAndWaitRequest(BaseModel):
    """Exact YAML submit plus bounded wait (POST /runs/submit-and-wait)."""

    mission_yaml: str = Field(..., min_length=1)
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


class RepositoryCommandRequest(BaseModel):
    """Typed repository command execution (POST /repository-commands)."""

    repository: str = Field(..., min_length=1)
    ref: str = Field(
        ...,
        min_length=1,
        description="Branch name or commit SHA to check out.",
    )
    argv: list[str] = Field(..., min_length=2)
    working_directory: str = "."
    timeout_seconds: float = Field(
        default=DEFAULT_TIMEOUT_SECONDS,
        ge=MIN_TIMEOUT_SECONDS,
        le=MAX_TIMEOUT_SECONDS,
    )
    allowed_env_names: list[str] = Field(default_factory=list)


class RepositoryCommandPersistenceModel(BaseModel):
    mode: str = "none"
    attempted: bool = False
    ok: bool | None = True
    commit_sha: str | None = None
    pushed: bool | None = False


class RepositoryCommandResponse(BaseModel):
    """Allowlisted repository command result (persistence always none)."""

    ok: bool
    run_id: str
    checkout_commit: str | None = None
    argv: list[str] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    elapsed_seconds: float = 0.0
    artifact_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Ephemeral local files under --candidate-output-root when present. "
            "Not durable proof; Case-00 durable candidate keys come from "
            "verified B2 upload reported in wrapper stdout (durable_artifacts)."
        ),
    )
    persistence: RepositoryCommandPersistenceModel
    error: str | None = None
    error_code: str | None = None


class WaitForRunResponse(RunStatusResponse):
    reached_terminal: bool
    wait_expired: bool
    timeout_seconds: float


def _structured_result_model(
    result: StructuredRunResult | None,
) -> StructuredRunResultModel | None:
    if result is None:
        return None
    return StructuredRunResultModel.model_validate(result.to_dict())


def _run_status_response(record: RunRecord) -> RunStatusResponse:
    structured = _structured_result_model(record.result)
    summary = None
    if structured is not None and structured.summary is not None:
        summary = structured.summary
    progress = None
    if record.progress is not None:
        progress = RunProgressModel(
            step=record.progress.get("step", record.phase.value),
            detail=record.progress.get("detail", ""),
        )
    return RunStatusResponse(
        run_id=record.run_id,
        status=record.status.value,
        created_at=record.created_at,
        queued_at=record.queued_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
        elapsed_seconds=record.elapsed_seconds,
        phase=record.phase.value,
        phase_started_at=record.phase_started_at,
        heartbeat_at=record.heartbeat_at,
        progress=progress,
        stdout=record.stdout,
        stderr=record.stderr,
        error=record.error,
        return_code=record.return_code,
        commit_sha=record.commit_sha,
        result=structured,
        summary=summary,
        retried_from=record.retried_from,
    )


def _wait_for_run_response(
    record: RunRecord,
    *,
    reached_terminal: bool,
    wait_expired: bool,
    timeout_seconds: float,
) -> WaitForRunResponse:
    base = _run_status_response(record)
    return WaitForRunResponse(
        **base.model_dump(),
        reached_terminal=reached_terminal,
        wait_expired=wait_expired,
        timeout_seconds=timeout_seconds,
    )


def _wait_for_run(
    run_id: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> WaitForRunResponse:
    """Poll registry get_run until terminal status or wait budget elapses."""
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
                timeout_seconds=timeout_seconds,
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
                timeout_seconds=timeout_seconds,
            )

        time.sleep(min(poll_interval_seconds, remaining))


def _reject_run_response(
    *,
    error: str,
    error_detail: ErrorDetail | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content=RunResponse(
            ok=False,
            error=error,
            error_detail=error_detail,
        ).model_dump(),
    )


def _accept_async_run(
    mission_yaml: str,
    *,
    retried_from: str | None = None,
) -> RunAcceptedResponse | JSONResponse:
    """Validate and queue a mission through the async submission pipeline."""
    structural_result, mission = load_mission_yaml(mission_yaml)
    if not structural_result.ok:
        return _reject_run_response(error=structural_result.error)
    execute_result = validate_mission_for_execute(mission)
    if not execute_result.ok:
        return _reject_run_response(error=execute_result.error)
    preflight_error = preflight_for_execution()
    if preflight_error is not None:
        return _reject_run_response(
            error=preflight_error.message,
            error_detail=ErrorDetail(**preflight_error.to_dict()),
        )
    record = run_registry.create_run(
        mission_yaml=mission_yaml,
        retried_from=retried_from,
    )
    count, keys = run_registry.diagnostic_state()
    logger.info(
        (
            "lifecycle run_id=%s event=accepted status=%s pending=%s "
            "active=%s retried_from=%s api_pid=%s registry_id=%s "
            "registry_count=%s registry_keys=%s"
        ),
        record.run_id,
        RunStatus.QUEUED.value,
        run_queue.pending_count(),
        run_queue.active_run_id,
        retried_from,
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
# Intentionally unauthenticated for health monitoring (e.g. Railway and uptime checks).
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(
    "/openapi-actions.json",
    include_in_schema=False,
    summary="Custom GPT Actions OpenAPI schema",
)
def openapi_actions_schema() -> JSONResponse:
    """Serve an Actions-importer-compatible OpenAPI document.

    Preserves ``/openapi.json`` (OpenAPI 3.1) for normal clients. Custom GPT
    Actions should import this endpoint instead.
    """
    return JSONResponse(build_actions_openapi(app.openapi()))


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
        return _reject_run_response(
            error=RECURSIVE_SUBMISSION_ERROR,
            error_detail=ErrorDetail(
                code="RECURSIVE_SUBMISSION",
                message=RECURSIVE_SUBMISSION_ERROR,
                stage="submit",
            ),
        )
    return _accept_async_run(request.mission_yaml)


@app.post(
    "/runs/structured",
    status_code=202,
    operation_id="submit_structured_run",
    summary="Submit asynchronous mission run from structured fields",
    description=(
        "Build Mission Spec v1.0 YAML from structured fields (safe execute "
        "defaults; omitted persistence_mode → push for create/modify, none "
        "for read-only), then validate and queue it through the same "
        "asynchronous pipeline as POST /runs. Poll GET /runs/{run_id} for "
        "status. Raw YAML submission via POST /runs remains supported."
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
def submit_structured_run_endpoint(
    request: StructuredRunRequest,
    raw_request: Request,
    _auth: None = Depends(require_api_key),
) -> RunAcceptedResponse:
    if is_recursive_submission(dict(raw_request.headers)):
        logger.info(
            "lifecycle event=recursive_submission_rejected"
        )
        return _reject_run_response(
            error=RECURSIVE_SUBMISSION_ERROR,
            error_detail=ErrorDetail(
                code="RECURSIVE_SUBMISSION",
                message=RECURSIVE_SUBMISSION_ERROR,
                stage="submit",
            ),
        )
    mission_yaml = render_mission_yaml(
        mission_id=request.mission_id,
        title=request.title,
        instructions=request.instructions,
        deliverables=request.deliverables,
        create_files=request.create_files,
        modify_files=request.modify_files,
        persistence_mode=request.persistence_mode,
        target_branch=request.target_branch,
        repository_name=request.repository_name,
        repository_path=request.repository_path,
        base_branch=request.base_branch,
        run_commands=request.run_commands,
        platform_push_approved=request.platform_push_approved,
        allow_automatic_platform_push=(
            request.allow_automatic_platform_push
        ),
        platform_main_write_acknowledged=(
            request.platform_main_write_acknowledged
        ),
    )
    return _accept_async_run(mission_yaml)


@app.post(
    "/runs/submit-and-wait",
    operation_id="submit_and_wait",
    summary="Submit mission YAML and wait for a terminal status",
    description=(
        "Accept an exact Mission Control YAML document, queue it through the "
        "same asynchronous pipeline as POST /runs, then wait via the same "
        "logic as POST /runs/{run_id}/wait until the run reaches a terminal "
        "status or timeout_seconds elapses. Returns the final authoritative "
        "run payload in one request. Validation or submission failures return "
        "immediately without entering the wait loop. Intended Custom GPT / "
        "HAL flow when exact YAML is already available."
    ),
    response_model=WaitForRunResponse,
    responses={
        200: {
            "description": (
                "Wait finished (terminal or wait budget exhausted), or "
                "structural validation / execute eligibility / Cursor CLI "
                "preflight / recursive submission rejection without queueing."
            ),
            "content": {
                "application/json": {
                    "schema": {
                        "oneOf": [
                            {"$ref": "#/components/schemas/WaitForRunResponse"},
                            {"$ref": "#/components/schemas/RunResponse"},
                        ]
                    }
                }
            },
        },
    },
)
def submit_and_wait_endpoint(
    request: SubmitAndWaitRequest,
    raw_request: Request,
    _auth: None = Depends(require_api_key),
) -> WaitForRunResponse | JSONResponse:
    if is_recursive_submission(dict(raw_request.headers)):
        logger.info(
            "lifecycle event=recursive_submission_rejected stage=submit_and_wait"
        )
        return _reject_run_response(
            error=RECURSIVE_SUBMISSION_ERROR,
            error_detail=ErrorDetail(
                code="RECURSIVE_SUBMISSION",
                message=RECURSIVE_SUBMISSION_ERROR,
                stage="submit",
            ),
        )

    accepted = _accept_async_run(request.mission_yaml)
    if isinstance(accepted, JSONResponse):
        return accepted

    logger.info(
        "lifecycle run_id=%s event=submit_and_wait_accepted",
        accepted.run_id,
    )
    return _wait_for_run(
        accepted.run_id,
        timeout_seconds=request.timeout_seconds,
        poll_interval_seconds=request.poll_interval_seconds,
    )


@app.get(
    "/runs/{run_id}",
    response_model=RunStatusResponse,
    operation_id="get_run",
    summary="Get asynchronous run status",
    description=(
        "Return the lifecycle status, execution output, error, commit SHA, "
        "authoritative summary, and structured result evidence for a run "
        "previously submitted via POST /runs. The top-level `summary` and "
        "`result` object are objective Mission Control evidence (changed "
        "files, commands Mission Control executed, deliverable verification, "
        "persistence outcome). Prefer `summary`, `result.persistence`, and "
        "`commit_sha` over agent-authored `stdout` / `stderr` for persistence "
        "claims: platform persistence runs after the agent completes, so "
        "agent prose may correctly report that no agent commit/push occurred "
        "while Mission Control still recorded a successful platform "
        "persistence outcome. Completed and failed runs remain available in "
        "the SQLite-backed run registry. When a run was created via "
        "POST /runs/{run_id}/retry, `retried_from` identifies the source "
        "failed run."
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
                                "queued_at": "2026-07-23T17:00:00+00:00",
                                "started_at": "2026-07-23T17:00:01+00:00",
                                "completed_at": "2026-07-23T17:01:30+00:00",
                                "elapsed_seconds": 89.0,
                                "phase": "completed",
                                "phase_started_at": "2026-07-23T17:01:30+00:00",
                                "heartbeat_at": "2026-07-23T17:01:30+00:00",
                                "progress": {
                                    "step": "completed",
                                    "detail": "Run completed",
                                },
                                "stdout": "Agent prose summary (diagnostic only)\n",
                                "stderr": "",
                                "error": None,
                                "return_code": 0,
                                "commit_sha": "abc123def456",
                                "summary": (
                                    "Platform persistence succeeded "
                                    "(mode=commit, commit_sha=abc123def456). "
                                    "Agent stdout is diagnostic only and was "
                                    "captured before platform persistence when "
                                    "persistence ran; prefer this summary, "
                                    "result.persistence, and commit_sha for "
                                    "persistence claims."
                                ),
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
                                        "pushed": False,
                                    },
                                    "documentation": {
                                        "mode": "required",
                                        "status": "updated",
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
                                        (
                                            "Agent stdout was captured before "
                                            "platform persistence; prefer "
                                            "result.summary, "
                                            "result.persistence, and "
                                            "commit_sha for the persistence "
                                            "outcome."
                                        ),
                                    ],
                                    "summary": (
                                        "Platform persistence succeeded "
                                        "(mode=commit, "
                                        "commit_sha=abc123def456). "
                                        "Agent stdout is diagnostic only and "
                                        "was captured before platform "
                                        "persistence when persistence ran; "
                                        "prefer this summary, "
                                        "result.persistence, and commit_sha "
                                        "for persistence claims."
                                    ),
                                },
                                "retried_from": None,
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
    "/runs/{run_id}/retry",
    status_code=202,
    operation_id="retry_run",
    summary="Retry a failed asynchronous run",
    description=(
        "Create a new asynchronous run from the exact stored mission YAML of "
        "an existing terminal failed run. The source run is left unchanged. "
        "The new run receives a fresh run_id, workspace lifecycle, and "
        "retried_from linkage back to the source. Only status failed may be "
        "retried; queued, running, completed, and timed_out return 409."
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
            "description": "Retry accepted and queued for background execution.",
        },
        404: {"description": "Unknown source run_id."},
        409: {
            "description": (
                "Source run is not eligible for retry (not failed, or missing "
                "stored mission YAML)."
            ),
        },
    },
)
def retry_run_endpoint(
    run_id: str,
    raw_request: Request,
    _auth: None = Depends(require_api_key),
) -> RunAcceptedResponse:
    if is_recursive_submission(dict(raw_request.headers)):
        logger.info(
            "lifecycle event=recursive_submission_rejected stage=retry"
        )
        return _reject_run_response(
            error=RECURSIVE_SUBMISSION_ERROR,
            error_detail=ErrorDetail(
                code="RECURSIVE_SUBMISSION",
                message=RECURSIVE_SUBMISSION_ERROR,
                stage="retry",
            ),
        )

    source = run_registry.get_run(run_id)
    if source is None:
        logger.info(
            "lifecycle run_id=%s event=retry_lookup_miss",
            run_id,
        )
        raise HTTPException(
            status_code=404,
            detail="Run not found",
        )

    if source.status is not RunStatus.FAILED:
        logger.info(
            "lifecycle run_id=%s event=retry_rejected status=%s",
            run_id,
            source.status.value,
        )
        raise HTTPException(
            status_code=409,
            detail=(
                "Only failed runs may be retried "
                f"(current status: {source.status.value})"
            ),
        )

    if not source.mission_yaml:
        logger.info(
            "lifecycle run_id=%s event=retry_rejected reason=missing_yaml",
            run_id,
        )
        raise HTTPException(
            status_code=409,
            detail="Source run has no stored mission YAML to retry",
        )

    logger.info(
        "lifecycle run_id=%s event=retry_requested",
        run_id,
    )
    return _accept_async_run(
        source.mission_yaml,
        retried_from=source.run_id,
    )


@app.post(
    "/repository-commands",
    operation_id="run_repository_command",
    summary="Run an allowlisted repository command in an ephemeral checkout",
    description=(
        "Clone an allowlisted repository at the requested branch/commit, "
        "execute argv directly without a shell, and return stdout/stderr. "
        "Allowlist includes LegalAI generation, Case-00 rebuild, and "
        "Case-00 B2 Q1 scripts. Persistence is always none (never commits "
        "or pushes). Sensitive argv values are redacted in the response. "
        "artifact_paths are ephemeral local files only; durable Case-00 "
        "candidate keys come from verified B2 upload in wrapper stdout."
    ),
    response_model=RepositoryCommandResponse,
)
def run_repository_command_endpoint(
    request: RepositoryCommandRequest,
    raw_request: Request,
    _auth: None = Depends(require_api_key),
) -> RepositoryCommandResponse:
    if is_recursive_submission(dict(raw_request.headers)):
        logger.info(
            "lifecycle event=recursive_submission_rejected "
            "stage=repository_command"
        )
        return RepositoryCommandResponse(
            ok=False,
            run_id="",
            argv=list(request.argv),
            persistence=RepositoryCommandPersistenceModel(),
            error=RECURSIVE_SUBMISSION_ERROR,
            error_code="RECURSIVE_SUBMISSION",
        )

    record = run_registry.create_run(
        mission_yaml=(
            "# repository-command\n"
            f"repository: {request.repository}\n"
            f"ref: {request.ref}\n"
        ),
    )
    run_registry.update_status(record.run_id, RunStatus.RUNNING)
    result = run_repository_command(
        RepositoryCommandSpec(
            repository=request.repository,
            ref=request.ref,
            argv=list(request.argv),
            working_directory=request.working_directory,
            timeout_seconds=request.timeout_seconds,
            allowed_env_names=list(request.allowed_env_names),
        ),
        run_id=record.run_id,
    )
    status = RunStatus.COMPLETED if result.ok else (
        RunStatus.TIMED_OUT
        if result.error_code == "TIMEOUT"
        else RunStatus.FAILED
    )
    run_registry.store_result(
        record.run_id,
        stdout=result.stdout,
        stderr=result.stderr,
        error=result.error,
        return_code=result.exit_code,
        commit_sha=result.checkout_commit,
    )
    run_registry.update_status(record.run_id, status)
    return RepositoryCommandResponse(
        ok=result.ok,
        run_id=result.run_id,
        checkout_commit=result.checkout_commit,
        argv=list(result.argv),
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        elapsed_seconds=result.elapsed_seconds,
        artifact_paths=list(result.artifact_paths),
        persistence=RepositoryCommandPersistenceModel(
            **result.persistence
        ),
        error=result.error,
        error_code=result.error_code,
    )


@app.post(
    "/runs/{run_id}/wait",
    response_model=WaitForRunResponse,
    operation_id="wait_for_run",
    summary="Wait for an asynchronous run to reach a terminal status",
    description=(
        "Poll the existing run lookup path until the run reaches a terminal "
        "status (completed, failed, or timed_out) or timeout_seconds elapses. "
        "Returns immediately when the run is already terminal. Wait timeout "
        "does not mutate run state. Intended HAL / Custom GPT flow: "
        "submit_run, then wait_for_run, then inspect status/output/commit_sha. "
        "For exact YAML end-to-end in one request, use submit_and_wait."
    ),
)
def wait_for_run_endpoint(
    run_id: str,
    request: WaitForRunRequest = Body(default_factory=WaitForRunRequest),
    _auth: None = Depends(require_api_key),
) -> WaitForRunResponse:
    return _wait_for_run(
        run_id,
        timeout_seconds=request.timeout_seconds,
        poll_interval_seconds=request.poll_interval_seconds,
    )
