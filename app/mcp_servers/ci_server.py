"""MCP server `sem7-ci`: code reading, dry-run testing and past-failure lookup for one repair.

    python -m app.mcp_servers.ci_server        (stdio; scope comes from SEM7_* environment variables)
"""

from mcp.server.fastmcp import FastMCP

from app.mcp_servers import tools

mcp = FastMCP("sem7-ci", instructions="Read code, dry-run a candidate fix against the real tests, "
                                      "and look up similar past failures in the workflow database.")


@mcp.tool()
def read_source(path: str, function: str = "", version: str = "current") -> dict:
    """Read a repository file, or just one function from it.

    Args:
        path: repository-relative file path, e.g. "src/calculator.py"
        function: optional function name, e.g. "divide"; empty = whole file
        version: "current" (code a fix will edit), "baseline" (healthy version before the commit)
                 or "commit" (the developer's commit)
    """
    return tools.read_source(tools.RepairScope.from_env(), path, function, version)


@mcp.tool()
def run_tests_with_patch(path: str, function: str, new_source: str) -> dict:
    """Dry-run a candidate fix: replace one function in a throwaway copy and run the real test suite.

    The repository is not changed. Returns PASS/FAIL with counts and the failing tests.

    Args:
        path: source file, e.g. "src/calculator.py" (tests/config/dependency files are refused)
        function: name of the function to replace
        new_source: complete new source of that function, starting at its `def` line
    """
    return tools.run_tests_with_patch(tools.RepairScope.from_env(), path, function, new_source)


@mcp.tool()
def past_failures(query: str, limit: int = 5) -> dict:
    """Search earlier workflows (the project database) for root causes or fixes matching the query.

    Args:
        query: words to look for, e.g. "divide floor division zero"
        limit: maximum number of matches
    """
    return tools.past_failures(query, limit)


if __name__ == "__main__":
    mcp.run()
