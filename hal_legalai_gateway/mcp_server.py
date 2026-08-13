"""Authenticated FastMCP server with namespaced thin-forward tools."""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.dependencies import get_access_token

from hal_legalai_gateway.auth import (
    build_github_oauth_provider,
    is_service_access_token,
)
from hal_legalai_gateway.config import GatewaySettings
from hal_legalai_gateway.forwarding import (
    ToolBinding,
    forward_mcp_tool,
    resolve_authorization_for_service,
)
from hal_legalai_gateway.registry import GatewayRegistry

logger = logging.getLogger(__name__)

# Settled gateway surface (Phase 2). Downstream tool names stay on the services.
DEFAULT_TOOL_BINDINGS: tuple[ToolBinding, ...] = (
    ToolBinding(
        gateway_tool="case.submit",
        namespace="case",
        downstream_service="bridge",
        downstream_tool="submit_case00",
        description=(
            "Dispatch a question-agnostic Case-00 run. Requires benchmark_id, "
            "question_id, authorization_confirmed, and an exact lowercase "
            "40-character commit_sha (mutable refs rejected). Only allowlisted "
            "benchmark/question pairs are accepted; Case-00-Triborough/Q1 routes "
            "to the existing safe generation-only path."
        ),
    ),
    ToolBinding(
        gateway_tool="case.status",
        namespace="case",
        downstream_service="bridge",
        downstream_tool="get_case00_run",
        description="Return the current GitHub status for a Case-00 mission_id.",
    ),
    ToolBinding(
        gateway_tool="case.cancel",
        namespace="case",
        downstream_service="bridge",
        downstream_tool="cancel_case00_run",
        description="Cancel the Case-00 GitHub Actions run for mission_id.",
    ),
    ToolBinding(
        gateway_tool="case.list_artifacts",
        namespace="case",
        downstream_service="bridge",
        downstream_tool="get_case00_artifacts",
        description="List and HEAD-verify durable Case-00 B2 objects for mission_id.",
    ),
    ToolBinding(
        gateway_tool="case.submit_case00_q1",
        namespace="case",
        downstream_service="bridge",
        downstream_tool="submit_case00_q1",
        description=(
            "Dispatch generation-only Case-00 Q1. Accepts the configured "
            "workflow branch (normally main), resolved immutably to HEAD of "
            "the LegalAI workflow repository, or an exact lowercase "
            "40-character commit SHA preflight-checked in that repository. "
            "GitHub Actions always receives the verified SHA."
        ),
    ),
    ToolBinding(
        gateway_tool="case.get_case00_q1_run",
        namespace="case",
        downstream_service="bridge",
        downstream_tool="get_case00_q1_run",
        description="Return the current GitHub status for a Case-00 Q1 run.",
    ),
    ToolBinding(
        gateway_tool="case.cancel_case00_q1_run",
        namespace="case",
        downstream_service="bridge",
        downstream_tool="cancel_case00_q1_run",
        description="Cancel the Case-00 Q1 GitHub Actions run for mission_id.",
    ),
    ToolBinding(
        gateway_tool="case.get_case00_q1_artifacts",
        namespace="case",
        downstream_service="bridge",
        downstream_tool="get_case00_q1_artifacts",
        description="HEAD-verify the four durable Case-00 Q1 B2 objects.",
    ),
    ToolBinding(
        gateway_tool="case.get_artifact",
        namespace="case",
        downstream_service="artifacts",
        downstream_tool="get_case_artifact",
        description=(
            "Read one allowlisted B2 artifact for a successful Case-00 mission. "
            "Filename must be exactly Q<N>_candidate_answer.json|.md for the "
            "mission's question (from Bridge run / verified B2 objects), or "
            "generation_manifest.json / model_input_audit.json."
        ),
    ),
    ToolBinding(
        gateway_tool="case.get_artifacts",
        namespace="case",
        downstream_service="artifacts",
        downstream_tool="get_artifacts",
        description="Publish proof JSON to B2, verify it, and return the object key.",
    ),
    ToolBinding(
        gateway_tool="storage.list_inventory",
        namespace="storage",
        downstream_service="storage",
        downstream_tool="list_case00_storage",
        description="List allowlisted Case-00 B2 object metadata under a canonical prefix.",
    ),
    ToolBinding(
        gateway_tool="storage.archive_feedback",
        namespace="storage",
        downstream_service="storage",
        downstream_tool="archive_case00_attorney_feedback",
        description="Archive and HEAD-verify one Case-00 attorney-feedback package.",
    ),
    ToolBinding(
        gateway_tool="storage.archive_review_packet",
        namespace="storage",
        downstream_service="storage",
        downstream_tool="archive_case00_review_packet",
        description="Archive and HEAD-verify one Case-00 attorney review-packet DOCX.",
    ),
    ToolBinding(
        gateway_tool="storage.archive_acceptance_contract",
        namespace="storage",
        downstream_service="storage",
        downstream_tool="archive_acceptance_contract",
        description=(
            "Archive and HEAD-verify one LegalAI acceptance_contract.v1 JSON "
            "object. Preferred input is structured contract (template example "
            "pass-through). Server serializes, hashes, and generates the "
            "canonical object key; legacy base64 inputs remain optional."
        ),
    ),
    ToolBinding(
        gateway_tool="storage.verify_acceptance_contract",
        namespace="storage",
        downstream_service="storage",
        downstream_tool="verify_acceptance_contract",
        description=(
            "Independently HEAD-verify one acceptance-contract object by key, "
            "size, contract_sha256, and object_sha256 (safe metadata only)."
        ),
    ),
    ToolBinding(
        gateway_tool="storage.list_acceptance_contracts",
        namespace="storage",
        downstream_service="storage",
        downstream_tool="list_acceptance_contracts",
        description=(
            "List object metadata under the canonical acceptance-contracts "
            "B2 prefix for acceptance_contract.v1 archives."
        ),
    ),
    ToolBinding(
        gateway_tool="storage.get_acceptance_contract_template",
        namespace="storage",
        downstream_service="storage",
        downstream_tool="get_acceptance_contract_template",
        description=(
            "Read-only acceptance_contract.v1 nested-identity JSON Schema, "
            "canonical hashing rules, and one synthetic non-private example "
            "passable directly as archive contract."
        ),
    ),
    ToolBinding(
        gateway_tool="storage.get_acceptance_contract",
        namespace="storage",
        downstream_service="storage",
        downstream_tool="get_acceptance_contract",
        description=(
            "Fetch and verify one acceptance_contract.v1 JSON object from "
            "canonical B2 using bounded identity fields only (benchmark_id, "
            "question_id, contract_id, version). Server generates the key and "
            "fail-closed verifies schema, identity, size, and hashes before "
            "returning safe metadata plus structured contract."
        ),
    ),
    ToolBinding(
        gateway_tool="storage.verify_archive",
        namespace="storage",
        downstream_service="storage",
        downstream_tool="list_case00_storage",
        description=(
            "Closest truthful inventory verification mapping: list allowlisted "
            "Case-00 storage object metadata (no separate verify_archive tool exists "
            "downstream)."
        ),
        notes="Maps to list_case00_storage (inventory verification).",
    ),
    ToolBinding(
        gateway_tool="mission.submit",
        namespace="mission",
        downstream_service="mission_control",
        downstream_tool="submit_run",
        description="Submit an exact Mission Control YAML document.",
    ),
    ToolBinding(
        gateway_tool="mission.submit_structured",
        namespace="mission",
        downstream_service="mission_control",
        downstream_tool="submit_structured_run",
        description="Submit a mission via structured fields.",
    ),
    ToolBinding(
        gateway_tool="mission.status",
        namespace="mission",
        downstream_service="mission_control",
        downstream_tool="get_run",
        description="Retrieve the current state of a Mission Control run.",
    ),
    ToolBinding(
        gateway_tool="mission.list_notifications",
        namespace="mission",
        downstream_service="mission_control",
        downstream_tool="list_run_notifications",
        description=(
            "List bounded, redacted Phase 2C durable notifications for a "
            "Mission Control run_id. Optional limit is safely clamped. Returns "
            "only allowlisted inspection fields; never webhook URL/secret, "
            "claim owner, raw request headers/body, or sensitive error "
            "contents. Does not mutate runs or change mission.wait/cursor."
        ),
    ),
    ToolBinding(
        gateway_tool="mission.wait",
        namespace="mission",
        downstream_service="mission_control",
        downstream_tool="wait_for_run",
        description=(
            "Wait for a Mission Control run to reach a terminal status "
            "(completed, failed, timed_out, or cancelled) or "
            "timeout_seconds. Forwards Phase 2B monitoring fields "
            "(heartbeat_health, stale_heartbeat, monitoring_history, cursor, "
            "stale_threshold_seconds) unchanged from Mission Control. "
            "Optional cursor resumes bounded history after wait_expired; "
            "omit for legacy callers. Wait expiry never mutates or cancels "
            "the run."
        ),
    ),
    ToolBinding(
        gateway_tool="mission.submit_and_wait",
        namespace="mission",
        downstream_service="mission_control",
        downstream_tool="submit_and_wait",
        description=(
            "Submit exact mission YAML and wait for a terminal run state. "
            "Returns Phase 2B monitoring fields from Mission Control. When "
            "wait_expired is true, resume with mission.wait using the same "
            "run_id and returned cursor."
        ),
    ),
    ToolBinding(
        gateway_tool="mission.run_repository_command",
        namespace="mission",
        downstream_service="mission_control",
        downstream_tool="run_repository_command",
        description="Run an allowlisted repository command via Mission Control.",
    ),
)


