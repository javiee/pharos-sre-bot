from __future__ import annotations

import argparse
import sys

def main() -> None:
    parser = argparse.ArgumentParser(description="Troubleshooting Agent CLI")
    sub = parser.add_subparsers(dest="command", required=True)  
    api_parser = sub.add_parser("api", help="Run the FastAPI server")
    api_parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable uvicorn auto-reload on code changes (local dev only).",
    )
    sub.add_parser("ingest", help="Ingest runbooks into the vector store")

    args = parser.parse_args()

    if args.command == "api":
        import uvicorn
        from .config import settings

        uvicorn.run(
            "troubleshooting_agent.api:create_app",
            host=settings.host,
            port=settings.port,
            reload=args.reload,  # local dev only: `tsa api --reload`
        )
    elif args.command == "ingest":
        from .ingest import ingest_all
        ingest_all()
    else:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        sys.exit(1)
