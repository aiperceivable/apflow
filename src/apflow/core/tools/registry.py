"""
Tool Registry and Resolution

Provides ToolRegistry for tool registration and resolve_tool for converting
string tool references to callable tool objects.
"""

import ast
from inspect import isfunction, isclass
from typing import Dict, Any, Optional
from apflow.logger import get_logger

logger = get_logger(__name__)


class ToolRegistry:
    """
    Registry for tools that can be referenced by string name

    This registry allows tools to be registered and then referenced by string
    in agent configurations, enabling dynamic tool loading.
    """

    _instance: Optional["ToolRegistry"] = None
    _tools: Dict[str, Any] = {}

    def __new__(cls):
        """Singleton pattern"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._tools = {}
        return cls._instance

    def register(self, name: str, tool: Any, override: bool = False) -> Any:
        """
        Register a tool in the registry

        Args:
            name: Tool name (string identifier)
            tool: Tool object (class, function, or instance)
            override: If True, always force override any previous registration.


        """
        if name in self._tools and not override:
            logger.warning(
                f"Tool '{name}' is already registered. "
                f"Use override=True to replace it, or use a different name."
            )
            return self._tools[name]

        self._tools[name] = tool
        logger.debug(f"Registered tool '{name}' in ToolRegistry")
        return tool

    def get(self, name: str) -> Optional[Any]:
        """
        Get a tool from the registry

        Args:
            name: Tool name

        Returns:
            Tool object or None if not found
        """
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        """
        List all registered tool names

        Returns:
            List of registered tool names
        """
        return list(self._tools.keys())


# Global registry instance
_registry = ToolRegistry()


def get_tool_registry() -> ToolRegistry:
    """
    Get the global tool registry instance

    Returns:
        ToolRegistry singleton instance
    """
    return _registry


def register_tool(name: str, tool: Any, override: bool = False) -> Any:
    """
    Register a tool in the global registry (convenience function)

    Args:
        name: Tool name (string identifier)
        tool: Tool object (class, function, or instance)
        override: If True, allow overriding existing registration

    Example:
        from apflow.core.tools import register_tool, BaseTool

        class MyTool(BaseTool):
            def _run(self, arg: str) -> str:
                return f"Result: {arg}"

        register_tool("my_tool", MyTool)
        # Now can use "my_tool()" in agent tools config
    """
    return _registry.register(name, tool, override=override)


def resolve_tool(tool_ref: Any) -> Any:
    """
    Resolve tool reference to callable tool object

    Handles resolution of:
    - String tool references (e.g., "SerperDevTool()") to tool instances
    - Already callable objects (functions, classes, instances) - returns as-is
    - Tool objects (has run/execute/call methods) - returns as-is

    The function first checks the tool registry, then tries other sources,
    then searches in the calling frame's globals.

    Args:
        tool_ref: Tool reference - can be string, function, class, or tool instance

    Returns:
        Callable tool object

    Raises:
        ValueError: If string format is invalid
        NameError: If tool name is not found
        TypeError: If input type is unsupported

    Example:
        # From registry
        register_tool("my_tool", MyTool)
        tool = resolve_tool("my_tool()")

        # From external tools
        tool = resolve_tool("SerperDevTool()")

        # Already a tool object
        tool = resolve_tool(SerperDevTool())
    """
    # If input is function, class, or instance, return directly
    if isfunction(tool_ref) or isclass(tool_ref) or callable(tool_ref):
        logger.debug(
            f"resolve_tool: Direct return (function/class/callable): {type(tool_ref).__name__}"
        )
        return tool_ref

    # Check if it's a tool object (has run method or similar)
    if hasattr(tool_ref, "run") or hasattr(tool_ref, "execute") or hasattr(tool_ref, "call"):
        logger.debug(
            f"resolve_tool: Direct return (tool object with run/execute/call): {type(tool_ref).__name__}"
        )
        return tool_ref

    # Check if it's any other object - return it as is
    if hasattr(tool_ref, "__dict__") or hasattr(tool_ref, "__slots__"):
        logger.debug(
            f"resolve_tool: Direct return (object with __dict__/__slots__): {type(tool_ref).__name__}"
        )
        return tool_ref

    # If input is string, parse to callable object
    if isinstance(tool_ref, str):
        # Auto-add () if it's a simple class name (not a function call format)
        if "(" not in tool_ref.strip():
            tool_ref = f"{tool_ref.strip()}()"

        # Parse string to AST
        try:
            tree = ast.parse(tool_ref, mode="eval")
            expr = tree.body
        except SyntaxError as e:
            raise ValueError(f"Invalid tool string syntax: {tool_ref}. Error: {e}")

        # Check if it is function call expression
        if not isinstance(expr, ast.Call):
            raise ValueError(f"Invalid function call: {tool_ref}")

        # Extract function name
        if hasattr(expr.func, "id") and getattr(expr.func, "id", None):
            func_name = getattr(expr.func, "id")  # type: ignore
        elif hasattr(expr.func, "attr") and getattr(expr.func, "attr", None):
            func_name = getattr(expr.func, "attr")  # type: ignore
        else:
            raise ValueError(f"Could not extract function name from {expr.func}")

        # Get function/class object
        try:
            logger.debug(
                f"resolve_tool: Resolving string tool reference '{tool_ref}' (extracted name: '{func_name}')"
            )

            # Method 1: Try to get from tool registry first
            registry = get_tool_registry()
            func = registry.get(func_name)
            if func is not None:
                logger.info(
                    f"resolve_tool: Method 1 (registry) - Found '{func_name}' in tool registry"
                )
                # Parse positional arguments
                args = []
                for arg in expr.args:
                    args.append(ast.literal_eval(arg))

                # Parse keyword arguments
                kwargs = {}
                for kw in expr.keywords:
                    key = kw.arg
                    value = ast.literal_eval(kw.value)
                    kwargs[key] = value

                # Return tool instance directly
                if not args and not kwargs:
                    # For tool classes, instantiate them
                    if hasattr(func, "__bases__") and any(
                        "BaseTool" in str(base) for base in func.__bases__
                    ):
                        logger.debug(
                            f"resolve_tool: Instantiating tool class '{func_name}' from registry"
                        )
                        return func()
                    logger.debug(
                        f"resolve_tool: Returning tool class/function '{func_name}' from registry"
                    )
                    return func
                logger.debug(
                    f"resolve_tool: Calling '{func_name}' from registry with args={args}, kwargs={kwargs}"
                )
                return func(*args, **kwargs)

            # Method 2: Try to get from current module globals
            # This requires the caller to have the function in their scope
            logger.debug(
                f"resolve_tool: Method 3 (globals) - Searching for '{func_name}' in calling frame globals"
            )
            import inspect

            frame = inspect.currentframe()
            frame_depth = 0
            while frame:
                frame_depth += 1
                if func_name in frame.f_globals:
                    func = frame.f_globals[func_name]
                    logger.info(
                        f"resolve_tool: Method 3 (globals) - Found '{func_name}' in frame {frame_depth} globals"
                    )

                    # If no arguments, return the function object itself
                    if not expr.args and not expr.keywords:
                        # For tool classes, instantiate them
                        if hasattr(func, "__bases__") and any(
                            "BaseTool" in str(base) for base in func.__bases__
                        ):
                            logger.debug(
                                f"resolve_tool: Instantiating tool class '{func_name}' from globals"
                            )
                            return func()
                        logger.debug(
                            f"resolve_tool: Returning tool class/function '{func_name}' from globals"
                        )
                        return func

                    # If there are arguments, return the function call result
                    # Parse positional arguments
                    args = []
                    for arg in expr.args:
                        args.append(ast.literal_eval(arg))

                    # Parse keyword arguments
                    kwargs = {}
                    for kw in expr.keywords:
                        key = kw.arg
                        value = ast.literal_eval(kw.value)
                        kwargs[key] = value

                    # Return tool instance directly
                    logger.debug(
                        f"resolve_tool: Calling '{func_name}' from globals with args={args}, kwargs={kwargs}"
                    )
                    return func(*args, **kwargs)
                frame = frame.f_back

            raise NameError(
                f"Tool '{func_name}' not found in registry or current scope. "
                f"Registered tools: {registry.list_tools()}"
            )

        except Exception as e:
            raise NameError(f"Tool '{func_name}' not found: {str(e)}")

    # Other types raise exception
    raise TypeError(
        f"Unsupported type: {type(tool_ref)}, "
        f"only support string, function, class, and object instances"
    )


__all__ = [
    "ToolRegistry",
    "get_tool_registry",
    "register_tool",
    "resolve_tool",
]
