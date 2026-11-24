import argparse
import logging
import secrets
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from .config import load_config
from .demo import knowledge_server, math_server
from .gateway import create_app

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(prog="mcp-gateway", description="MCP Gateway")
    parser.add_argument("command", choices=["init", "gateway", "math", "knowledge", "inventory"])
    parser.add_argument("--config", type=Path, help="Gateway profile YAML path")
    parser.add_argument("--port", type=int)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "runtime")
    parser.add_argument("--surface", choices=["classic", "code"], help="Select the model-facing interface")
    args = parser.parse_args()
    if args.command == "gateway" and args.config is None:
        parser.error("gateway requires an explicit --config profile path")
    load_dotenv(ROOT / ".env")
    if args.command == "init":
        destination = ROOT / ".env"
        if destination.exists():
            print("Existing .env preserved.")
            return
        destination.write_text("MCP_GATEWAY_READER_TOKEN=" + secrets.token_urlsafe(32) +
            "\nMCP_GATEWAY_OPERATOR_TOKEN=" + secrets.token_urlsafe(32) + "\n", encoding="utf-8")
        print("Created local tokens in .env. Keep that file private.")
    elif args.command == "math":
        math_server(args.port or 8766).run(transport="streamable-http")
    elif args.command == "knowledge":
        knowledge_server(args.data_dir, args.port or 8767).run(transport="streamable-http")
    elif args.command == "inventory":
        from .inventory_demo import inventory_app
        uvicorn.run(inventory_app(args.data_dir), host="127.0.0.1", port=args.port or 8788, log_level="warning")
    else:
        settings = load_config(args.config)
        if args.port:
            settings.port = args.port
        if args.surface:
            settings.surface = args.surface
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("mcp").setLevel(logging.ERROR)
        uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
