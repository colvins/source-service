import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path


async def run() -> None:
    script_path = Path(sys.argv[1])
    action = os.environ["COLVINS_ACTION"]
    params = json.loads(os.environ.get("COLVINS_PARAMS", "{}"))
    context = json.loads(os.environ.get("COLVINS_CONTEXT", "{}"))

    spec = importlib.util.spec_from_file_location("colvins_runtime_source", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    fn = getattr(module, action, None)
    if fn is None:
        raise RuntimeError(f"source does not export function: {action}")

    if asyncio.iscoroutinefunction(fn):
        result = await fn(params, context)
    else:
        result = fn(params, context)
    sys.stdout.write(json.dumps(result or {}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(run())
