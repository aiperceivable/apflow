# Feature Spec: TaskCreator Relaxation (F-005)

**Feature ID:** F-005
**Priority:** P0
**Phase:** Phase 1 (0.20.0-alpha.1)
**Tech Design Reference:** Section 4.6

---

## Purpose

Remove the single-root constraint from `TaskCreator.create_task_tree_from_array()`, allowing multiple independent root tasks (multi-root forests) in a single creation call. This removes an artificial limitation that forced all tasks into a single tree hierarchy.

---

## File Changes

### Modified Files

**`src/apflow/core/execution/task_creator.py`**

**Change 1: Remove single-root validation (lines 237-240)**

```python
# DELETE these 4 lines:
if len(root_tasks) > 1:
    raise ValueError(
        "Multiple root tasks found. All tasks must be in a single task tree. Only one task should have parent_id=None or no parent_id field."
    )
```

The zero-root check (lines 233-236) is preserved:
```python
# KEEP:
if len(root_tasks) == 0:
    raise ValueError(
        "No root task found (task with no parent_id). At least one task in the array must have parent_id=None or no parent_id field."
    )
```

**Change 2: Update multi-root return logic (lines 253-257)**

```python
# Before:
root_models: List[TaskModelType] = [task for task in task_models if task.parent_id is None]
root_task = root_models[0]
task_tree = self.build_task_tree_from_task_models(root_task, task_models)
logger.info(f"Created task tree: root task {task_tree.task.id}")
return task_tree

# After:
root_models: List[TaskModelType] = [task for task in task_models if task.parent_id is None]
if len(root_models) == 1:
    task_tree = self.build_task_tree_from_task_models(root_models[0], task_models)
    logger.info(f"Created task tree: root task {task_tree.task.id}")
    return task_tree
else:
    task_trees = self.build_task_trees_from_task_models(task_models)
    logger.info(
        f"Created {len(task_trees)} task trees: "
        f"{', '.join(t.task.id for t in task_trees)}"
    )
    return task_trees[0]  # Backward compatible: return first tree
```

**Rationale for returning `task_trees[0]`:** The method signature returns `TaskTreeNode` (single tree). Changing the return type would break existing callers. The `create_task_trees_from_array()` method (line 259) already returns `List[TaskTreeNode]` for multi-root use cases. The relaxation in `create_task_tree_from_array()` is about removing the validation error, not changing the return type.

**Change 3: Update method docstring (line 221-228)**

```python
# Before:
"""
Create task tree from a flat array of task dictionaries (single tree).
...
"""

# After:
"""
Create task tree(s) from a flat array of task dictionaries.

Supports both single-root and multi-root task forests.
For single-root, returns the root TaskTreeNode.
For multi-root, returns the first root TaskTreeNode
(use create_task_trees_from_array() for all roots).
...
"""
```

### Preserved Validations

The following validations remain unchanged:

1. **Zero-root check** (`_validate_common`, line 233-236): At least one root task required.
2. **Circular dependency detection** (`_validate_common`): Detects cycles in dependency graph.
3. **Reference validation** (`_validate_common`): All `parent_id` values point to existing tasks in the array.
4. **Duplicate ID detection** (`_validate_common`): No two tasks share the same ID.
5. **task_tree_id uniqueness** (`_validate_common`, line 174-176): Different root tasks cannot share the same `task_tree_id`.

### Test Files

**`tests/core/execution/test_task_creator.py`** -- Modify existing test file.

---

## Method Signatures

No new methods. One modified method:

```python
async def create_task_tree_from_array(
    self, tasks: List[Dict[str, Any]]
) -> TaskTreeNode:
    """Create task tree(s) from a flat array of task dictionaries.

    Args:
        tasks: List of task dictionaries. Each dict must have at least 'name'.
               Tasks with parent_id=None are root tasks. Multiple roots are allowed.

    Returns:
        TaskTreeNode for the first root task.

    Raises:
        ValueError: If tasks is empty, no root task found, circular dependency
                    detected, duplicate IDs found, or invalid references.
    """
```

---

## Data Models

No data model changes.

---

## Test Requirements

### New Test Cases

```python
async def test_create_single_root_still_works():
    """Single root task creation works as before (backward compatibility).
    Input: [{"name": "root"}, {"name": "child", "parent_id": "root_id"}]
    Expected: Returns TaskTreeNode for root with child.
    """

async def test_create_multi_root_returns_first():
    """Multiple root tasks: returns first root's TaskTreeNode.
    Input: [
        {"name": "task_a"},  # root 1
        {"name": "task_b"},  # root 2
        {"name": "task_c"},  # root 3
    ]
    Expected: Returns TaskTreeNode for task_a. All three tasks created in DB.
    """

async def test_create_multi_root_all_persisted():
    """All root tasks are persisted in database.
    Input: 3 independent root tasks.
    Verify: Query DB, all 3 exist with parent_id=None.
    """

async def test_create_multi_root_with_children():
    """Multi-root where each root has children.
    Input: [
        {"id": "a", "name": "root_a"},
        {"id": "b", "name": "child_a", "parent_id": "a"},
        {"id": "c", "name": "root_c"},
        {"id": "d", "name": "child_c", "parent_id": "c"},
    ]
    Expected: Two trees. First tree: root_a -> child_a. Second tree: root_c -> child_c.
    """

async def test_create_zero_root_still_raises():
    """No root tasks still raises ValueError.
    Input: [{"name": "orphan", "parent_id": "nonexistent"}]
    Expected: ValueError with 'No root task found'.
    """

async def test_create_multi_root_circular_dep_still_detected():
    """Circular dependency detection works with multi-root.
    Input: [
        {"id": "a", "name": "a", "dependencies": [{"id": "b"}]},
        {"id": "b", "name": "b", "dependencies": [{"id": "a"}]},
    ]
    Expected: ValueError (circular dependency).
    """

async def test_create_multi_root_duplicate_id_still_detected():
    """Duplicate ID detection works with multi-root.
    Input: [
        {"id": "same", "name": "task1"},
        {"id": "same", "name": "task2"},
    ]
    Expected: ValueError (duplicate ID).
    """

async def test_create_multi_root_unique_tree_ids():
    """Each root gets a unique task_tree_id.
    Input: [{"name": "a"}, {"name": "b"}]
    Verify: task_tree_id differs between the two roots.
    """

async def test_create_multi_root_shared_tree_id_raises():
    """Two roots with same explicit task_tree_id raises.
    Input: [
        {"name": "a", "task_tree_id": "shared"},
        {"name": "b", "task_tree_id": "shared"},
    ]
    Expected: ValueError (shared task_tree_id).
    """
```

### Modified Existing Tests

If any existing test asserts that multi-root raises `ValueError("Multiple root tasks found")`, update that test to expect success instead.

---

## Acceptance Criteria

1. Tasks can be created without `parent_id` and exist as independent roots.
2. Multiple root tasks can coexist in the same `create_task_tree_from_array()` call without raising an error.
3. The method returns the first root's `TaskTreeNode` (backward-compatible return type).
4. `create_task_trees_from_array()` (existing method) continues to work and returns all roots.
5. Circular dependency detection still works correctly with multi-root forests.
6. Reference validation still rejects invalid `parent_id` references.
7. Duplicate ID detection still works correctly.
8. `task_tree_id` uniqueness across roots is still enforced.
9. Existing single-root creation patterns continue to work unchanged.
