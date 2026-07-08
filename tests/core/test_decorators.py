"""
Test decorators functionality

Tests for the unified decorators system (Flask-style API):
- register_pre_hook
- register_post_hook
- set_task_model_class / get_task_model_class
- executor_register
"""

import pytest
from apflow import (
    register_pre_hook,
    register_post_hook,
    set_task_model_class,
    get_task_model_class,
    clear_config,
    executor_register,
)
from apflow.core.storage.sqlalchemy.models import TaskModel
from apflow.core.extensions.types import ExtensionCategory
from apflow.logger import get_logger

logger = get_logger(__name__)


class TestConfigDecorators:
    """Test configuration decorators (hooks, task_model_class)"""

    def setup_method(self):
        """Clear config registry before each test"""
        clear_config()

    def test_register_pre_hook_as_decorator(self):
        """Test @register_pre_hook decorator syntax"""
        hook_called = []

        @register_pre_hook
        async def my_pre_hook(task):
            hook_called.append(task.id)

        # Verify hook was registered
        from apflow.core.config import get_pre_hooks

        hooks = get_pre_hooks()
        assert len(hooks) == 1
        assert hooks[0] == my_pre_hook

        # Verify the function is still callable
        assert callable(my_pre_hook)

    def test_register_pre_hook_as_function(self):
        """Test register_pre_hook() function call syntax"""
        hook_called = []

        async def my_pre_hook(task):
            hook_called.append(task.id)

        # Register using function call
        register_pre_hook(my_pre_hook)

        # Verify hook was registered
        from apflow.core.config import get_pre_hooks

        hooks = get_pre_hooks()
        assert len(hooks) == 1
        assert hooks[0] == my_pre_hook

    def test_register_post_hook_as_decorator(self):
        """Test @register_post_hook decorator syntax"""
        hook_called = []

        @register_post_hook
        async def my_post_hook(task, inputs, result):
            hook_called.append((task.id, result))

        # Verify hook was registered
        from apflow.core.config import get_post_hooks

        hooks = get_post_hooks()
        assert len(hooks) == 1
        assert hooks[0] == my_post_hook

        # Verify the function is still callable
        assert callable(my_post_hook)

    def test_register_post_hook_as_function(self):
        """Test register_post_hook() function call syntax"""
        hook_called = []

        async def my_post_hook(task, inputs, result):
            hook_called.append((task.id, result))

        # Register using function call
        register_post_hook(my_post_hook)

        # Verify hook was registered
        from apflow.core.config import get_post_hooks

        hooks = get_post_hooks()
        assert len(hooks) == 1
        assert hooks[0] == my_post_hook

    def test_multiple_pre_hooks(self):
        """Test registering multiple pre-hooks"""

        @register_pre_hook
        async def hook1(task):
            pass

        @register_pre_hook
        async def hook2(task):
            pass

        @register_pre_hook
        async def hook3(task):
            pass

        from apflow.core.config import get_pre_hooks

        hooks = get_pre_hooks()
        assert len(hooks) == 3
        assert hooks == [hook1, hook2, hook3]

    def test_multiple_post_hooks(self):
        """Test registering multiple post-hooks"""

        @register_post_hook
        async def hook1(task, inputs, result):
            pass

        @register_post_hook
        async def hook2(task, inputs, result):
            pass

        from apflow.core.config import get_post_hooks

        hooks = get_post_hooks()
        assert len(hooks) == 2
        assert hooks == [hook1, hook2]

    def test_sync_hooks(self):
        """Test registering synchronous hooks"""

        @register_pre_hook
        def sync_pre_hook(task):
            pass

        @register_post_hook
        def sync_post_hook(task, inputs, result):
            pass

        from apflow.core.config import get_pre_hooks, get_post_hooks

        assert len(get_pre_hooks()) == 1
        assert len(get_post_hooks()) == 1

    def test_set_and_get_task_model_class(self):
        """Test set_task_model_class and get_task_model_class"""
        # Test that we can set and get TaskModel (using default)
        # Since set_task_model_class requires TaskModel subclass,
        # we'll test with the default TaskModel itself

        # Set to default TaskModel explicitly
        set_task_model_class(TaskModel)

        # Get and verify
        retrieved_class = get_task_model_class()
        assert retrieved_class == TaskModel

        # Test that get returns the same instance after multiple calls
        assert get_task_model_class() == TaskModel

    def test_task_model_class_default(self):
        """Test that default TaskModel is returned when not set"""
        # Clear any custom model
        clear_config()

        # Get default model
        model_class = get_task_model_class()
        assert model_class == TaskModel

    @pytest.mark.asyncio
    async def test_hooks_with_agent_executor(self):
        """Test that hooks registered via decorators work with AgentExecutor"""
        try:
            from apflow.api.a2a.agent_executor import AIPartnerUpFlowAgentExecutor
        except ImportError:
            pytest.skip("a2a module not available, skipping AgentExecutor test")
            return

        pre_hook_called = []
        post_hook_called = []

        @register_pre_hook
        async def test_pre_hook(task):
            pre_hook_called.append(task.id)

        @register_post_hook
        async def test_post_hook(task, inputs, result):
            post_hook_called.append((task.id, result))

        # Create executor (should pick up hooks from registry)
        executor = AIPartnerUpFlowAgentExecutor()

        # Verify hooks were loaded
        assert len(executor.pre_hooks) == 1
        assert len(executor.post_hooks) == 1
        assert executor.pre_hooks[0] == test_pre_hook
        assert executor.post_hooks[0] == test_post_hook

    @pytest.mark.asyncio
    async def test_hooks_with_a2a_server(self):
        """Test that hooks registered via decorators work with create_a2a_server"""
        try:
            from apflow.api.a2a.server import create_a2a_server
        except ImportError:
            pytest.skip("a2a module not available, skipping create_a2a_server test")
            return

        pre_hook_called = []

        @register_pre_hook
        async def test_pre_hook(task):
            pre_hook_called.append(task.id)

        # Create server (should pick up hooks from registry)
        server = create_a2a_server(
            verify_token_secret_key=None,
            base_url="http://localhost:8000",
        )

        # Verify server was created (hooks are used internally)
        assert server is not None


