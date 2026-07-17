"""Mission Control cloud API."""

from fastapi import FastAPI
from pydantic import BaseModel, Field

from mission_control.executor import run_cursor_agent
from mission_control.validator import load_mission_yaml, validate_mission_for_run

app = FastAPI(title="Mission Control API", version="1.0.0")


class MissionYamlRequest(BaseModel):
    mission_yaml: str = Field(..., min_length=1)


class ValidateResponse(BaseModel):
    ok: bool
    error: str | None = None


class RunResponse(BaseModel):
    ok: bool
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/validate", response_model=ValidateResponse)
def validate_mission_endpoint(request: MissionYamlRequest) -> ValidateResponse:
    result, _ = load_mission_yaml(request.mission_yaml)
    return ValidateResponse(ok=result.ok, error=result.error)


@app.post("/run", response_model=RunResponse)
def run_mission_endpoint(request: MissionYamlRequest) -> RunResponse:
    structural_result, mission = load_mission_yaml(request.mission_yaml)
    if not structural_result.ok:
        return RunResponse(ok=False, error=structural_result.error)

    run_result = validate_mission_for_run(mission)
    if not run_result.ok:
        return RunResponse(ok=False, error=run_result.error)

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
