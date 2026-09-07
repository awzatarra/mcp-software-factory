"""Read-only, redacted checks of tracked files and all reachable Git history.

This is a limited pattern audit, not proof that credentials are inactive.
Binary assets and provider-side revocation require separate owner review.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path, PurePosixPath


PATTERNS = {
    "provider_token": re.compile(
        r"\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{30,}|"
        r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|"
        r"AKIA[A-Z0-9]{16})\b"
    ),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "credential_url": re.compile(r"https?://[^/\s:@]+:[^/\s@]+@"),
    "personal_path": re.compile(r"(?:[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s]+|/(?:home|Users)/[^/\s]+)"),
    "secret_assignment": re.compile(
        r"(?im)^\s*(?:OPENAI_API_KEY|GITHUB_TOKEN|GH_TOKEN|AWS_SECRET_ACCESS_KEY|"
        r"WEBHOOK_SECRET|DATABASE_PASSWORD)\s*=\s*[\"']?[^\s\"']{20,}"
    ),
}
RUNTIME_DIRS = {"data", "workspace", "workspaces", "logs", "snapshots", "artifacts", ".venv", "venv", "node_modules", "__pycache__"}
EXAMPLE_ENVS = {".env.example", ".env.production.example"}


def forbidden_path(name: str) -> bool:
    path = PurePosixPath(name)
    if path.name == ".gitkeep":
        return False
    if (path.name == ".env" or path.name.startswith(".env.")) and path.name not in EXAMPLE_ENVS:
        return True
    return bool(RUNTIME_DIRS.intersection(path.parts)) or bool(
        re.search(r"\.(?:db|sqlite3?|log|pyc|webm)(?:-(?:wal|shm))?$", path.name)
    )


def scan_text(data: bytes) -> list[dict]:
    if b"\0" in data:
        return []
    try:
        content = data.decode("utf-8-sig")
    except UnicodeError:
        return []
    findings = []
    for rule, pattern in PATTERNS.items():
        for match in pattern.finditer(content):
            findings.append({"rule": rule, "line": content.count("\n", 0, match.start()) + 1})
    return findings


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ["git", "-c", f"safe.directory={root.as_posix()}", *args],
        cwd=root, stdin=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )


def audit(root: Path, history: bool = False, include_untracked: bool = False) -> dict:
    root = root.resolve()
    findings = []
    binary_files = []
    files = [name for name in git(root, "ls-files", "-z").decode().split("\0") if name]
    tracked = set(files)
    tracked_count = len(files)
    if include_untracked:
        files += [name for name in git(root, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0") if name]
    for name in files:
        scope = "tracked" if name in tracked else "candidate"
        if forbidden_path(name):
            findings.append({"scope": scope, "path": name, "rule": "runtime_artifact"})
        path = root / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        try:
            data.decode("utf-8-sig")
            binary = b"\0" in data
        except UnicodeError:
            binary = True
        if binary:
            binary_files.append(name)
        findings.extend({"scope": scope, "path": name, **item} for item in scan_text(data))
    object_count = 0
    history_binary_count = 0
    if history:
        objects = git(root, "rev-list", "--objects", "--all").decode().splitlines()
        # One object at a time keeps pipe buffers bounded; never print content.
        with subprocess.Popen(
            ["git", "-c", f"safe.directory={root.as_posix()}", "cat-file", "--batch"],
            cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ) as process:
            try:
                for entry in objects:
                    oid, _, name = entry.partition(" ")
                    process.stdin.write((oid + "\n").encode())
                    process.stdin.flush()
                    header = process.stdout.readline().decode().split()
                    if len(header) != 3:
                        raise RuntimeError("git_object_read_failed")
                    data = process.stdout.read(int(header[2]))
                    process.stdout.read(1)
                    if header[1] not in {"blob", "commit", "tag"}:
                        continue
                    object_count += 1
                    if header[1] == "blob" and forbidden_path(name):
                        findings.append({"scope": "history", "object": oid, "path": name, "rule": "runtime_artifact"})
                    try:
                        data.decode("utf-8-sig")
                        binary = b"\0" in data
                    except UnicodeError:
                        binary = True
                    history_binary_count += int(binary)
                    findings.extend({"scope": "history", "object": oid, "path": name,
                                     **item} for item in scan_text(data))
            finally:
                process.stdin.close()
            if process.wait(timeout=30):
                raise RuntimeError("git_object_read_failed")
    return {
        "tracked_files": tracked_count, "candidate_files_scanned": len(files),
        "history_objects_scanned": object_count,
        "binary_files_requiring_review": binary_files,
        "history_binary_objects_requiring_review": history_binary_count,
        "findings": findings,
        "limitations": ["Pattern scan only; false positives require review.",
                        "Cannot prove token revocation, binary safety, remote refs, or deleted/unreachable history.",
                        "Does not change visibility or rewrite history."],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--include-untracked", action="store_true", help="Include non-ignored candidate additions.")
    args = parser.parse_args()
    try:
        report = audit(args.root, args.history, args.include_untracked)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        print(json.dumps({"error": "repository_audit_failed"}))
        return 2
    print(json.dumps(report, indent=2))
    return int(bool(report["findings"]))


if __name__ == "__main__":
    raise SystemExit(main())