def bindings_from_registry(registry: GatewayRegistry) -> tuple[ToolBinding, ...]:
    """Prefer registry tool_bindings when present; else built-in settled defaults."""
    if registry.tool_bindings:
        return registry.tool_bindings
    return DEFAULT_TOOL_BINDINGS


def build_inbound_auth_provider(settings: GatewaySettings) -> AuthProvider:
    """ChatGPT-compatible GitHub OAuth (same FastMCP pattern as the Bridge)."""
    return build_github_oauth_provider(
        client_id=settings.github_oauth_client_id,
        client_secret=settings.github_oauth_client_secret,
        public_url=settings.gateway_public_url,
        jwt_signing_key=settings.jwt_signing_key,
        redis_host=settings.redis_host,
        redis_port=settings.redis_port,
        storage_encryption_key=settings.storage_encryption_key,
    )


def _require_gateway_principal(settings: GatewaySettings) -> str | None:
    """Return authorized GitHub login, or None when the caller is not allowed."""
    token = get_access_token()
    if token is None:
        return None
    if is_service_access_token(token):
        # Inbound gateway auth is user GitHub OAuth only; service tokens are
        # reserved for gateway→bridge and must not unlock the public /mcp surface.
        return None
    login = (token.claims or {}).get("login")
    if login != settings.allowed_github_login:
        return None
    return str(login)


