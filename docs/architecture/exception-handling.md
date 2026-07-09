---
description: Exception-handling architecture for apflow task execution, logging, and failure propagation.
---

# Exception Handling Standards

## Overview

This document defines the exception handling standards for apflow.

## Exception Hierarchy

```
ApflowError (RuntimeError)
├── BusinessError (expected/user errors)
│   ├── ValidationError
│   └── ConfigurationError
└── SystemError (unexpected errors)
    ├── ExecutorError  
    └── StorageError
```

## Exception Types

### BusinessError
**Purpose**: Expected failures caused by user input, missing configuration, or business logic constraints.

**Logging**: WITHOUT stack trace (`exc_info=False`)

**Use cases**:
- Invalid input parameters
- Missing required configuration
- Permission/quota violations
- Resource not found (user-specified)

**Examples**:
```python
# Validation
if not inputs.get("model"):
    raise ValidationError("model is required in inputs")

# Configuration  
if not LIBRARY_AVAILABLE:
    raise ConfigurationError("library X is not installed. Install with: pip install X")

# Business logic
if user.quota_exceeded():
    raise BusinessError("API quota exceeded for this user")
```

### SystemError
**Purpose**: Unexpected system-level failures requiring investigation.

**Logging**: WITH stack trace (`exc_info=True`)

**Use cases**:
- Unexpected internal errors
- Database corruption
- Resource exhaustion

**Note**: Most system errors should propagate naturally (TimeoutError, ConnectionError, etc.) rather than being wrapped.

## Executor Implementation Guidelines

### DO: Let Technical Exceptions Propagate

```python
async def execute(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
    # Validate inputs
    if not inputs.get("url"):
        raise ValidationError("url is required")
    
    # Let httpx exceptions propagate naturally
    # (TimeoutException, ConnectError, etc.)
    response = await httpx.get(inputs["url"])
    
    return {"status": response.status_code}
```

### DON'T: Catch and Return Error Dicts

```python
# ❌ BAD - Don't do this
async def execute(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
    try:
        response = await httpx.get(inputs["url"])
    except httpx.TimeoutException:
        return {"success": False, "error": "Timeout"}  # Wrong!
```

### DO: Distinguish Business vs System Errors

```python
async def execute(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
    # Business error - missing config
    if not os.getenv("API_KEY"):
        raise ConfigurationError("API_KEY environment variable not set")
    
    # Technical error - let propagate
    response = await service.call()  # May raise TimeoutError, ConnectionError
    
    return {"result": response}
```

## TaskManager Logging

TaskManager handles exceptions with context-aware logging:

```python
try:
    result = await executor.execute(inputs)
except BusinessError as e:
    # Expected error - log message only
    logger.error(f"Business error: {str(e)}")
    # Still mark task as failed
except Exception as e:
    # Unexpected error - log with stack trace
    logger.error(f"Unexpected error: {str(e)}", exc_info=True)
    # Mark task as failed
```

## Comparison with Other Frameworks

### FastAPI Pattern
- `HTTPException` for client errors (400-499)
- `RequestValidationError` for input validation
- Generic exceptions for server errors (500+)
- Custom exception handlers for different error types

### Our Pattern
- `BusinessError` for user/config errors (like FastAPI's validation errors)
- `SystemError` for unexpected errors (rare, most propagate naturally)
- `ApflowError` as base for framework-specific exceptions
- TaskManager acts as central exception handler

## Migration Notes

For existing code:
1. Replace `return {"success": False, "error": "..."}` with `raise BusinessError("...")`
2. Remove try/except blocks around technical operations (httpx, aiosmtplib, sqlalchemy)
3. Keep business validation and raise `ValidationError` or `ConfigurationError`
4. Let timeout/connection/service errors propagate naturally

## Testing

Tests should verify:
```python
@pytest.mark.asyncio
async def test_validation_error_marks_failed():
    task = await create_task_with_missing_input()
    await task_manager.execute(task)
    
    # Verify task marked as failed
    assert task.status == "failed"
    assert "required" in task.error
    
@pytest.mark.asyncio  
async def test_timeout_marks_failed():
    task = await create_task_with_slow_service()
    await task_manager.execute(task)
    
    # Timeout propagated and task marked failed
    assert task.status == "failed"
    assert "timeout" in task.error.lower()
```

## Summary

1. **Use `BusinessError` subclasses** for expected failures (validation, config)
2. **Let technical exceptions propagate** (timeout, connection, service errors)
3. **TaskManager handles all exceptions** and marks tasks as failed appropriately
4. **Log with context**: `BusinessError` without stack trace, others with stack trace
5. **Never return error dicts** - always raise exceptions for failures