class TestExtensionDecorator:
    """Test @executor_register decorator"""

    def setup_method(self):
        """Clear extension registry before each test"""
        from apflow.core.extensions import get_registry

        registry = get_registry()
        # Clear all registrations
        registry._executor_classes.clear()
        registry._factory_functions.clear()
        # Note: _extensions is a dict keyed by extension.id, not a list
        # We'll let tests register their own extensions

    def test_executor_register_decorator(self):
        """Test @executor_register decorator"""
        from apflow.core.base import BaseTask

        @executor_register()
        class TestExecutor(BaseTask):
            id = "test_executor"
            name = "Test Executor"
            description = "Test executor for decorator testing"
            category = ExtensionCategory.EXECUTOR

            def __init__(self, inputs=None):
                super().__init__(inputs=inputs or {})

            async def execute(self, inputs):
                return {"result": "test"}

            def get_input_schema(self):
                return {"type": "object"}

        # Verify extension was registered
        from apflow.core.extensions import get_registry

        registry = get_registry()

        # Check if executor can be retrieved
        executor_instance = registry.create_executor_instance("test_executor", inputs={})
        assert executor_instance is not None
        assert executor_instance.id == "test_executor"
        assert executor_instance.name == "Test Executor"

    def test_executor_register_with_factory(self):
        """Test @executor_register with custom factory"""
        from apflow.core.base import BaseTask

        def custom_factory(inputs):
            executor = TestExecutorWithFactory(inputs=inputs)
            executor.custom_initialized = True
            return executor

        @executor_register(factory=custom_factory)
        class TestExecutorWithFactory(BaseTask):
            id = "test_executor_factory"
            name = "Test Executor Factory"
            description = "Test executor with custom factory"
            category = ExtensionCategory.EXECUTOR
            custom_initialized = False

            def __init__(self, inputs=None):
                super().__init__(inputs=inputs or {})

            async def execute(self, inputs):
                return {"result": "test"}

            def get_input_schema(self):
                return {"type": "object"}

        # Verify extension was registered with custom factory
        from apflow.core.extensions import get_registry

        registry = get_registry()

        executor_instance = registry.create_executor_instance("test_executor_factory", inputs={})
        assert executor_instance is not None
        assert executor_instance.custom_initialized is True

    def test_property_based_metadata_survives_template_fallback(self):
        """Regression: when cls(inputs={}) fails during template creation
        (e.g. __init__ requires an extra positional arg), the fallback path
        assigned template.id/.name/.description directly on the instance.
        If a subclass declares these as read-only @property (a valid
        implementation of the ExecutableTask interface contract), that
        assignment either crashed (no setter) or stored the property
        descriptor object itself instead of a string. A property's computed
        value is unobtainable without a real instance (which is exactly what
        creation just failed to produce), so the correct fix falls back to a
        plain string default — the key requirement is that registration
        succeeds and id/name/description are genuine strings, never the
        property descriptor object itself. (Review CRITICAL #31)
        """
        from apflow.core.base import BaseTask
        from apflow.core.extensions import get_registry

        @executor_register()
        class PropertyBasedExecutor(BaseTask):
            category = ExtensionCategory.EXECUTOR

            def __init__(self, required_arg, inputs=None):
                # Requires a positional arg, so cls(inputs={}) always fails
                # during template creation, forcing the fallback path.
                super().__init__(inputs=inputs or {})
                self.required_arg = required_arg

            @property
            def id(self):
                return "property_based_executor"

            @property
            def name(self):
                return "Property Based Executor"

            @property
            def description(self):
                return "Uses computed properties for identity"

            async def execute(self, inputs):
                return {"result": "test"}

            def get_input_schema(self):
                return {"type": "object"}

        registry = get_registry()
        extension = registry.get_by_id("propertybasedexecutor")
        assert extension is not None
        assert isinstance(extension.id, str)
        assert isinstance(extension.name, str)
        assert isinstance(extension.description, str)

    def test_reregistration_returns_registry_recognized_class_for_hook_attachment(self):
        """Regression: idempotent re-registration (override=False) looked up
        a nonexistent 'executor_class' attribute on the registered Extension
        instance (always missing — the real class lives in a separate
        registry index), so getattr's fallback always returned the fresh
        cls argument instead of the class the registry actually uses. Hooks
        attached on a second @executor_register() call for the same id
        landed on a throwaway duplicate class, never the one instantiated
        at execution time. (Review CRITICAL #32)
        """
        from apflow.core.base import BaseTask
        from apflow.core.extensions import get_registry

        async def hook_v1(executor, task, inputs):
            pass

        @executor_register(pre_hook=hook_v1)
        class ReregisteredExecutorV1(BaseTask):
            id = "reregistered_executor"
            name = "Reregistered Executor"
            description = "test"
            category = ExtensionCategory.EXECUTOR

            def __init__(self, inputs=None):
                super().__init__(inputs=inputs or {})

            async def execute(self, inputs):
                return {"result": "ok"}

            def get_input_schema(self):
                return {"type": "object"}

        async def hook_v2(executor, task, inputs):
            pass

        # Second decoration of the same id, without override — must not
        # register again, but its hook must still land on the class the
        # registry actually recognizes.
        @executor_register(pre_hook=hook_v2)
        class ReregisteredExecutorV2(BaseTask):
            id = "reregistered_executor"
            name = "Reregistered Executor"
            description = "test"
            category = ExtensionCategory.EXECUTOR

            def __init__(self, inputs=None):
                super().__init__(inputs=inputs or {})

            async def execute(self, inputs):
                return {"result": "ok"}

            def get_input_schema(self):
                return {"type": "object"}

        registry = get_registry()
        actual_class = registry.get_executor_class("reregistered_executor")
        assert actual_class is not None
        assert actual_class._executor_hooks["pre_hook"] is hook_v2


