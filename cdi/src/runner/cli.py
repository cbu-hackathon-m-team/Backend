import argparse
import json
from pathlib import Path

from runner.browser import execute_browser
from runner.compose import Compose
from runner.history import History
from runner.http import execute_http


def main():
    parser = argparse.ArgumentParser(description="Trusted local test runner")
    parser.add_argument("--db", type=Path, default=Path(".local/history.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    http = commands.add_parser("http")
    http.add_argument("url")
    http.add_argument("--method", choices=["GET", "POST", "PUT", "PATCH", "DELETE"], default="GET")
    http.add_argument("--payload", type=Path)
    http.add_argument("--expected-status", type=int, default=200)
    browser = commands.add_parser("browser")
    browser.add_argument("url")
    browser.add_argument("--selector", required=True)
    browser.add_argument("--text", required=True)
    browser.add_argument("--channel", choices=["msedge", "chrome"])
    commands.add_parser("history")
    compose = commands.add_parser("compose")
    compose.add_argument("action", choices=["up", "down", "logs"])
    compose.add_argument("--file", type=Path, default=Path("infra/compose.yaml"))
    compose.add_argument("--project", required=True)
    args = parser.parse_args()
    if args.command == "compose":
        print(getattr(Compose(args.file, args.project), args.action)())
        return
    history = History(args.db)
    if args.command == "history":
        print(json.dumps(history.latest(), ensure_ascii=False, indent=2))
        return
    if args.command == "http":
        payload = json.loads(args.payload.read_text(encoding="utf-8")) if args.payload else None
        result = execute_http(
            args.url, method=args.method, payload=payload, expected_status=args.expected_status
        )
    else:
        result = execute_browser(
            args.url, selector=args.selector, expected_text=args.text, channel=args.channel
        )
    history.save(result)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    raise SystemExit(0 if result.status == "PASS" else 1)


if __name__ == "__main__":
    main()
