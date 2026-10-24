"""MCP server `sem7-risk`: the project's ML risk model as tools.

    python -m app.mcp_servers.risk_server      (stdio; scope comes from SEM7_* environment variables)
"""

from mcp.server.fastmcp import FastMCP

from app.mcp_servers import tools

mcp = FastMCP("sem7-risk", instructions="ML risk model trained on 3,625 real CI runs from 9 public repositories.")


@mcp.tool()
def score_change_risk() -> dict:
    """Score the developer's commit with the trained ML risk model.

    Returns the risk level (LOW/MEDIUM/HIGH), the failure-probability score, the main risk factors and the
    size of the change. Use it to judge how unusual/risky the change is - it is a signal, not proof of a bug.
    """
    return tools.score_change_risk(tools.RepairScope.from_env())


@mcp.tool()
def risk_model_card() -> dict:
    """Describe the risk model: chosen algorithms, held-out ROC-AUC / PR-AUC and failure rate per risk level."""
    return tools.risk_model_card()


if __name__ == "__main__":
    mcp.run()
