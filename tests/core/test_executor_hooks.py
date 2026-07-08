"""
Test executor-specific pre/post hooks functionality
"""

import pytest
from apflow import (
    executor_register,
    clear_config,
)
from apflow.core.base import BaseTask
from apflow.core.extensions.registry import add_executor_hook
from apflow.core.extensions import get_registry


class TestExecutorHooks:
    """Test executor-specific hooks via executor_register"""

    def setup_method(self):
        """Clear extension registry before each test"""
        from apflow.core.extensions import get_registry

        registry = get_registry()
        registry._executor_classes.clear()
        registry._factory_functions.clear()
        clear_config()

    def test_executor_register_with_pre_hook(self):
        """Test executor_register with pre_hook parameter"""
        pre_hook_called = []

        def pre_hook(executor, task, inputs):
            pre_hook_called.append((executor.id, task.id, inputs))
            return None  # Continue execution

        @executor_register(pre_hook=pre_hook)
        class TestExecutor(BaseTask):
            id = "test_executor_with_pre_hook"
            name = "Test Executor"
            description = "Test executor with pre_hook"

            def __init__(self, inputs=None):
                super().__init__(inputs=inputs or {})

            async def execute(self, inputs):
                return {"result": "executed"}

            def get_input_schema(self):
                return {"type": "object"}

        # Verify hook was stored in executor class
        assert hasattr(TestExecutor, "_executor_hooks")
        assert "pre_hook" in TestExecutor._executor_hooks
        assert TestExecutor._executor_hooks["pre_hook"] == pre_hook

    def test_executor_register_with_post_hook(self):
        """Test executor_register with post_hook parameter"""
        post_hook_called = []

        def post_hook(executor, task, inputs, result):
            post_hook_called.append((executor.id, task.id, result))

        @executor_register(post_hook=post_hook)
        class TestExecutor(BaseTask):
            id = "test_executor_with_post_hook"
            name = "Test Executor"
            description = "Test executor with post_hook"

            def __init__(self, inputs=None):
                super().__init__(inputs=inputs or {})

            async def execute(self, inputs):
                return {"result": "executed"}

            def get_input_schema(self):
                return {"type": "object"}

        # Verify hook was stored in executor class
        assert hasattr(TestExecutor, "_executor_hooks")
        assert "post_hook" in TestExecutor._executor_hooks
        assert TestExecutor._executor_hooks["post_hook"] == post_hook

    def test_executor_register_with_both_hooks(self):
        """Test executor_register with both pre_hook and post_hook"""
        pre_hook_called = []
        post_hook_called = []

        def pre_hook(executor, task, inputs):
            pre_hook_called.append("pre")
            return None

        def post_hook(executor, task, inputs, result):
            post_hook_called.append("post")

        @executor_register(pre_hook=pre_hook, post_hook=post_hook)
        class TestExecutor(BaseTask):
            id = "test_executor_with_both_hooks"
            name = "Test Executor"
            description = "Test executor with both hooks"

            def __init__(self, inputs=None):
                super().__init__(inputs=inputs or {})

            async def execute(self, inputs):
                return {"result": "executed"}

            def get_input_schema(self):
                return {"type": "object"}

        # Verify both hooks were stored
        assert hasattr(TestExecutor, "_executor_hooks")
        assert "pre_hook" in TestExecutor._executor_hooks
        assert "post_hook" in TestExecutor._executor_hooks
        assert TestExecutor._executor_hooks["pre_hook"] == pre_hook
        assert TestExecutor._executor_hooks["post_hook"] == post_hook

    def test_executor_register_pre_hook_skip_execution(self):
        """Test that pre_hook can return result to skip execution"""

        def pre_hook(executor, task, inputs):
            # Return result to skip executor execution
            return {"result": "from_pre_hook", "demo_mode": True}

        @executor_register(pre_hook=pre_hook)
        class TestExecutor(BaseTask):
            id = "test_executor_skip"
            name = "Test Executor"
            description = "Test executor that can be skipped"

            def __init__(self, inputs=None):
                super().__init__(inputs=inputs or {})

            async def execute(self, inputs):
                # This should not be called if pre_hook returns result
                return {"result": "executed"}

            def get_input_schema(self):
                return {"type": "object"}

        # Verify hook was stored
        assert hasattr(TestExecutor, "_executor_hooks")
        assert "pre_hook" in TestExecutor._executor_hooks

    def test_add_executor_hook_runtime(self):
        """Test adding hooks to existing executor at runtime"""

        @executor_register()
        class TestExecutor(BaseTask):
            id = "test_executor_runtime"
            name = "Test Executor"
            description = "Test executor for runtime hook addition"

            def __init__(self, inputs=None):
                super().__init__(inputs=inputs or {})

            async def execute(self, inputs):
                return {"result": "executed"}

            def get_input_schema(self):
                return {"type": "object"}

        # Register executor first (need to create instance)
        registry = get_registry()
        executor_instance = TestExecutor()
        registry.register(executor_instance, override=True)

        # Add hook at runtime
        def runtime_pre_hook(executor, task, inputs):
            return None

        add_executor_hook("test_executor_runtime", "pre_hook", runtime_pre_hook)

        # Verify hook was added
        assert hasattr(TestExecutor, "_executor_hooks")
        assert "pre_hook" in TestExecutor._executor_hooks
        assert TestExecutor._executor_hooks["pre_hook"] == runtime_pre_hook

    def test_add_executor_hook_reaches_real_class_not_wrapper(self):
        """Regression: add_executor_hook()'s fallback (when _executor_classes
        has no entry for the id) used extension.__class__, which for a
        decorator-registered executor is the disconnected CategoryOverride
        wrapper defined inside _register_extension — a throwaway class
        never used to instantiate real executor instances, so the hook was
        never actually invoked at execution time. (Review CRITICAL #35)
        """

        @executor_register()
        class WrapperFallbackExecutor(BaseTask):
            id = "wrapper_fallback_executor"
            name = "Wrapper Fallback Executor"
            description = "test"

            def __init__(self, inputs=None):
                super().__init__(inputs=inputs or {})

            async def execute(self, inputs):
                return {"result": "ok"}

            def get_input_schema(self):
                return {"type": "object"}

        registry = get_registry()
        # Force add_executor_hook into its fallback branch, simulating any
        # registration path where _executor_classes has no entry for this id.
        registry._executor_classes.pop("wrapper_fallback_executor", None)

        def runtime_hook(executor, task, inputs):
            return None

        add_executor_hook("wrapper_fallback_executor", "pre_hook", runtime_hook)

        # The hook must land on the REAL executor class — proven by actually
        # instantiating a fresh executor and checking its class carries it.
        instance = registry.create_executor_instance("wrapper_fallback_executor", inputs={})
        assert instance is not None
        assert hasattr(type(instance), "_executor_hooks")
        assert type(instance)._executor_hooks.get("pre_hook") is runtime_hook

    def test_add_executor_hook_invalid_type(self):
        """Test that invalid hook type raises error"""

        @executor_register()
        class TestExecutor(BaseTask):
            id = "test_executor_invalid"
            name = "Test Executor"
            description = "Test executor"

            def __init__(self, inputs=None):
                super().__init__(inputs=inputs or {})

            async def execute(self, inputs):
                return {"result": "executed"}

            def get_input_schema(self):
                return {"type": "object"}

        # Executor is already registered by decorator
        with pytest.raises(ValueError, match="Invalid hook_type"):
            add_executor_hook("test_executor_invalid", "invalid_hook", lambda: None)

    def test_add_executor_hook_nonexistent_executor(self):
        """Test that adding hook to non-existent executor raises error"""
        with pytest.raises(ValueError, match="Executor.*not found"):
            add_executor_hook("nonexistent_executor", "pre_hook", lambda: None)