def create_mcp_server(
    settings: GatewaySettings,
    *,
    auth: AuthProvider | None = None,
) -> FastMCP:
    """Build the gateway MCP server with thin forwarders.

    Inbound auth is FastMCP ``GitHubProvider`` (ChatGPT Business custom MCP OAuth).
    Downstream Bridge/Storage/Artifacts calls use the dedicated service credential
    from settings — never the inbound OAuth session token.
    """
    auth_provider = auth if auth is not None else build_inbound_auth_provider(settings)
    mcp = FastMCP(
        "HAL LegalAI Gateway",
        instructions=(
            "Thin authenticated router for LegalAI namespaces (case, storage, "
            "mission). Tools forward to independently deployed Bridge, Storage, "
            "artifact, and Mission Control MCP servers. No Case-00 generation, "
            "archive mutation, or mission execution logic runs inside the gateway."
        ),
        auth=auth_provider,
        mask_error_details=True,
        stateless_http=True,
        json_response=True,
    )
    bindings = bindings_from_registry(settings.registry)
    register_forwarding_tools(mcp, settings, bindings)
    return mcp


def register_forwarding_tools(
    mcp: FastMCP,
    settings: GatewaySettings,
    bindings: tuple[ToolBinding, ...],
) -> None:
    """Register one FastMCP tool per settled gateway binding."""
    by_name = {binding.gateway_tool: binding for binding in bindings}

    async def _forward(gateway_tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        binding = by_name[gateway_tool]
        if _require_gateway_principal(settings) is None:
            return {
                "ok": False,
                "gateway_tool": gateway_tool,
                "downstream_service": binding.downstream_service,
                "downstream_tool": binding.downstream_tool,
                "failure_stage": "auth",
                "duration_ms": 0.0,
                "error": {
                    "message": "authenticated GitHub user is not authorized",
                    "stage": "auth",
                },
            }
        try:
            downstream = settings.downstream_by_key(binding.downstream_service)
        except KeyError:
            return {
                "ok": False,
                "gateway_tool": gateway_tool,
                "downstream_service": binding.downstream_service,
                "downstream_tool": binding.downstream_tool,
                "failure_stage": "unconfigured",
                "duration_ms": 0.0,
                "error": {
                    "message": f"unknown downstream '{binding.downstream_service}'",
                    "stage": "unconfigured",
                },
            }
        require_auth = binding.downstream_service in {
            "bridge",
            "storage",
            "artifacts",
        }
        authorization = resolve_authorization_for_service(
            downstream_service=binding.downstream_service,
            bridge_authorization=settings.bridge_authorization,
        )
        return await forward_mcp_tool(
            binding=binding,
            arguments=arguments,
            base_url=downstream.base_url,
            authorization=authorization,
            connect_timeout_seconds=settings.connect_timeout_seconds,
            read_timeout_seconds=settings.read_timeout_seconds,
            mcp_path=settings.mcp_path_for_service(binding.downstream_service),
            require_authorization=require_auth,
            extra_secrets=settings.secret_values_for_redaction(),
        )

    # --- case ---
    @mcp.tool(name="case.submit", description=by_name["case.submit"].description)
    async def case_submit(
        commit_sha: str,
        benchmark_id: str,
        question_id: str,
        authorization_confirmed: bool,
        mission_id: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "commit_sha": commit_sha,
            "benchmark_id": benchmark_id,
            "question_id": question_id,
            "authorization_confirmed": authorization_confirmed,
        }
        if mission_id is not None:
            args["mission_id"] = mission_id
        return await _forward("case.submit", args)

    @mcp.tool(name="case.status", description=by_name["case.status"].description)
    async def case_status(mission_id: str) -> dict[str, Any]:
        return await _forward("case.status", {"mission_id": mission_id})

    @mcp.tool(name="case.cancel", description=by_name["case.cancel"].description)
    async def case_cancel(mission_id: str) -> dict[str, Any]:
        return await _forward("case.cancel", {"mission_id": mission_id})

    @mcp.tool(
        name="case.list_artifacts",
        description=by_name["case.list_artifacts"].description,
    )
    async def case_list_artifacts(mission_id: str) -> dict[str, Any]:
        return await _forward("case.list_artifacts", {"mission_id": mission_id})

    @mcp.tool(name="case.submit_case00_q1", description=by_name["case.submit_case00_q1"].description)
    async def case_submit_case00_q1(
        ref: str,
        authorization_confirmed: bool,
        mission_id: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "ref": ref,
            "authorization_confirmed": authorization_confirmed,
        }
        if mission_id is not None:
            args["mission_id"] = mission_id
        return await _forward("case.submit_case00_q1", args)

    @mcp.tool(name="case.get_case00_q1_run", description=by_name["case.get_case00_q1_run"].description)
    async def case_get_case00_q1_run(mission_id: str) -> dict[str, Any]:
        return await _forward("case.get_case00_q1_run", {"mission_id": mission_id})

    @mcp.tool(
        name="case.cancel_case00_q1_run",
        description=by_name["case.cancel_case00_q1_run"].description,
    )
    async def case_cancel_case00_q1_run(mission_id: str) -> dict[str, Any]:
        return await _forward("case.cancel_case00_q1_run", {"mission_id": mission_id})

    @mcp.tool(
        name="case.get_case00_q1_artifacts",
        description=by_name["case.get_case00_q1_artifacts"].description,
    )
    async def case_get_case00_q1_artifacts(mission_id: str) -> dict[str, Any]:
        return await _forward("case.get_case00_q1_artifacts", {"mission_id": mission_id})

    @mcp.tool(name="case.get_artifact", description=by_name["case.get_artifact"].description)
    async def case_get_artifact(
        mission_id: str,
        filename: str,
    ) -> dict[str, Any]:
        """Forward one allowlisted Case-00 artifact read.

        ``filename`` must be a bare basename: ``Q<N>_candidate_answer.json`` or
        ``.md`` for the mission's actual question, or one of the shared
        ``generation_manifest.json`` / ``model_input_audit.json`` files.
        Downstream Bridge enforces question correlation and rejects traversal.
        """
        return await _forward(
            "case.get_artifact",
            {"mission_id": mission_id, "filename": filename},
        )

    @mcp.tool(name="case.get_artifacts", description=by_name["case.get_artifacts"].description)
    async def case_get_artifacts(mission_id: str) -> dict[str, Any]:
        return await _forward("case.get_artifacts", {"mission_id": mission_id})

    # --- storage ---
    @mcp.tool(
        name="storage.list_inventory",
        description=by_name["storage.list_inventory"].description,
    )
    async def storage_list_inventory(
        category: Literal[
            "all",
            "source",
            "questions",
            "candidate_answers",
            "attorney_reviews",
            "attorney_review_packets",
        ] = "all",
        max_keys: int = 200,
    ) -> dict[str, Any]:
        return await _forward(
            "storage.list_inventory",
            {"category": category, "max_keys": max_keys},
        )

    @mcp.tool(
        name="storage.archive_feedback",
        description=by_name["storage.archive_feedback"].description,
    )
    async def storage_archive_feedback(
        evaluation_date: str,
        original_packet_md: str,
        feedback_email_md: str,
        structured_evaluation_json: str,
    ) -> dict[str, Any]:
        return await _forward(
            "storage.archive_feedback",
            {
                "evaluation_date": evaluation_date,
                "original_packet_md": original_packet_md,
                "feedback_email_md": feedback_email_md,
                "structured_evaluation_json": structured_evaluation_json,
            },
        )

    @mcp.tool(
        name="storage.archive_review_packet",
        description=by_name["storage.archive_review_packet"].description,
    )
    async def storage_archive_review_packet(
        docx_base64: str,
        recipient: str,
        question_id: str,
        sent_at: str,
        original_filename: str,
    ) -> dict[str, Any]:
        return await _forward(
            "storage.archive_review_packet",
            {
                "docx_base64": docx_base64,
                "recipient": recipient,
                "question_id": question_id,
                "sent_at": sent_at,
                "original_filename": original_filename,
            },
        )

    @mcp.tool(
        name="storage.archive_acceptance_contract",
        description=by_name["storage.archive_acceptance_contract"].description,
    )
    async def storage_archive_acceptance_contract(
        contract: dict[str, Any] | None = None,
        contract_json_base64: str = "",
        expected_benchmark_id: str = "",
        expected_question_id: str = "",
        expected_contract_id: str = "",
        expected_version: str = "",
        expected_contract_sha256: str = "",
        expected_sha256: str = "",
        expected_object_key: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if contract is not None:
            payload["contract"] = contract
        if contract_json_base64:
            payload["contract_json_base64"] = contract_json_base64
        if expected_benchmark_id:
            payload["expected_benchmark_id"] = expected_benchmark_id
        if expected_question_id:
            payload["expected_question_id"] = expected_question_id
        if expected_contract_id:
            payload["expected_contract_id"] = expected_contract_id
        if expected_version:
            payload["expected_version"] = expected_version
        if expected_contract_sha256:
            payload["expected_contract_sha256"] = expected_contract_sha256
        if expected_sha256:
            payload["expected_sha256"] = expected_sha256
        if expected_object_key:
            payload["expected_object_key"] = expected_object_key
        return await _forward(
            "storage.archive_acceptance_contract",
            payload,
        )

    @mcp.tool(
        name="storage.verify_acceptance_contract",
        description=by_name["storage.verify_acceptance_contract"].description,
    )
    async def storage_verify_acceptance_contract(
        object_key: str,
        expected_contract_sha256: str,
        expected_object_sha256: str,
        expected_size: int,
    ) -> dict[str, Any]:
        return await _forward(
            "storage.verify_acceptance_contract",
            {
                "object_key": object_key,
                "expected_contract_sha256": expected_contract_sha256,
                "expected_object_sha256": expected_object_sha256,
                "expected_size": expected_size,
            },
        )

    @mcp.tool(
        name="storage.list_acceptance_contracts",
        description=by_name["storage.list_acceptance_contracts"].description,
    )
    async def storage_list_acceptance_contracts(
        max_keys: int = 200,
    ) -> dict[str, Any]:
        return await _forward(
            "storage.list_acceptance_contracts",
            {"max_keys": max_keys},
        )

    @mcp.tool(
        name="storage.get_acceptance_contract_template",
        description=by_name["storage.get_acceptance_contract_template"].description,
    )
    async def storage_get_acceptance_contract_template() -> dict[str, Any]:
        return await _forward(
            "storage.get_acceptance_contract_template",
            {},
        )

    @mcp.tool(
        name="storage.get_acceptance_contract",
        description=by_name["storage.get_acceptance_contract"].description,
    )
    async def storage_get_acceptance_contract(
        benchmark_id: str,
        question_id: str,
        contract_id: str,
        version: str,
    ) -> dict[str, Any]:
        return await _forward(
            "storage.get_acceptance_contract",
            {
                "benchmark_id": benchmark_id,
                "question_id": question_id,
                "contract_id": contract_id,
                "version": version,
            },
        )

    @mcp.tool(
        name="storage.verify_archive",
        description=by_name["storage.verify_archive"].description,
    )
    async def storage_verify_archive(
        category: Literal[
            "all",
            "source",
            "questions",
            "candidate_answers",
            "attorney_reviews",
            "attorney_review_packets",
        ] = "all",
        max_keys: int = 200,
    ) -> dict[str, Any]:
        return await _forward(
            "storage.verify_archive",
            {"category": category, "max_keys": max_keys},
        )

    # --- mission ---
    @mcp.tool(name="mission.submit", description=by_name["mission.submit"].description)
    async def mission_submit(mission_yaml: str) -> dict[str, Any]:
        return await _forward("mission.submit", {"mission_yaml": mission_yaml})

    @mcp.tool(
        name="mission.submit_structured",
        description=by_name["mission.submit_structured"].description,
    )
    async def mission_submit_structured(
        mission_id: str,
        title: str,
        instructions: str,
        deliverables: list[str],
        create_files: bool,
        modify_files: bool,
        persistence_mode: str | None = None,
        repository_name: str = "Mission-Control",
        repository_path: str = ".",
        base_branch: str = "main",
        run_commands: bool = True,
        platform_push_approved: bool | None = None,
        allow_automatic_platform_push: bool = False,
        approval: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "mission_id": mission_id,
            "title": title,
            "instructions": instructions,
            "deliverables": deliverables,
            "create_files": create_files,
            "modify_files": modify_files,
            "repository_name": repository_name,
            "repository_path": repository_path,
            "base_branch": base_branch,
            "run_commands": run_commands,
            "allow_automatic_platform_push": allow_automatic_platform_push,
        }
        if persistence_mode is not None:
            args["persistence_mode"] = persistence_mode
        if platform_push_approved is not None:
            args["platform_push_approved"] = platform_push_approved
        if approval is not None:
            args["approval"] = approval
        return await _forward("mission.submit_structured", args)

    @mcp.tool(name="mission.status", description=by_name["mission.status"].description)
    async def mission_status(run_id: str) -> dict[str, Any]:
        return await _forward("mission.status", {"run_id": run_id})

    @mcp.tool(
        name="mission.list_notifications",
        description=(
            by_name["mission.list_notifications"].description
            or (
                "List bounded, redacted Phase 2C durable notifications for a "
                "Mission Control run_id. Optional limit is safely clamped. "
                "Returns only allowlisted inspection fields; never webhook "
                "URL/secret, claim owner, raw request headers/body, or "
                "sensitive error contents. Does not mutate runs or change "
                "mission.wait/cursor."
            )
        ),
    )
    async def mission_list_notifications(
        run_id: str,
        limit: int = 64,
    ) -> dict[str, Any]:
        return await _forward(
            "mission.list_notifications",
            {"run_id": run_id, "limit": limit},
        )

    @mcp.tool(
        name="mission.wait",
        description=(
            by_name["mission.wait"].description
            or (
                "Wait for a Mission Control run to reach a terminal status "
                "(completed, failed, timed_out, or cancelled) or "
                "timeout_seconds. Forwards Phase 2B monitoring fields "
                "(heartbeat_health, stale_heartbeat, monitoring_history, "
                "cursor, stale_threshold_seconds) unchanged from Mission "
                "Control. Optional cursor resumes bounded history after "
                "wait_expired; omit for legacy callers. Wait expiry never "
                "mutates or cancels the run."
            )
        ),
    )
    async def mission_wait(
        run_id: str,
        timeout_seconds: float = 20.0,
        poll_interval_seconds: float = 2.0,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "run_id": run_id,
            "timeout_seconds": timeout_seconds,
            "poll_interval_seconds": poll_interval_seconds,
        }
        if cursor is not None:
            args["cursor"] = cursor
        return await _forward("mission.wait", args)

    @mcp.tool(
        name="mission.submit_and_wait",
        description=(
            by_name["mission.submit_and_wait"].description
            or (
                "Submit exact mission YAML and wait for a terminal run state. "
                "Returns Phase 2B monitoring fields from Mission Control. "
                "When wait_expired is true, resume with mission.wait using "
                "the same run_id and returned cursor."
            )
        ),
    )
    async def mission_submit_and_wait(
        mission_yaml: str,
        timeout_seconds: float = 20.0,
        poll_interval_seconds: float = 2.0,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "mission_yaml": mission_yaml,
            "timeout_seconds": timeout_seconds,
            "poll_interval_seconds": poll_interval_seconds,
        }
        if cursor is not None:
            args["cursor"] = cursor
        return await _forward("mission.submit_and_wait", args)

    @mcp.tool(
        name="mission.run_repository_command",
        description=by_name["mission.run_repository_command"].description,
    )
    async def mission_run_repository_command(
        repository: str,
        ref: str,
        argv: list[str],
        working_directory: str | None = None,
        timeout_seconds: float | None = None,
        allowed_env_names: list[str] | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "repository": repository,
            "ref": ref,
            "argv": argv,
        }
        if working_directory is not None:
            args["working_directory"] = working_directory
        if timeout_seconds is not None:
            args["timeout_seconds"] = timeout_seconds
        if allowed_env_names is not None:
            args["allowed_env_names"] = allowed_env_names
        return await _forward("mission.run_repository_command", args)

    logger.info(
        "registered gateway MCP tools count=%s names=%s",
        len(bindings),
        ",".join(sorted(by_name)),
    )


async def list_registered_tool_names(mcp: FastMCP) -> list[str]:
    """Exact sorted tool names from the running FastMCP instance."""
    tools = await mcp.get_tools()
    if isinstance(tools, dict):
        return sorted(tools)
    return sorted(str(item) for item in tools)