class TestDecoratorIntegration:
    """Test integration of decorators with real components"""

    def setup_method(self):
        """Clear config before each test"""
        clear_config()

    @pytest.mark.asyncio
    async def test_full_decorator_workflow(self):
        """Test complete workflow using all decorators"""
        from apflow.core.config import clear_config

        clear_config()
        try:
            from apflow.api.a2a.agent_executor import AIPartnerUpFlowAgentExecutor
        except ImportError:
            pytest.skip("a2a module not available, skipping full workflow test")
            return

        pre_hooks_called = []
        post_hooks_called = []

        # Register hooks using decorators
        @register_pre_hook
        async def pre_hook1(task):
            pre_hooks_called.append(f"hook1-{task.id}")

        @register_pre_hook
        async def pre_hook2(task):
            pre_hooks_called.append(f"hook2-{task.id}")

        @register_post_hook
        async def post_hook1(task, inputs, result):
            post_hooks_called.append(f"hook1-{task.id}")

        # Set custom TaskModel (if needed)
        # For this test, we'll use default TaskModel

        # Create executor (should use registered hooks)
        executor = AIPartnerUpFlowAgentExecutor()

        # Verify hooks were registered
        assert len(executor.pre_hooks) == 2
        assert len(executor.post_hooks) == 1

        # Verify hooks are in correct order
        assert executor.pre_hooks[0] == pre_hook1
        assert executor.pre_hooks[1] == pre_hook2
        assert executor.post_hooks[0] == post_hook1

    def test_decorator_imports(self):
        """Test that all decorators can be imported from main package"""
        # Test that decorators are available from main package
        from apflow import (
            register_pre_hook,
            register_post_hook,
            set_task_model_class,
            get_task_model_class,
            executor_register,
        )

        # Verify they are callable
        assert callable(register_pre_hook)
        assert callable(register_post_hook)
        assert callable(set_task_model_class)
        assert callable(get_task_model_class)
        assert callable(executor_register)


class TestConfigRegistryIsolation:
    """Test that config registry is properly isolated between tests"""

    def setup_method(self):
        """Clear config before each test"""
        clear_config()

    def test_config_isolation(self):
        """Test that config changes don't leak between tests"""

        # Register a hook
        @register_pre_hook
        async def isolated_hook(task):
            pass

        # Verify it's registered
        from apflow.core.config import get_pre_hooks

        assert len(get_pre_hooks()) == 1

        # Clear config
        clear_config()

        # Verify it's cleared
        assert len(get_pre_hooks()) == 0
