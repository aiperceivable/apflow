"""
Zero-import extension scanner using AST parsing

This module provides dynamic executor discovery without importing any modules.
It uses AST parsing to extract metadata from Python files, enabling fast CLI
startup and true lazy loading.

Key features:
- NO imports during scanning (uses AST only)
- Metadata cached to JSON file for performance
- Discovers executors by scanning @executor_register decorated classes
- Cache invalidation based on file modification time
"""

import ast
import json
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict, field

from apflow.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ExecutorMetadata:
    """
    Lightweight executor metadata for lazy loading

    This class holds metadata extracted from executor classes without
    importing them. Used for fast lookups and CLI listing.

    Attributes:
        id: Unique executor identifier (e.g., "rest_executor")
        name: Human-readable name (e.g., "REST Executor")
        description: Brief description of executor functionality
        module_path: Python module path (e.g., "apflow.extensions.http.rest_executor")
        class_name: Class name (e.g., "RestExecutor")
        file_path: Absolute path to Python file
        dependencies: List of required packages (e.g., ["httpx"])
        always_available: True if no external dependencies required
        tags: List of tags for categorization (e.g., ["http", "api"])
    """

    id: str
    name: str
    description: str
    module_path: str
    class_name: str
    file_path: str
    dependencies: List[str]
    always_available: bool = False
    tags: List[str] = field(default_factory=list)


