"""Mission Control cloud API."""

from contextlib import asynccontextmanager
import logging
import os

from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.cursor_cli import (
    augment_path,
    check_cursor_cli_status,
    preflight_for_execution,
)
from mission_control.executor import (
    execute_cursor_agent,
    run_cursor_agent,
)
from mission_control.validator import (
    load_mission_yaml,
    validate_mission_for_execute,
    validate_mission_for_run,
)

logger = logging.getLogger(__name__)


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


@app.post("/execute", response_model=RunResponse)
def execute_mission_endpoint(
    request: MissionYamlRequest,
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
