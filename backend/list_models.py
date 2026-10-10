"""Print model IDs and supported actions visible to GEMINI_API_KEY."""

from __future__ import annotations

import asyncio
import sys

from backend.ai import GeminiGateway


async def _main() -> int:
    gateway = GeminiGateway()
    if not gateway.key_present:
        print("GEMINI_API_KEY is not configured; no account-specific models can be listed.")
        return 2
    models = await gateway.discover_models()
    if gateway.discovery_error:
        print("Could not list models for this key. No model IDs are confirmed.")
        await gateway.close()
        return 1
    if not models:
        print("The API returned no models for this key.")
        await gateway.close()
        return 1
    for model_id in sorted(models):
        actions = ", ".join(sorted(models[model_id])) or "no supported actions reported"
        print(f"{model_id}\t{actions}")
    await gateway.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
