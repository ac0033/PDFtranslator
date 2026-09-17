"""Find out what an OpenAI-compatible endpoint actually implements.

The translation provider uses the Responses API. Plenty of gateways only speak
Chat Completions, and some accept structured output only in one of them. Rather
than discover that in the middle of a paid run, spend one tiny call per shape
and report which ones work.

Reads CODEBUDDY_BASE_URL, CODEBUDDY_API_KEY and CODEBUDDY_MODEL_ID from .env.
Prints nothing that could leak the key.
"""
from pathlib import Path
import argparse
import json
import os

from dotenv import load_dotenv

PROBE_SCHEMA = {
    "type": "object",
    "properties": {"unit_id": {"type": "string"}, "translated_text": {"type": "string"}},
    "required": ["unit_id", "translated_text"],
    "additionalProperties": False,
}
PROBE_PROMPT = 'Translate into Simplified Chinese and return JSON: {"unit_id": "probe", "translated_text": "..."} for: The cat sat on the mat.'


def _client(base_url: str, api_key: str):
    from openai import OpenAI

    return OpenAI(api_key=api_key, base_url=base_url)


def try_responses(client, model: str) -> dict:
    response = client.responses.create(
        model=model,
        instructions="Return only the JSON object requested.",
        input=PROBE_PROMPT,
        max_output_tokens=200,
        store=False,
        text={"format": {"type": "json_schema", "name": "probe", "schema": PROBE_SCHEMA}},
    )
    return {"ok": True, "output": response.output_text[:200]}


def try_chat_json_schema(client, model: str) -> dict:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": PROBE_PROMPT}],
        max_tokens=200,
        response_format={"type": "json_schema", "json_schema": {"name": "probe", "schema": PROBE_SCHEMA}},
    )
    return {"ok": True, "output": response.choices[0].message.content[:200]}


def try_chat_json_object(client, model: str) -> dict:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": PROBE_PROMPT}],
        max_tokens=200,
        response_format={"type": "json_object"},
    )
    return {"ok": True, "output": response.choices[0].message.content[:200]}


def try_chat_plain(client, model: str) -> dict:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": PROBE_PROMPT}],
        max_tokens=200,
    )
    return {"ok": True, "output": response.choices[0].message.content[:200]}


SHAPES = {
    "responses.json_schema": try_responses,
    "chat.json_schema": try_chat_json_schema,
    "chat.json_object": try_chat_json_object,
    "chat.plain": try_chat_plain,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--model", default=None, help="Overrides CODEBUDDY_MODEL_ID.")
    parser.add_argument("--shape", action="append", help="Probe only these shapes; repeat.")
    args = parser.parse_args()

    load_dotenv(args.env)
    base_url = (os.environ.get("CODEBUDDY_BASE_URL") or "").strip()
    api_key = (os.environ.get("CODEBUDDY_API_KEY") or "").strip()
    model = (args.model or os.environ.get("CODEBUDDY_MODEL_ID") or "").strip()
    missing = [name for name, value in
               (("CODEBUDDY_BASE_URL", base_url), ("CODEBUDDY_API_KEY", api_key), ("CODEBUDDY_MODEL_ID", model))
               if not value]
    if missing:
        print(json.dumps({"status": "not_configured", "missing": missing}, ensure_ascii=False, indent=2))
        return 1

    client = _client(base_url, api_key)
    host = base_url.split("//", 1)[-1].split("/", 1)[0]
    results = {}
    for name, probe in SHAPES.items():
        if args.shape and name not in args.shape:
            continue
        try:
            results[name] = probe(client, model)
        except Exception as exc:
            message = str(exc)
            results[name] = {"ok": False, "error": f"{type(exc).__name__}: {message[:300]}"}
    working = [name for name, result in results.items() if result["ok"]]
    print(json.dumps({
        "host": host, "model": model,
        "working_shapes": working,
        "results": results,
        "calls_made": len(results),
    }, ensure_ascii=False, indent=2))
    return 0 if working else 2


if __name__ == "__main__":
    raise SystemExit(main())
