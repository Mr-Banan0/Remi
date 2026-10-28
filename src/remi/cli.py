from __future__ import annotations

import argparse
import json
import os

from .settings import load_environment, private_key
from .storage import SQLiteStore, TABLES
from .version import PIPELINE, VERSION


def main():
    parser = argparse.ArgumentParser(description="Remi local development commands")
    parser.add_argument("--env-file")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-db", help="Create the six approved tables in a new database")
    init.add_argument("--path", default=".local-data/remi.sqlite3")
    init.add_argument("--allow-create-schema", action="store_true", help="Use after explicit approval of the six table scope")
    sub.add_parser("check-config", help="Report only model names and credential presence")
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8011, type=int)
    args = parser.parse_args()
    load_environment(args.env_file)
    if args.command == "init-db":
        SQLiteStore.initialize(args.path, allow_create_schema=args.allow_create_schema)
        print(json.dumps({"created": args.path, "tables": sorted(TABLES)}))
    elif args.command == "check-config":
        print(json.dumps({"version": VERSION, "pipeline": PIPELINE,
            "llm_model": os.getenv("LLM_MODEL", "gpt-4o-mini"),
            "embedding_model": "text-embedding-v4", "reranker_model": os.getenv("TYPESAFE_MODEL", "jev-1.13.0"),
            "text_key_present": bool(private_key("LLM_API_KEY", "LLM_API_KEY_FILE")),
            "embedding_key_present": bool(private_key("DASHSCOPE_API_KEY", "DASHSCOPE_API_KEY_FILE")),
            "reranker_key_present": bool(private_key("TYPESAFE_API_KEY", "TYPESAFE_API_KEY_FILE"))}))
    elif args.command == "serve":
        import uvicorn
        uvicorn.run("remi.api:app", host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
