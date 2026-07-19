    1  """Mission Control cloud API."""
     2
     3  from contextlib import asynccontextmanager
     4  from datetime import datetime
     5  import logging
     6  import os
     7  import threading
     8
     9  from fastapi import Depends, FastAPI, HTTPException
    10  from fastapi.responses import JSONResponse
    11  from pydantic import BaseModel, Field
    12
    13  from app.auth import require_api_key
    14  from app.cursor_cli import (
    15      augment_path,
    16      check_cursor_cli_status,
    17      preflight_for_execution,
    18  )
    19  from mission_control.executor import (
    20      execute_cursor_agent,
    21      run_cursor_agent,
    22  )
    23  from mission_control.run_registry import (
    24      RunRecord,
    25      RunRegistry,
    26      RunStatus,
    27  )
    28  from mission_control.validator import (
    29      load_mission_yaml,
    30      validate_mission_for_execute,
    31      validate_mission_for_run,
    32  )
    33
    34  logger = logging.getLogger(__name__)
    35
    36  run_registry = RunRegistry()
    37
    38
    39  @asynccontextmanager
    40  async def lifespan(_: FastAPI):
    41      os.environ["PATH"] = augment_path()
    42
    43      status = check_cursor_cli_status()
    44
    45      logger.info(
    46          "Cursor CLI startup check: installed=%s authenticated=%s binary=%s",
    47          status.installed,
    48          status.authenticated,
    49          status.binary_path or "not found",
    50      )
    51
    52      yield
    53
    54
    55  app = FastAPI(
    56      title="Mission Control API",
    57      version="1.0.0",
    58      lifespan=lifespan,
    59  )
    60
    61
    62  class MissionYamlRequest(BaseModel):
    63      mission_yaml: str = Field(..., min_length=1)
    64
    65
    66  class ValidateResponse(BaseModel):
    67      ok: bool
    68      error: str | None = None
    69
    70
    71  class ErrorDetail(BaseModel):
    72      code: str
    73      message: str
    74      stage: str
    75
    76
    77  class RunResponse(BaseModel):
    78      ok: bool
    79      stdout: str = ""
    80      stderr: str = ""
    81      error: str | None = None
    82      error_detail: ErrorDetail | None = None
    83
    84
    85  class RunAcceptedResponse(BaseModel):
    86      run_id: str
    87      status: str
    88
    89
    90  class RunStatusResponse(BaseModel):
    91      run_id: str
    92      status: str
    93      created_at: datetime
    94      started_at: datetime | None = None
    95      completed_at: datetime | None = None
    96      elapsed_seconds: float | None = None
    97      stdout: str = ""
    98      stderr: str = ""
    99      error: str | None = None
   100
   101
   102  def _map_execution_status(ok: bool, error: str | None) -> RunStatus:
   103      if ok:
   104          return RunStatus.COMPLETED
   105      if error is not None and "timed out" in error:
   106          return RunStatus.TIMED_OUT
   107      return RunStatus.FAILED
   108
   109
   110  def _execute_run_worker(
   111      run_id: str,
   112      mission: dict,
   113      registry: RunRegistry,
   114  ) -> None:
   115      registry.update_status(run_id, RunStatus.RUNNING)
   116
   117      try:
   118          execution_result = execute_cursor_agent(mission)
   119      except Exception as exc:  # pragma: no cover - defensive
   120          registry.store_result(run_id, error=str(exc))
   121          registry.update_status(run_id, RunStatus.FAILED)
   122          return
   123
   124      registry.store_result(
   125          run_id,
   126          stdout=execution_result.stdout,
   127          stderr=execution_result.stderr,
   128          error=execution_result.error,
   129      )
   130      registry.update_status(
   131          run_id,
   132          _map_execution_status(
   133              execution_result.ok,
   134              execution_result.error,
   135          ),
   136      )
   137
   138
   139  def _run_status_response(record: RunRecord) -> RunStatusResponse:
   140      return RunStatusResponse(
   141          run_id=record.run_id,
   142          status=record.status.value,
   143          created_at=record.created_at,
   144          started_at=record.started_at,
   145          completed_at=record.completed_at,
   146          elapsed_seconds=record.elapsed_seconds,
   147          stdout=record.stdout,
   148          stderr=record.stderr,
   149          error=record.error,
   150      )
   151
   152
   153  @app.get("/health")
   154  def health() -> dict[str, str]:
   155      return {"status": "ok"}
   156
   157
   158  @app.post("/validate", response_model=ValidateResponse)
   159  def validate_mission_endpoint(
   160      request: MissionYamlRequest,
   161  ) -> ValidateResponse:
   162      result, _ = load_mission_yaml(request.mission_yaml)
   163
   164      return ValidateResponse(
   165          ok=result.ok,
   166          error=result.error,
   167      )
   168
   169
   170  @app.post("/run", response_model=RunResponse)
   171  def run_mission_endpoint(
   172      request: MissionYamlRequest,
   173      _auth: None = Depends(require_api_key),
   174  ) -> RunResponse:
   175      structural_result, mission = load_mission_yaml(
   176          request.mission_yaml
   177      )
   178
   179      if not structural_result.ok:
   180          return RunResponse(
   181              ok=False,
   182              error=structural_result.error,
   183          )
   184
   185      run_result = validate_mission_for_run(mission)
   186
   187      if not run_result.ok:
   188          return RunResponse(
   189              ok=False,
   190              error=run_result.error,
   191          )
   192
   193      preflight_error = preflight_for_execution()
   194
   195      if preflight_error is not None:
   196          return RunResponse(
   197              ok=False,
   198              error=preflight_error.message,
   199              error_detail=ErrorDetail(
   200                  **preflight_error.to_dict()
   201              ),
   202          )
   203
   204      execution_result = run_cursor_agent(mission)
   205
   206      if not execution_result.ok:
   207          return RunResponse(
   208              ok=False,
   209              stdout=execution_result.stdout,
   210              stderr=execution_result.stderr,
   211              error=execution_result.error,
   212          )
   213
   214      return RunResponse(
   215          ok=True,
   216          stdout=execution_result.stdout,
   217          stderr=execution_result.stderr,
   218      )
   219
   220
   221  @app.post("/execute", response_model=RunResponse)
   222  def execute_mission_endpoint(
   223      request: MissionYamlRequest,
   224      _auth: None = Depends(require_api_key),
   225  ) -> RunResponse:
   226      structural_result, mission = load_mission_yaml(
   227          request.mission_yaml
   228      )
   229
   230      if not structural_result.ok:
   231          return RunResponse(
   232              ok=False,
   233              error=structural_result.error,
   234          )
   235
   236      execute_result = validate_mission_for_execute(mission)
   237
   238      if not execute_result.ok:
   239          return RunResponse(
   240              ok=False,
   241              error=execute_result.error,
   242          )
   243
   244      preflight_error = preflight_for_execution()
   245
   246      if preflight_error is not None:
   247          return RunResponse(
   248              ok=False,
   249              error=preflight_error.message,
   250              error_detail=ErrorDetail(
   251                  **preflight_error.to_dict()
   252              ),
   253          )
   254
   255      execution_result = execute_cursor_agent(mission)
   256
   257      if not execution_result.ok:
   258          return RunResponse(
   259              ok=False,
   260              stdout=execution_result.stdout,
   261              stderr=execution_result.stderr,
   262              error=execution_result.error,
   263          )
   264
   265      return RunResponse(
   266          ok=True,
   267          stdout=execution_result.stdout,
   268          stderr=execution_result.stderr,
   269      )
   270
   271
   272  @app.post("/runs")
   273  def submit_run_endpoint(
   274      request: MissionYamlRequest,
   275      _auth: None = Depends(require_api_key),
   276  ) -> RunAcceptedResponse | RunResponse | JSONResponse:
   277      structural_result, mission = load_mission_yaml(
   278          request.mission_yaml
   279      )
   280
   281      if not structural_result.ok:
   282          return RunResponse(
   283              ok=False,
   284              error=structural_result.error,
   285          )
   286
   287      execute_result = validate_mission_for_execute(mission)
   288
   289      if not execute_result.ok:
   290          return RunResponse(
   291              ok=False,
   292              error=execute_result.error,
   293          )
   294
   295      preflight_error = preflight_for_execution()
   296
   297      if preflight_error is not None:
   298          return RunResponse(
   299              ok=False,
   300              error=preflight_error.message,
   301              error_detail=ErrorDetail(
   302                  **preflight_error.to_dict()
   303              ),
   304          )
   305
   306      record = run_registry.create_run()
   307      thread = threading.Thread(
   308          target=_execute_run_worker,
   309          args=(record.run_id, mission, run_registry),
   310          daemon=True,
   311      )
   312      thread.start()
   313
   314      return JSONResponse(
   315          status_code=202,
   316          content={
   317              "run_id": record.run_id,
   318              "status": RunStatus.QUEUED.value,
   319          },
   320      )
   321
   322
   323  @app.get("/runs/{run_id}", response_model=RunStatusResponse)
   324  def get_run_endpoint(
   325      run_id: str,
   326      _auth: None = Depends(require_api_key),
   327  ) -> RunStatusResponse:
   328      record = run_registry.get_run(run_id)
   329
   330      if record is None:
   331          raise HTTPException(
   332              status_code=404,
   333              detail="Run not found",
   334          )
   335
   336      return _run_status_response(record)