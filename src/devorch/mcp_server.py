"""MCP server exposing DevOrch to Claude Code / Cursor / any MCP client.

Note what is deliberately absent: there is no tool that approves a change. Approval is only recorded by a
human running ``devorch approve <task_id>`` in a terminal.
"""

from __future__ import annotations

import inspect
from functools import wraps
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError

from devorch.service import ControlPlane, ControlPlaneError
from devorch.store import StoreError

EXPECTED_ERRORS = (ControlPlaneError, StoreError)


def _tool_errors(fn):
    """Expected failures become ToolError so the model reads our message instead of a bare
    'Error executing tool' (the SDK treats any other exception as a crash)."""
    if inspect.iscoroutinefunction(fn):

        @wraps(fn)
        async def awrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except EXPECTED_ERRORS as e:
                raise ToolError(str(e)) from None

        return awrapper

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except EXPECTED_ERRORS as e:
            raise ToolError(str(e)) from None

    return wrapper

INSTRUCTIONS = """\
DevOrch is a control plane for AI-made code changes. Use it in this order:
1. devorch_analyze_task(task) → get a task_id, the risk level, the impact and the reviews/tests/approval required.
2. Implement the change yourself in the working tree (do not commit).
3. devorch_request_review(task_id) → independent reviewers check the exact diff. If status is NOT_READY, fix the
   blocking findings and call it again.
4. devorch_run_tests(task_id) → the diff is tested in a throw-away git worktree.
5. devorch_apply_changes(task_id) → commits the reviewed + tested diff to a new branch.
If it returns approval_required, STOP and give the user the command in the response's `how_to_approve` field EXACTLY as written (it is a
complete command for their terminal; do not shorten it to `devorch approve`, which may not run there). You cannot approve changes, and
approval is void if the diff changes afterwards. Never work around a blocked step.
"""


def build_server(plane: ControlPlane) -> MCPServer:
    mcp = MCPServer("devorch", instructions=INSTRUCTIONS)

    @mcp.tool(description="Describe the project: git state, available LLM providers, and the active policy.")
    @_tool_errors
    def devorch_inspect_project() -> dict[str, Any]:
        return plane.inspect_project()

    @mcp.tool(description="Analyze a development task BEFORE coding: type, risk level, impact on other files, required reviews/tests/human approval. Returns a task_id used by all other tools.")
    @_tool_errors
    def devorch_analyze_task(task: str) -> dict[str, Any]:
        return plane.analyze_task(task)

    @mcp.tool(description="Detailed risk score breakdown for a task_id.")
    @_tool_errors
    def devorch_analyze_risk(task_id: str) -> dict[str, Any]:
        return plane.get_risk(task_id)

    @mcp.tool(description="Detailed change-impact report (direct and indirectly affected files) for a task_id.")
    @_tool_errors
    def devorch_analyze_impact(task_id: str) -> dict[str, Any]:
        return plane.get_impact(task_id)

    @mcp.tool(description="Snapshot the current working-tree diff (vs HEAD) for this task. Credentials files (.env, keys) are excluded; diffs containing credential-looking strings are refused.")
    @_tool_errors
    def devorch_get_diff(task_id: str) -> dict[str, Any]:
        return plane.get_diff(task_id)

    @mcp.tool(description="Run the independent reviewers required for this task on the current diff and evaluate the result (READY / READY_WITH_WARNINGS / NOT_READY). Costs LLM tokens; limited review rounds per task.")
    @_tool_errors
    async def devorch_request_review(task_id: str) -> dict[str, Any]:
        return await plane.request_review(task_id)

    @mcp.tool(description="Apply the reviewed diff to a temporary git worktree and run the project's tests there. Refused if the review is missing or NOT_READY.")
    @_tool_errors
    async def devorch_run_tests(task_id: str) -> dict[str, Any]:
        return await plane.run_tests(task_id)

    @mcp.tool(description="Commit the reviewed and tested diff to a new branch. Requires passing review and tests, and human approval when the task needs it (the user approves with `devorch approve`). Never modifies the current branch.")
    @_tool_errors
    async def devorch_apply_changes(task_id: str) -> dict[str, Any]:
        return await plane.apply_changes(task_id)

    @mcp.resource("devorch://policy", description="Active DevOrch routing and approval policy.", mime_type="application/json")
    def policy_resource() -> dict[str, Any]:
        return plane.policy_summary()

    @mcp.resource("devorch://project", description="Project, git and provider status.", mime_type="application/json")
    def project_resource() -> dict[str, Any]:
        return plane.inspect_project()

    @mcp.resource("devorch://task/{task_id}", description="State and gate status of a task.", mime_type="application/json")
    def task_resource(task_id: str) -> dict[str, Any]:
        try:
            return plane.task_summary(task_id)
        except EXPECTED_ERRORS as e:
            raise ResourceError(str(e)) from None

    return mcp


__all__ = ["build_server", "ControlPlaneError"]
