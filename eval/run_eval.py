"""Evaluate Advocate's landlord-line flags in text mode only."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.ai import GeminiGateway, TEXT_CANDIDATES  # noqa: E402


DATA_PATH = ROOT / "data" / "landlord_lines.json"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "results.json"


def dry_run_model(line: dict[str, Any]) -> dict[str, Any]:
    """Small deterministic stand-in for a model; it never reads gold labels."""
    text = line["text"].lower()
    flags: list[str] = []
    money_terms = ("deposit", "rent", "fee", "cost", "dollar", "percent", "utilities", "extra")
    if any(term in text for term in money_terms) or re.search(r"\$|\b\d+(?:\.\d+)?%?\b", text):
        flags.append("financial_term")
    if any(term in text for term in ("lose the deposit", "fee", "owe the rest", "breaking that rule")):
        flags.append("penalty")
    if any(term in text for term in ("maybe", "probably", "i'll see", "we'll see", "no guarantees", "i'll try", "talk later")):
        flags.append("vague_commitment")
    promise = any(term in text for term in ("i promise", "i will ", "i'll cover", "i will keep"))
    if promise:
        flags.append("promise")
    if any(term in text for term in ("not allowed", "no pets", "no overnight guests", "must give", "not want to renew")):
        flags.append("restriction")
    return {"id": line["id"], "flags": flags, "promise": promise}


def _score_results(truth: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location("advocate_score", ROOT / "score.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the project's score.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.score(truth, results)


async def _run(args: argparse.Namespace, gateway_factory: Callable[[], GeminiGateway]) -> int:
    with DATA_PATH.open(encoding="utf-8") as data_file:
        truth = json.load(data_file)

    if args.dry_run:
        results = [dry_run_model(line) for line in truth]
    else:
        gateway = gateway_factory()
        if not gateway.key_present:
            print("GEMINI_API_KEY is not configured. Add it to the ignored repo .env to run model evaluation.")
            return 2
        await gateway.initialize()
        if not gateway.select_model("generateContent", TEXT_CANDIDATES):
            print("No text-generation model is available to this key; no evaluation was run.")
            await gateway.close()
            return 2
        results = []
        for index, line in enumerate(truth, start=1):
            output = await gateway.analyze_landlord_line(line["id"], line["text"], "en")
            if output["note"].startswith("Automated analysis is unavailable;"):
                print(f"Model evaluation failed on {line['id']}; no partial results were written.")
                await gateway.close()
                return 1
            results.append(
                {"id": line["id"], "flags": output["flags"], "promise": output["promise"]}
            )
            print(f"Evaluated {index}/{len(truth)} lines", file=sys.stderr, flush=True)
        await gateway.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as output_file:
        json.dump(results, output_file, ensure_ascii=False, indent=2)
        output_file.write("\n")
    print(json.dumps(_score_results(truth, results), indent=2))
    return 0


def main(
    argv: list[str] | None = None,
    *,
    gateway_factory: Callable[[], GeminiGateway] = GeminiGateway,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Use a local fake classifier; makes no API calls.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Results JSON path.")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args, gateway_factory))


if __name__ == "__main__":
    raise SystemExit(main())
