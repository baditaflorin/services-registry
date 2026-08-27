#!/usr/bin/env python3
"""Audit that every configured repository has exactly one active CI hook.

The audit intentionally emits only webhook hostnames. GitHub webhook URLs can
contain repository-scoped signed tokens and must never be printed or persisted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "ci-authorities.json"


@dataclass(frozen=True)
class AuditResult:
    repository: str
    expected_host: str
    active_hosts: tuple[str, ...]
    ok: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "expected_host": self.expected_host,
            "active_hosts": list(self.active_hosts),
            "ok": self.ok,
            "reason": self.reason,
        }


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if config.get("version") != 1:
        raise ValueError("unsupported ci-authorities.json version")
    if not config.get("known_ci_hosts") or not config.get("owners"):
        raise ValueError("config must declare known_ci_hosts and owners")
    return config


def expected_host(config: dict[str, Any], repository: str) -> str:
    try:
        owner, name = repository.split("/", 1)
        owner_config = config["owners"][owner]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"no CI authority configured for {repository}") from exc
    return owner_config.get("overrides", {}).get(name, owner_config["default"])


def hook_hostname(hook: dict[str, Any]) -> str | None:
    raw_url = hook.get("config", {}).get("url")
    if not isinstance(raw_url, str):
        return None
    try:
        return urllib.parse.urlsplit(raw_url).hostname
    except ValueError:
        return None


def evaluate_hooks(
    repository: str,
    expected: str,
    known_hosts: Iterable[str],
    hooks: Iterable[dict[str, Any]],
) -> AuditResult:
    known = set(known_hosts)
    active = sorted(
        host
        for hook in hooks
        if hook.get("active") is True
        if (host := hook_hostname(hook)) in known
    )
    if not active:
        reason = "no active CI webhook"
    elif len(active) > 1:
        reason = "multiple active CI webhooks"
    elif active[0] != expected:
        reason = f"active CI is {active[0]}, expected {expected}"
    else:
        reason = "exactly one authoritative CI webhook"
    return AuditResult(repository, expected, tuple(active), reason.startswith("exactly"), reason)


class GitHubClient:
    def __init__(self, token: str | None, api_url: str = "https://api.github.com") -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")

    def _get(self, path: str) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "services-registry-ci-authority-audit/1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(f"{self.api_url}{path}", headers=headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    def hooks(self, repository: str) -> list[dict[str, Any]]:
        return self._get(f"/repos/{repository}/hooks")

    def owned_repositories(self, owner: str) -> list[str]:
        if not self.token:
            raise ValueError("GH_TOKEN or GITHUB_TOKEN is required for --all")
        repositories: list[str] = []
        page = 1
        while True:
            rows = self._get(
                "/user/repos?affiliation=owner&per_page=100&sort=full_name"
                f"&page={page}"
            )
            if not rows:
                break
            repositories.extend(
                row["full_name"]
                for row in rows
                if row.get("owner", {}).get("login", "").lower() == owner.lower()
            )
            if len(rows) < 100:
                break
            page += 1
        return repositories


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--repo", help="single OWNER/REPO to audit")
    target.add_argument("--all", metavar="OWNER", help="audit every owned repository")
    parser.add_argument("--json", action="store_true", help="emit JSON results")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        config = load_config(args.config)
        client = GitHubClient(os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN"))
        repositories = [args.repo] if args.repo else client.owned_repositories(args.all)
        results = []
        for repository in repositories:
            try:
                expected = expected_host(config, repository)
            except ValueError:
                # --all only covers owners declared in the registry; a repository
                # under an unconfigured owner is outside this audit's authority.
                continue
            hooks = client.hooks(repository)
            results.append(
                evaluate_hooks(
                    repository,
                    expected,
                    config["known_ci_hosts"],
                    hooks,
                )
            )
    except (ValueError, OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
        print(f"ci-authority audit failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps([result.as_dict() for result in results], indent=2, sort_keys=True))
    else:
        for result in results:
            status = "PASS" if result.ok else "FAIL"
            active = ",".join(result.active_hosts) or "none"
            print(
                f"{status} repo={result.repository} expected={result.expected_host} "
                f"active={active} reason={result.reason}"
            )
        passed = sum(result.ok for result in results)
        print(f"summary pass={passed} fail={len(results) - passed} total={len(results)}")
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