class ExtensionScanner:
    """
    Zero-import extension scanner using AST parsing

    This scanner discovers executors by parsing Python files using AST
    (Abstract Syntax Tree) without importing them. This enables:

    1. Fast CLI startup: `cli --help` doesn't import any executors
    2. Lazy loading: Only import executors when actually executing tasks
    3. Auto-discovery: New executors with @executor_register are found automatically
    4. Caching: Metadata cached to JSON for fast subsequent lookups

    Usage:
        # Scan all executors (fast, no imports)
        metadata = ExtensionScanner.scan_builtin_executors()

        # Get metadata for specific executor (no import)
        meta = ExtensionScanner.get_executor_metadata("rest_executor")

        # Clear cache during development
        ExtensionScanner.clear_cache()
    """

    _metadata_cache: Dict[str, ExecutorMetadata] = {}
    _cache_file: Path = Path(__file__).parent / ".executor_cache.json"
    _scanned: bool = False

    @classmethod
    def scan_builtin_executors(cls, force_rescan: bool = False) -> Dict[str, ExecutorMetadata]:
        """
        Scan extensions directory using AST (NO IMPORTS)

        This method discovers all executors in src/apflow/extensions by:
        1. Checking cache first (fast path)
        2. If cache miss or stale, scanning Python files with AST
        3. Extracting metadata from @executor_register decorated classes
        4. Caching results to JSON file

        Args:
            force_rescan: Force rescan even if cache exists and is fresh

        Returns:
            Dictionary mapping executor_id -> ExecutorMetadata

        Example:
            >>> metadata = ExtensionScanner.scan_builtin_executors()
            >>> print(metadata["rest_executor"].dependencies)
            ['httpx']
        """
        # Try loading from cache first (fast path)
        if not force_rescan and not cls._should_rescan():
            if cls._load_from_cache():
                return cls._metadata_cache

        logger.debug("Scanning executors using AST (no imports)...")

        extensions_dir = Path(__file__).parent.parent.parent / "extensions"

        if not extensions_dir.exists():
            logger.warning(f"Extensions directory not found: {extensions_dir}")
            return {}

        # Directories to skip (non-executor extensions)
        skip_dirs = {"storage", "llm_key_config", "tools", "hooks", "__pycache__"}

        # Scan all Python files in extensions
        for py_file in extensions_dir.rglob("*.py"):
            # Skip non-executor directories
            if any(skip_dir in py_file.parts for skip_dir in skip_dirs):
                continue

            # Skip __init__.py files (no executors defined there)
            if py_file.name == "__init__.py":
                continue

            # Extract metadata using AST (no import)
            metadata_list = cls._extract_metadata_from_file(py_file)
            for metadata in metadata_list:
                cls._metadata_cache[metadata.id] = metadata
                logger.debug(f"Discovered: {metadata.id} in {py_file.name}")

        # Save to cache for next time
        cls._save_to_cache()
        cls._scanned = True

        logger.info(f"Discovered {len(cls._metadata_cache)} executors")
        return cls._metadata_cache

    @classmethod
    def _extract_metadata_from_file(cls, file_path: Path) -> List[ExecutorMetadata]:
        """
        Extract executor metadata from Python file using AST (NO IMPORT)

        Parses the file's AST looking for:
        - Classes inheriting from BaseTask
        - @executor_register() decorator
        - Class attributes: id, name, description, tags

        Args:
            file_path: Path to Python file to parse

        Returns:
            List of ExecutorMetadata extracted from the file

        Note:
            Returns empty list if file cannot be parsed or contains no executors
        """
        metadata_list = []

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                source_code = f.read()

            tree = ast.parse(source_code)

            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue

                # Check if inherits from BaseTask
                is_basetask = any(
                    isinstance(base, ast.Name) and base.id == "BaseTask" for base in node.bases
                )

                if not is_basetask:
                    continue

                # Check for @executor_register decorator
                has_decorator = any(
                    (isinstance(dec, ast.Name) and dec.id == "executor_register")
                    or (
                        isinstance(dec, ast.Call)
                        and isinstance(dec.func, ast.Name)
                        and dec.func.id == "executor_register"
                    )
                    for dec in node.decorator_list
                )

                if not has_decorator:
                    continue

                # Extract class attributes (id, name, description, tags)
                executor_id = None
                executor_name = None
                description = ""
                tags = []

                for item in node.body:
                    targets: list[ast.expr] = []
                    value_node: Optional[ast.expr] = None

                    if isinstance(item, ast.Assign):
                        targets = list(item.targets)
                        value_node = item.value
                    elif isinstance(item, ast.AnnAssign):
                        targets = [item.target]
                        value_node = item.value

                    if not targets or value_node is None:
                        continue

                    for target in targets:
                        if isinstance(target, ast.Name):
                            attr_name = target.id

                            if attr_name == "id" and isinstance(value_node, ast.Constant):
                                executor_id = value_node.value
                            elif attr_name == "name" and isinstance(value_node, ast.Constant):
                                executor_name = value_node.value
                            elif attr_name == "description":
                                # Handle both simple strings and multi-line strings
                                if isinstance(value_node, ast.Constant):
                                    description = value_node.value
                                elif isinstance(value_node, (ast.Str, ast.JoinedStr)):
                                    # Handle f-strings or complex string expressions
                                    description = ""  # Skip complex expressions
                            elif attr_name == "tags" and isinstance(value_node, ast.List):
                                tags = [
                                    elt.value
                                    for elt in value_node.elts
                                    if isinstance(elt, ast.Constant)
                                ]

                # Skip if no executor ID found
                if not executor_id:
                    logger.debug(f"Skipping {node.name} in {file_path.name}: no 'id' attribute")
                    continue

                # Build module path from file path
                module_path = cls._file_to_module_path(file_path)

                # Detect dependencies based on module path
                dependencies = cls._detect_dependencies(module_path)
                always_available = cls._is_always_available(module_path)

                metadata = ExecutorMetadata(
                    id=executor_id,
                    name=executor_name or node.name,
                    description=description,
                    module_path=module_path,
                    class_name=node.name,
                    file_path=str(file_path),
                    dependencies=dependencies,
                    always_available=always_available,
                    tags=tags,
                )

                metadata_list.append(metadata)

        except SyntaxError as e:
            logger.warning(f"Syntax error parsing {file_path}: {e}")
        except Exception as e:
            logger.warning(f"Failed to parse {file_path}: {e}")

        return metadata_list

    @classmethod
    def _file_to_module_path(cls, file_path: Path) -> str:
        """
        Convert file path to Python module path

        Args:
            file_path: Path to Python file

        Returns:
            Module path string (e.g., "apflow.extensions.http.rest_executor")

        Example:
            >>> path = Path("/path/to/apflow/extensions/http/rest_executor.py")
            >>> _file_to_module_path(path)
            "apflow.extensions.http.rest_executor"
        """
        parts = file_path.parts

        try:
            # Find the last occurrence of "apflow" in the path to handle nested apflow directories
            # e.g., /path/to/apflow/src/apflow/extensions/... -> start from the last "apflow"
            apflow_indices = [i for i, part in enumerate(parts) if part == "apflow"]

            if not apflow_indices:
                raise ValueError("No 'apflow' in path")

            # Use the last occurrence of "apflow" as the start
            apflow_index = apflow_indices[-1]

            module_parts = parts[apflow_index:-1] + (file_path.stem,)
            return ".".join(module_parts)
        except ValueError:
            logger.warning(f"Could not determine module path for {file_path}")
            return ""

    @classmethod
    def _detect_dependencies(cls, module_path: str) -> List[str]:
        """
        Detect required dependencies based on module path

        Uses heuristics to map module paths to required packages.
        This mapping should match pyproject.toml [project.optional-dependencies].

        Args:
            module_path: Python module path

        Returns:
            List of required package names

        Example:
            >>> _detect_dependencies("apflow.extensions.http.rest_executor")
            ['httpx']
        """
        dependency_map = {
            "http": ["httpx"],
        }

        for key, deps in dependency_map.items():
            if key in module_path:
                return deps

        return []

    @classmethod
    def _is_always_available(cls, module_path: str) -> bool:
        """
        Check if module has no external dependencies

        Modules that only use standard library or core apflow are always available.

        Args:
            module_path: Python module path

        Returns:
            True if module is always available (no external dependencies)
        """
        always_available_categories = {"core", "stdio", "apflow", "mcp", "generate"}
        parts = module_path.split(".")

        if "extensions" in parts:
            extensions_idx = parts.index("extensions")
            if extensions_idx + 1 < len(parts):
                category = parts[extensions_idx + 1]
                return category in always_available_categories

        return False

    @classmethod
    def _should_rescan(cls) -> bool:
        """
        Check if cache is stale and should be rescanned

        Cache is considered stale if:
        1. Cache file doesn't exist
        2. Any Python file in extensions is newer than cache

        Returns:
            True if should rescan, False if cache is fresh
        """
        if not cls._cache_file.exists():
            return True

        # Check if any Python file in extensions is newer than cache
        extensions_dir = Path(__file__).parent.parent.parent / "extensions"

        if not extensions_dir.exists():
            return False

        try:
            cache_mtime = cls._cache_file.stat().st_mtime

            for py_file in extensions_dir.rglob("*.py"):
                if py_file.stat().st_mtime > cache_mtime:
                    logger.debug(f"Cache stale: {py_file.name} modified after cache")
                    return True
        except Exception as e:
            logger.warning(f"Error checking cache freshness: {e}")
            return True

        return False

    @classmethod
    def _load_from_cache(cls) -> bool:
        """
        Load metadata from cache file

        Returns:
            True if cache loaded successfully, False otherwise
        """
        if not cls._cache_file.exists():
            return False

        try:
            with open(cls._cache_file, "r", encoding="utf-8") as f:
                cache_data = json.load(f)

            cls._metadata_cache = {
                executor_id: ExecutorMetadata(**data) for executor_id, data in cache_data.items()
            }

            cls._scanned = True
            logger.debug(f"Loaded {len(cls._metadata_cache)} executors from cache")
            return True

        except json.JSONDecodeError as e:
            logger.warning(f"Invalid cache file format: {e}")
            return False
        except Exception as e:
            logger.warning(f"Failed to load cache: {e}")
            return False

    @classmethod
    def _save_to_cache(cls) -> None:
        """
        Save metadata to cache file

        Serializes all discovered executor metadata to JSON for fast loading
        on subsequent runs.
        """
        try:
            cache_data = {
                executor_id: asdict(metadata)
                for executor_id, metadata in cls._metadata_cache.items()
            }

            # Ensure parent directory exists
            cls._cache_file.parent.mkdir(parents=True, exist_ok=True)

            with open(cls._cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, indent=2)

            logger.debug(f"Saved {len(cls._metadata_cache)} executors to cache")

        except Exception as e:
            logger.warning(f"Failed to save cache: {e}")

    @classmethod
    def get_executor_metadata(cls, executor_id: str) -> Optional[ExecutorMetadata]:
        """
        Get cached metadata for an executor (NO IMPORT)

        This method returns metadata without importing the executor module.
        Safe to call from CLI listing or help commands.

        Args:
            executor_id: Unique executor identifier

        Returns:
            ExecutorMetadata if found, None otherwise

        Example:
            >>> meta = ExtensionScanner.get_executor_metadata("rest_executor")
            >>> print(meta.dependencies)
            ['httpx']
        """
        if not cls._scanned:
            cls.scan_builtin_executors()
        return cls._metadata_cache.get(executor_id)

    @classmethod
    def get_all_executor_ids(cls) -> List[str]:
        """
        Get all discovered executor IDs (NO IMPORT)

        Returns list of all executor IDs found during scanning.
        Does not import any modules.

        Returns:
            List of executor ID strings

        Example:
            >>> ids = ExtensionScanner.get_all_executor_ids()
            >>> print(len(ids))
            17
        """
        if not cls._scanned:
            cls.scan_builtin_executors()
        return list(cls._metadata_cache.keys())

    @classmethod
    def get_all_metadata(cls) -> Dict[str, ExecutorMetadata]:
        """
        Get all executor metadata (NO IMPORT)

        Returns:
            Dictionary mapping executor_id -> ExecutorMetadata
        """
        if not cls._scanned:
            cls.scan_builtin_executors()
        return cls._metadata_cache.copy()

    @classmethod
    def get_executor_ids_by_extension(cls, extension_name: str) -> List[str]:
        """
        Get all executor IDs belonging to a specific extension (by directory name)

        Args:
            extension_name: Extension directory name (e.g., "http", "core", "apflow")

        Returns:
            List of executor IDs belonging to the extension

        Example:
            >>> ids = ExtensionScanner.get_executor_ids_by_extension("stdio")
            >>> print(ids)
            ['system_info_executor', 'command_executor']
        """
        if not cls._scanned:
            cls.scan_builtin_executors()

        result = []
        for executor_id, metadata in cls._metadata_cache.items():
            # Check if the extension name is in the module path
            # e.g., "apflow.extensions.stdio.system_info_executor" contains "stdio"
            if f".{extension_name}." in metadata.module_path or metadata.module_path.endswith(
                f".{extension_name}"
            ):
                result.append(executor_id)

        return result

    @classmethod
    def clear_cache(cls) -> None:
        """
        Clear cache file and in-memory cache

        Useful during development when executor files are modified.
        The cache will be automatically rebuilt on next scan.

        Example:
            >>> ExtensionScanner.clear_cache()
            >>> ExtensionScanner.scan_builtin_executors(force_rescan=True)
        """
        if cls._cache_file.exists():
            cls._cache_file.unlink()
            logger.info("Cleared executor cache")
        cls._metadata_cache.clear()
        cls._scanned = False


# Singleton instance for convenience
_scanner_instance = None


def get_scanner() -> ExtensionScanner:
    """
    Get the global ExtensionScanner instance

    Returns:
        Singleton ExtensionScanner instance
    """
    global _scanner_instance
    if _scanner_instance is None:
        _scanner_instance = ExtensionScanner()
    return _scanner_instance
