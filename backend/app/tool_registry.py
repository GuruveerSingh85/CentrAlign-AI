
def execute_tool(
    capability: str,
    context: dict | None = None,
) -> dict:
    """Execute only explicitly registered tools."""
    tool = TOOLS.get(capability)

    if tool is None:
        return {
            "success": False,
            "error": f"Unsupported capability: {capability}",
            "requires_input": True,
        }

    try:
        if capability == "check_policy":
            return tool(context or {})

        return tool()

    except (OSError, UnicodeError) as exc:
        return {
            "success": False,
            "error": f"Tool execution failed: {exc}",
            "requires_input": True,
        }
