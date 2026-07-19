     1  """Focused tests for asynchronous POST /runs and GET /runs/{run_id}."""
     2
     3  from __future__ import annotations
     4
     5  import os
     6  import textwrap
     7  import time
     8  import unittest
     9  from pathlib import Path
    10  from unittest.mock import patch
    11
    12  from fastapi.testclient import TestClient
    13
    14  import app.api as api_module
    15  from app.api import app
    16  from mission_control.executor import ExecutionResult
    17  from mission_control.run_registry import RunRegistry, RunStatus
    18
    19  REPO_ROOT = Path(__file__).resolve().parent.parent
    20  REFERENCE = REPO_ROOT / "missions" / "reference"
    21
    22  TEST_API_KEY = "mc_test_authentication_key"
    23  AUTH_HEADERS = {
    24      "Authorization": f"Bearer {TEST_API_KEY}",
    25  }
    26
    27  os.environ["MISSION_CONTROL_API_KEY"] = TEST_API_KEY
    28
    29  TERMINAL_STATUSES = {
    30      RunStatus.COMPLETED.value,
    31      RunStatus.FAILED.value,
    32      RunStatus.TIMED_OUT.value,
    33  }
    34
    35
    36  def _executable_mission_yaml() -> str:
    37      return textwrap.dedent(
    38          f"""
    39          version: 1.0
    40          mission_id: 2026-07-19-runs
    41          title: Async Run Test
    42          repository:
    43            name: Mission-Control
    44            path: {REPO_ROOT}
    45            base_branch: main
    46          execution:
    47            agent: cursor
    48            mode: execute
    49            sandbox: true
    50            worktree: false
    51          permissions:
    52            read: true
    53            create_files: true
    54            modify_files: false
    55            delete_files: false
    56            run_commands: true
    57            stage_changes: false
    58            commit: false
    59            push: false
    60          instructions: |
    61            Create a file.
    62          deliverables:
    63            - summary
    64          approval:
    65            execute_without_approval: true
    66            commit_requires_approval: true
    67            push_requires_approval: true
    68          """
    69      )
    70
    71
    72  class TestRunsApi(unittest.TestCase):
    73      def setUp(self) -> None:
    74          api_module.run_registry = RunRegistry()
    75          self.client = TestClient(
    76              app,
    77              headers=AUTH_HEADERS,
    78          )
    79
    80      def _wait_for_terminal(self, run_id: str, timeout: float = 2.0) -> dict:
    81          deadline = time.time() + timeout
    82          body: dict | None = None
    83
    84          while time.time() < deadline:
    85              response = self.client.get(f"/runs/{run_id}")
    86              self.assertEqual(response.status_code, 200)
    87              body = response.json()
    88              if body["status"] in TERMINAL_STATUSES:
    89                  return body
    90              time.sleep(0.01)
    91
    92          self.fail(
    93              f"run {run_id} did not reach a terminal status; last={body}"
    94          )
    95
    96      @patch("app.api.preflight_for_execution", return_value=None)
    97      @patch("app.api.execute_cursor_agent")
    98      def test_post_runs_accepts_and_returns_queued(
    99          self,
   100          mock_execute,
   101          _mock_preflight,
   102      ) -> None:
   103          mock_execute.return_value = ExecutionResult(
   104              ok=True,
   105              stdout="done\n",
   106          )
   107
   108          response = self.client.post(
   109              "/runs",
   110              json={"mission_yaml": _executable_mission_yaml()},
   111          )
   112
   113          self.assertEqual(response.status_code, 202)
   114          body = response.json()
   115          self.assertIn("run_id", body)
   116          self.assertEqual(body["status"], "queued")
   117
   118          self._wait_for_terminal(body["run_id"])
   119          mock_execute.assert_called_once()
   120
   121      @patch("app.api.preflight_for_execution", return_value=None)
   122      @patch("app.api.execute_cursor_agent")
   123      def test_get_run_reports_completed(
   124          self,
   125          mock_execute,
   126          _mock_preflight,
   127      ) -> None:
   128          mock_execute.return_value = ExecutionResult(
   129              ok=True,
   130              stdout="agent response\n",
   131          )
   132
   133          submit = self.client.post(
   134              "/runs",
   135              json={"mission_yaml": _executable_mission_yaml()},
   136          )
   137          run_id = submit.json()["run_id"]
   138          body = self._wait_for_terminal(run_id)
   139
   140          self.assertEqual(body["status"], "completed")
   141          self.assertEqual(body["stdout"], "agent response\n")
   142          self.assertEqual(body["stderr"], "")
   143          self.assertIsNone(body["error"])
   144          self.assertIsNotNone(body["started_at"])
   145          self.assertIsNotNone(body["completed_at"])
   146          self.assertIsNotNone(body["elapsed_seconds"])
   147
   148      @patch("app.api.preflight_for_execution", return_value=None)
   149      @patch("app.api.execute_cursor_agent")
   150      def test_get_run_reports_failed(
   151          self,
   152          mock_execute,
   153          _mock_preflight,
   154      ) -> None:
   155          mock_execute.return_value = ExecutionResult(
   156              ok=False,
   157              stderr="agent failed",
   158              error="agent failed",
   159          )
   160
   161          submit = self.client.post(
   162              "/runs",
   163              json={"mission_yaml": _executable_mission_yaml()},
   164          )
   165          body = self._wait_for_terminal(submit.json()["run_id"])
   166
   167          self.assertEqual(body["status"], "failed")
   168          self.assertEqual(body["stderr"], "agent failed")
   169          self.assertEqual(body["error"], "agent failed")
   170
   171      @patch("app.api.preflight_for_execution", return_value=None)
   172      @patch("app.api.execute_cursor_agent")
   173      def test_get_run_reports_timed_out(
   174          self,
   175          mock_execute,
   176          _mock_preflight,
   177      ) -> None:
   178          mock_execute.return_value = ExecutionResult(
   179              ok=False,
   180              error="cursor-agent timed out after 600 seconds",
   181          )
   182
   183          submit = self.client.post(
   184              "/runs",
   185              json={"mission_yaml": _executable_mission_yaml()},
   186          )
   187          body = self._wait_for_terminal(submit.json()["run_id"])
   188
   189          self.assertEqual(body["status"], "timed_out")
   190          self.assertIn("timed out", body["error"])
   191
   192      def test_post_runs_rejects_invalid_mission(self) -> None:
   193          mission_yaml = (REFERENCE / "invalid-bad-version.yaml").read_text(
   194              encoding="utf-8"
   195          )
   196          response = self.client.post(
   197              "/runs",
   198              json={"mission_yaml": mission_yaml},
   199          )
   200
   201          self.assertEqual(response.status_code, 200)
   202          body = response.json()
   203          self.assertFalse(body["ok"])
   204          self.assertIn("Unsupported version", body["error"])
   205          self.assertEqual(len(api_module.run_registry._runs), 0)
   206
   207      @patch("app.api.preflight_for_execution")
   208      def test_post_runs_rejects_preflight_failure(
   209          self,
   210          mock_preflight,
   211      ) -> None:
   212          from app.cursor_cli import StructuredError
   213
   214          mock_preflight.return_value = StructuredError(
   215              code="CURSOR_API_KEY_MISSING",
   216              message="CURSOR_API_KEY environment variable is not set",
   217              stage="preflight",
   218          )
   219
   220          response = self.client.post(
   221              "/runs",
   222              json={"mission_yaml": _executable_mission_yaml()},
   223          )
   224
   225          self.assertEqual(response.status_code, 200)
   226          body = response.json()
   227          self.assertFalse(body["ok"])
   228          self.assertEqual(
   229              body["error_detail"]["code"],
   230              "CURSOR_API_KEY_MISSING",
   231          )
   232          self.assertEqual(len(api_module.run_registry._runs), 0)
   233
   234      def test_get_unknown_run_returns_404(self) -> None:
   235          response = self.client.get("/runs/missing-run-id")
   236
   237          self.assertEqual(response.status_code, 404)
   238          self.assertEqual(response.json()["detail"], "Run not found")
   239
   240      def test_post_runs_requires_auth(self) -> None:
   241          client = TestClient(app)
   242          response = client.post(
   243              "/runs",
   244              json={"mission_yaml": _executable_mission_yaml()},
   245          )
   246
   247          self.assertEqual(response.status_code, 401)
   248
   249      def test_get_run_requires_auth(self) -> None:
   250          client = TestClient(app)
   251          response = client.get("/runs/some-id")
   252
   253          self.assertEqual(response.status_code, 401)
   254
   255
   256  if __name__ == "__main__":
   257      unittest.main()