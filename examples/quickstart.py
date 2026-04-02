"""
apflow Quickstart — End-to-End Example

This example shows the complete workflow:
1. Register custom executors
2. Create a task tree
3. Execute it
4. Get results

Run: python examples/quickstart.py
"""

import asyncio
from apflow.adapters.function_executor import function_executor
from apflow.app import create_app


# Step 1: Register custom executors using @function_executor
@function_executor(
    id="fetch_price",
    description="Fetch the price of a product",
    input_schema={
        "type": "object",
        "properties": {"product": {"type": "string"}},
        "required": ["product"],
    },
)
async def fetch_price(inputs: dict) -> dict:
    """Simulate fetching a product price."""
    prices = {"widget": 9.99, "gadget": 19.99, "doohickey": 4.99}
    product = inputs["product"]
    return {"product": product, "price": prices.get(product, 0.0)}


@function_executor(
    id="calculate_total",
    description="Calculate total from fetched prices",
)
async def calculate_total(inputs: dict) -> dict:
    """Aggregate results from dependency tasks."""
    total = sum(
        v.get("price", 0) for k, v in inputs.items() if isinstance(v, dict) and "price" in v
    )
    return {"total": round(total, 2), "item_count": len(inputs)}


async def main():
    # Step 2: Create the app (bootstraps session, TaskManager, Registry)
    app = create_app(connection_string="sqlite:///:memory:")

    print(f"Registered modules: {len(list(app.registry.list()))}")
    for m in sorted(app.registry.list()):
        print(f"  {m}")

    # Step 3: Create a task tree
    #   fetch_widget ──┐
    #                   ├──→ calculate_total
    #   fetch_gadget ──┘
    tasks = [
        {
            "id": "fetch_widget",
            "name": "Fetch Widget Price",
            "priority": 1,
            "inputs": {"product": "widget"},
            "params": {"executor_id": "fetch_price"},
        },
        {
            "id": "fetch_gadget",
            "name": "Fetch Gadget Price",
            "priority": 1,
            "inputs": {"product": "gadget"},
            "params": {"executor_id": "fetch_price"},
        },
        {
            "id": "total",
            "name": "Calculate Total",
            "priority": 2,
            "parent_id": "fetch_widget",
            "dependencies": [
                {"id": "fetch_widget", "required": True},
                {"id": "fetch_gadget", "required": True},
            ],
            "params": {"executor_id": "calculate_total"},
        },
    ]

    tree = await app.task_creator.create_task_tree_from_array(tasks)
    print(f"\nTask tree created: root={tree.task.id}")

    # Step 4: Execute
    await app.task_manager.distribute_task_tree(tree)

    # Step 5: Get results
    for task_id in ["fetch_widget", "fetch_gadget", "total"]:
        task = await app.task_repository.get_task_by_id(task_id)
        print(f"  {task.name}: status={task.status}, result={task.result}")


if __name__ == "__main__":
    asyncio.run(main())
