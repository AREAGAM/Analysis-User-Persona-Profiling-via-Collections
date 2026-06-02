from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


REPO = "AREAGAM/Analysis-User-Persona-Profiling-via-Collections"
BRANCH = "main"
ROOT = Path(__file__).parent.resolve()
FILES = [
    ".gitignore",
    "README.md",
    "app.py",
    "requirements.txt",
    "start.bat",
    "git-local.bat",
    "upload_to_github.py",
]


def request(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "xhs-collection-persona-uploader",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        if exc.code == 404:
            raise FileNotFoundError(raw) from exc
        raise RuntimeError(f"GitHub API {exc.code}: {raw}") from exc


def existing_sha(path: str, token: str) -> str | None:
    try:
        payload = request("GET", f"/contents/{path}?ref={BRANCH}", token)
    except FileNotFoundError:
        return None
    return payload.get("sha")


def upload_file(path: str, token: str) -> None:
    full_path = ROOT / path
    content = base64.b64encode(full_path.read_bytes()).decode("ascii")
    sha = existing_sha(path, token)
    body = {
        "message": f"Upload {path}",
        "content": content,
        "branch": BRANCH,
    }
    if sha:
        body["sha"] = sha
    request("PUT", f"/contents/{path}", token, body)
    print(f"{'updated' if sha else 'created'} {path}")


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("Missing GITHUB_TOKEN environment variable.")
        print("Create a fine-grained token with Contents: Read and write, then run:")
        print("  set GITHUB_TOKEN=your_token_here")
        print("  python upload_to_github.py")
        return 2

    missing = [path for path in FILES if not (ROOT / path).exists()]
    if missing:
        print("Missing files:", ", ".join(missing))
        return 1

    for path in FILES:
        upload_file(path, token)

    print(f"Done: https://github.com/{REPO}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
