from __future__ import annotations

import fnmatch
import glob
import json
import os
import posixpath
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from google import genai
from google.genai import types

MISSING_ISSUE_MARKER = "<!-- pr-council:missing-issue -->"
LEGACY_MISSING_ISSUE_MARKER = "<!-- gemini-pr-reviewer:missing-issue -->"
DEFAULT_PRIMARY_MODEL = "gemini-3.8-flash"
DEFAULT_FALLBACK_MODELS = "gemini-3.6-flash,gemini-3.5-flash-lite"
DEFAULT_PROVIDER_ORDER = "gemini,groq,openrouter,cerebras,mistral,custom-openai"
DEFAULT_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_GROQ_MODELS = "openai/gpt-oss-120b,llama-3.3-70b-versatile"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODELS = "qwen/qwen3-coder:free,deepseek/deepseek-r1:free"
DEFAULT_CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"
DEFAULT_CEREBRAS_MODELS = "llama-3.3-70b"
DEFAULT_MISTRAL_BASE_URL = "https://api.mistral.ai/v1"
DEFAULT_MISTRAL_MODELS = "codestral-latest"
VALID_REVIEW_MODES = ("fallback", "subagents", "consensus", "parallel")
DEFAULT_REVIEW_MODE = "fallback"
VALID_EXTRA_PROVIDERS_MODES = ("fallback", "subagents")
DEFAULT_EXTRA_PROVIDERS_MODE = "fallback"
DEFAULT_MAIN_REVIEWERS = 2
DEFAULT_CONSENSUS_MAX_PROVIDERS = 3
ACTION_TO_GH_EVENT = {
    "approve": "APPROVE",
    "request-changes": "REQUEST_CHANGES",
    "comment": "COMMENT",
}
INLINE_COMMENTS_BLOCK_RE = re.compile(
    r"```json:inline-comments\s*\n?(.*?)```",
    re.IGNORECASE | re.DOTALL,
)

DEFAULT_EXCLUDE_BASENAMES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pipfile.lock",
        "cargo.lock",
        "composer.lock",
        "gemfile.lock",
        "go.sum",
        "uv.lock",
        "bun.lock",
        "bun.lockb",
    }
)

DEFAULT_EXCLUDE_PATTERNS = (
    "*.min.js",
    "*.min.css",
    "*.bundle.js",
    "*.map",
    "*.d.ts.map",
    "*.snap",
    "dist/*",
    "*/dist/*",
    "build/*",
    "*/build/*",
    ".next/*",
    "*/.next/*",
    "out/*",
    "*/out/*",
    "vendor/*",
    "*/vendor/*",
    "node_modules/*",
    "*/node_modules/*",
)

COMPILED_ARTIFACT_PATTERNS = (
    "*.min.js",
    "*.min.css",
    "*.bundle.js",
    "dist/*",
    "*/dist/*",
    "build/*",
    "*/build/*",
    ".next/*",
    "*/.next/*",
    "out/*",
    "*/out/*",
)

FATAL_AUTH_INDICATORS = (
    "401",
    "403",
    "api_key_invalid",
    "api key not valid",
    "invalid api key",
    "permission_denied",
    "permission denied",
    "unauthenticated",
)

MODEL_UNAVAILABLE_INDICATORS = (
    "404",
    "not_found",
    "not found",
    "model_not_found",
    "not supported",
    "deprecated",
)

TRANSIENT_ERROR_INDICATORS = (
    "503",
    "unavailable",
    "high demand",
    "429",
    "resource_exhausted",
    "overloaded",
    "spikes in demand",
    "temporarily unavailable",
    "quota exceeded",
    "rate limit",
    "500",
    "502",
    "504",
    "internal",
    "bad gateway",
    "gateway timeout",
    "disconnected",
    "server disconnected",
    "connection reset",
    "connection aborted",
    "connection refused",
    "remoteprotocolerror",
    "remote end closed",
    "broken pipe",
    "eof occurred",
    "timed out",
    "timeout",
    "readtimeout",
    "connecttimeout",
    "deadline_exceeded",
    "apiconnectionerror",
    "network",
)


def sanitize_error(err_str: str, *secrets: str) -> str:
    """Scrubs API keys/secrets and normalizes error messages for safe logging."""
    cleaned = str(err_str or "")
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "***")
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > 240:
        cleaned = cleaned[:237] + "..."
    return cleaned


def is_fatal_auth_error(err_str: str) -> bool:
    """Checks if the exception indicates invalid credentials or denied permissions."""
    lowered = str(err_str or "").lower()
    return any(indicator in lowered for indicator in FATAL_AUTH_INDICATORS)


def is_model_unavailable_error(err_str: str) -> bool:
    """Checks if the exception indicates an unknown, unsupported, or deprecated model."""
    lowered = str(err_str or "").lower()
    return any(indicator in lowered for indicator in MODEL_UNAVAILABLE_INDICATORS)


def is_transient_error(err_str: str) -> bool:
    """Checks if the exception indicates transient capacity, rate limit, timeout, or socket drop."""
    lowered = str(err_str or "").lower()
    return any(indicator in lowered for indicator in TRANSIENT_ERROR_INDICATORS)


is_transient_busy_error = is_transient_error


def _dedupe_models(raw_items: list[str]) -> list[str]:
    """Trims and deduplicates model names while preserving order."""
    candidates: list[str] = []
    for item in raw_items:
        model = item.strip()
        if model and model not in candidates:
            candidates.append(model)
    return candidates


def parse_model_list(raw_models: str, default_models: str = "") -> list[str]:
    """Parses a comma-separated model list, falling back to default_models when empty."""
    parsed = _dedupe_models(str(raw_models or "").split(","))
    if parsed:
        return parsed
    return _dedupe_models(str(default_models or "").split(",")) if default_models else []


def build_candidate_models(primary_model: str, fallback_models_raw: str) -> list[str]:
    """Builds an ordered, deduplicated list of candidate models from primary and comma-separated fallbacks."""
    candidates = _dedupe_models([primary_model] + str(fallback_models_raw or "").split(","))
    return candidates or [DEFAULT_PRIMARY_MODEL]


def parse_review_mode(raw_mode: str) -> str:
    """Normalizes and validates the configured REVIEW_MODE."""
    mode = str(raw_mode or "").strip().lower()
    if mode in VALID_REVIEW_MODES:
        return mode
    if mode:
        print(f"Warning: Unknown review-mode '{raw_mode}'. Falling back to '{DEFAULT_REVIEW_MODE}'.")
    return DEFAULT_REVIEW_MODE


def parse_extra_providers_mode(raw_extra_mode: str, raw_review_mode: str = "") -> str:
    """Determines whether providers beyond main-reviewers act as 'fallback' or 'subagents'."""
    mode = str(raw_extra_mode or "").strip().lower()
    if mode in VALID_EXTRA_PROVIDERS_MODES:
        return mode
    if str(raw_review_mode or "").strip().lower() == "subagents":
        return "subagents"
    if mode:
        print(
            f"Warning: Unknown extra-providers-mode '{raw_extra_mode}'. "
            f"Falling back to '{DEFAULT_EXTRA_PROVIDERS_MODE}'."
        )
    return DEFAULT_EXTRA_PROVIDERS_MODE


def parse_bool_env(raw_val: str, default: bool = True) -> bool:
    """Parses a boolean environment string with a safe default."""
    cleaned = str(raw_val or "").strip().lower()
    if not cleaned:
        return default
    if cleaned in ("true", "1", "yes", "on"):
        return True
    if cleaned in ("false", "0", "no", "off"):
        return False
    return default


def run_gh_command(args: list[str]) -> tuple[str | None, str]:
    """Runs a GitHub CLI command and returns (stdout, stderr). stdout is None on error."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return result.stdout.strip(), ""
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        print(f"Warning: gh command {' '.join(args)} failed (exit {e.returncode}): {stderr or e}")
        return None, stderr
    except Exception as e:
        print(f"Warning: gh command {' '.join(args)} failed: {e}")
        return None, str(e)


def fetch_pr_context(repo: str, pr_number: str) -> dict:
    """Fetches PR metadata (title, body, linked issues, and prior reviews)."""
    context = {
        "title": "",
        "body": "",
        "headRefName": "",
        "closingIssuesReferences": [],
        "reviews": [],
    }

    # 1. Read webhook event payload if available as baseline fallback
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        try:
            with open(event_path, "r") as f:
                event_data = json.load(f)
            pr_payload = event_data.get("pull_request", {})
            context["title"] = pr_payload.get("title") or ""
            context["body"] = pr_payload.get("body") or ""
            context["headRefName"] = (pr_payload.get("head") or {}).get("ref") or ""
        except Exception as e:
            print(f"Warning: Failed to parse GITHUB_EVENT_PATH: {e}")

    if not repo or not pr_number:
        return context

    # 2. Query gh pr view (with closingIssuesReferences first, then fallback without it)
    out, _ = run_gh_command(
        [
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "title,body,headRefName,closingIssuesReferences,reviews",
        ]
    )
    if out is None:
        out, _ = run_gh_command(
            [
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "title,body,headRefName,reviews",
            ]
        )

    if out:
        try:
            data = json.loads(out)
            context["title"] = data.get("title") or context["title"]
            context["body"] = data.get("body") or context["body"]
            context["headRefName"] = data.get("headRefName") or context["headRefName"]
            if "closingIssuesReferences" in data and data["closingIssuesReferences"]:
                context["closingIssuesReferences"] = data["closingIssuesReferences"]
            if "reviews" in data and data["reviews"]:
                context["reviews"] = data["reviews"]
        except Exception as e:
            print(f"Warning: Failed to parse gh pr view output: {e}")

    return context


def extract_linked_issue_numbers(pr_context: dict) -> list[int]:
    """Extracts candidate linked issue numbers from closingIssuesReferences, PR title/body, and branch name."""
    issue_numbers: list[int] = []

    def add_num(n: int):
        if n > 0 and n not in issue_numbers:
            issue_numbers.append(n)

    # 1. Native GitHub closing issue references
    for ref in pr_context.get("closingIssuesReferences") or []:
        if isinstance(ref, dict) and isinstance(ref.get("number"), int):
            add_num(ref["number"])

    # 2. Parse PR title and body for issue references
    text_to_scan = f"{pr_context.get('title', '')}\n{pr_context.get('body', '')}"

    # Full GitHub issue URLs: https://github.com/owner/repo/issues/70
    for match in re.finditer(r"github\.com/[^/\s]+/[^/\s]+/issues/(\d+)", text_to_scan, re.IGNORECASE):
        add_num(int(match.group(1)))

    # Keyword + optional '#' + number: "Implements Issue #70", "Issue 70", "Fixes #70", "Closes #70", "Resolves #70"
    for match in re.finditer(
        r"\b(?:issue|implements(?:\s+issue)?|close[sd]?|fix(?:e[sd])?|resolve[sd]?|ref(?:erence)?s?)\s*:?\s*#?(\d+)\b",
        text_to_scan,
        re.IGNORECASE,
    ):
        add_num(int(match.group(1)))

    # Standalone #70 anywhere in title or body
    for match in re.finditer(r"(?:^|[\s(\[/:—–-])#(\d+)\b", text_to_scan):
        add_num(int(match.group(1)))

    # 3. Parse branch name (e.g., "feat/issue-70-executive-briefings", "issue-70", "fix/70-bug")
    branch_name = pr_context.get("headRefName") or ""
    for match in re.finditer(r"(?:issue[-_/]|^(?:feat|fix|bugfix|hotfix|chore)/#?)(\d+)\b", branch_name, re.IGNORECASE):
        add_num(int(match.group(1)))

    return issue_numbers


def fetch_issue_details(repo: str, issue_numbers: list[int]) -> tuple[list[dict], bool]:
    """
    Fetches the original issue specification (title, body, labels, comments) for each linked issue.
    Returns (issues_list, permission_denied_flag).
    """
    if not repo or not issue_numbers:
        return [], False

    issues: list[dict] = []
    permission_denied = False

    for num in issue_numbers:
        # Try gh issue view (GraphQL - automatically excludes PRs)
        out, err1 = run_gh_command(
            [
                "issue",
                "view",
                str(num),
                "--repo",
                repo,
                "--json",
                "number,title,body,state,labels,comments",
            ]
        )
        if out:
            try:
                issue_data = json.loads(out)
                issues.append(issue_data)
                continue
            except Exception as e:
                print(f"Warning: Failed to parse gh issue view for #{num}: {e}")

        # Fallback to REST API if GraphQL failed
        rest_out, err2 = run_gh_command(["api", f"repos/{repo}/issues/{num}"])
        if rest_out:
            try:
                rest_data = json.loads(rest_out)
                # Ensure it's an actual issue and not a pull request
                if "pull_request" not in rest_data:
                    comments_list = []
                    comments_out, _ = run_gh_command(["api", f"repos/{repo}/issues/{num}/comments"])
                    if comments_out:
                        try:
                            raw_comments = json.loads(comments_out)
                            for c in raw_comments:
                                comments_list.append(
                                    {
                                        "author": {"login": (c.get("user") or {}).get("login", "unknown")},
                                        "body": c.get("body", ""),
                                    }
                                )
                        except Exception:
                            pass
                    issues.append(
                        {
                            "number": rest_data.get("number", num),
                            "title": rest_data.get("title", ""),
                            "body": rest_data.get("body", ""),
                            "state": rest_data.get("state", ""),
                            "labels": [{"name": lbl.get("name", "")} for lbl in (rest_data.get("labels") or [])],
                            "comments": comments_list,
                        }
                    )
                    continue
            except Exception as e:
                print(f"Warning: Failed to parse REST issue #{num}: {e}")

        combined_err = f"{err1} {err2}".lower()
        if any(
            perm_err in combined_err
            for perm_err in [
                "resource not accessible by integration",
                "http 403",
                "403",
                "insufficient_scopes",
                "forbidden",
            ]
        ):
            permission_denied = True
            print(
                f"Notice: Issue #{num} is referenced in the PR, but GITHUB_TOKEN lacks 'issues: read' permission "
                f"to fetch its details from {repo}."
            )

    return issues, permission_denied


def has_bypass_keyword(pr_context: dict, bypass_keyword: str) -> bool:
    """Checks if the bypass keyword is present in the PR description or title."""
    if not bypass_keyword:
        return False
    combined = f"{pr_context.get('title', '')}\n{pr_context.get('body', '')}"
    return bypass_keyword.lower() in combined.lower()


def count_prior_missing_issue_reviews(reviews: list[dict]) -> int:
    """Counts how many prior reviews on this PR flagged a missing linked issue."""
    count = 0
    for rev in reviews or []:
        body = (rev.get("body") or "") if isinstance(rev, dict) else ""
        if (
            MISSING_ISSUE_MARKER in body
            or LEGACY_MISSING_ISSUE_MARKER in body
            or "No Linked GitHub Issue Found" in body
        ):
            count += 1
    return count


def build_missing_issue_banner(unlinked_review_count: int, bypass_keyword: str) -> str:
    """Builds the strict-mode warning banner when no issue is linked to the PR."""
    lines = [
        MISSING_ISSUE_MARKER,
        "> [!WARNING]",
        "> ### ❌ Strict Issue Verification: No Linked GitHub Issue Found",
        "> This pull request does not reference an original GitHub Issue. To ensure all requested features and acceptance criteria from the original issue are properly verified (and not just the PR description), please link the issue in your PR description (e.g., `Closes #<issue-number>` or `Fixes #<issue-number>`).",
    ]
    if unlinked_review_count >= 2:
        lines.extend(
            [
                ">",
                f"> 💡 **Bypass Option (Review #{unlinked_review_count} without a linked issue):** "
                f"If this PR intentionally does not have a corresponding issue (e.g., a minor chore, docs update, or hotfix), "
                f"you can bypass this requirement by adding `{bypass_keyword}` anywhere in the PR description.",
            ]
        )
    return "\n".join(lines) + "\n\n---\n\n"


def format_issues_for_prompt(issues: list[dict]) -> str:
    """Formats fetched GitHub Issues into a structured markdown block for the LLM prompt."""
    blocks = []
    for issue in issues:
        num = issue.get("number")
        title = issue.get("title") or "(Untitled)"
        state = issue.get("state") or "OPEN"
        labels = [
            lbl.get("name", "")
            for lbl in (issue.get("labels") or [])
            if isinstance(lbl, dict) and lbl.get("name")
        ]
        label_str = f" | Labels: {', '.join(labels)}" if labels else ""
        body = (issue.get("body") or "").strip() or "(No issue description provided.)"

        comment_lines = []
        for idx, c in enumerate(issue.get("comments") or [], start=1):
            if isinstance(c, dict) and (c.get("body") or "").strip():
                author = (c.get("author") or {}).get("login", "user")
                comment_lines.append(f"  - Comment #{idx} (@{author}): {c['body'].strip()}")

        comments_section = (
            "\n\n### Clarifying Issue Comments:\n" + "\n".join(comment_lines)
            if comment_lines
            else ""
        )

        blocks.append(
            f"## Original Issue #{num}: {title} (State: {state}{label_str})\n"
            f"### Original Issue Description & Acceptance Criteria:\n"
            f"{body}"
            f"{comments_section}"
        )
    return "\n\n---\n\n".join(blocks)


def parse_exclude_patterns(raw_patterns: str) -> list[str]:
    """Parses comma-separated glob patterns from EXCLUDE_PATHS."""
    return [pat.strip() for pat in str(raw_patterns or "").split(",") if pat.strip()]


def _matches_any_pattern(filepath: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """Checks if a normalized filepath or its basename matches any glob pattern."""
    norm = filepath.replace("\\", "/").lstrip("./").lower()
    basename = posixpath.basename(norm)
    return any(
        fnmatch.fnmatch(norm, pat.lower()) or fnmatch.fnmatch(basename, pat.lower())
        for pat in patterns
    )


def should_exclude_path(filepath: str, extra_patterns: list[str]) -> bool:
    """Checks whether a changed filepath should be excluded from LLM diff review."""
    norm = filepath.replace("\\", "/").lstrip("./").lower()
    if posixpath.basename(norm) in DEFAULT_EXCLUDE_BASENAMES:
        return True
    return _matches_any_pattern(filepath, (*DEFAULT_EXCLUDE_PATTERNS, *extra_patterns))


def is_compiled_artifact_path(filepath: str) -> bool:
    """Checks whether a filepath matches typical compiled/bundled frontend artifact paths."""
    return _matches_any_pattern(filepath, COMPILED_ARTIFACT_PATTERNS)


def is_compiled_artifact_added_or_modified(filepath: str, patch: str) -> bool:
    """Returns True only when a compiled artifact is added or modified (not deleted)."""
    if not is_compiled_artifact_path(filepath):
        return False
    return re.search(r"^deleted file mode ", patch, re.MULTILINE) is None


def compute_diff_line_stats(files: list[tuple[str, str]]) -> tuple[int, int]:
    """Counts added and deleted lines across unified diff patches."""
    additions = 0
    deletions = 0
    for _, patch in files:
        for line in patch.splitlines():
            if line.startswith("+") and not line.startswith("+++ "):
                additions += 1
            elif line.startswith("-") and not line.startswith("--- "):
                deletions += 1
    return additions, deletions


def classify_pr_size(
    changed_lines: int, file_count: int, warn_lines: int = 800, warn_files: int = 12
) -> str:
    """Classifies PR diff size as S, M, L, or XL."""
    if changed_lines > warn_lines or file_count > warn_files:
        return "XL"
    if changed_lines > 600 or file_count > 10:
        return "L"
    if changed_lines > 200 or file_count > 4:
        return "M"
    return "S"


def build_pr_size_callout(
    kept_files: list[tuple[str, str]],
    excluded_files: list[tuple[str, str]],
    warn_lines: int = 800,
    warn_files: int = 12,
) -> str:
    """Builds a compact PR size badge or warning callout for oversized/bundled diffs."""
    additions, deletions = compute_diff_line_stats(kept_files)
    changed_lines = additions + deletions
    file_count = len(kept_files)
    size_label = classify_pr_size(changed_lines, file_count, warn_lines, warn_files)

    compiled_artifacts = [
        path
        for path, patch in (*kept_files, *excluded_files)
        if is_compiled_artifact_added_or_modified(path, patch)
    ]

    summary_line = (
        f"> **PR Size:** `{size_label}` "
        f"(+{additions:,}/-{deletions:,} lines across {file_count} source file(s))"
    )
    if size_label != "XL" and not compiled_artifacts:
        return summary_line

    notes: list[str] = ["> [!NOTE]", summary_line]
    if size_label == "XL":
        notes.append(
            f"> - Diff exceeds the recommended threshold (>{warn_lines:,} changed source lines "
            f"or >{warn_files} files). Consider breaking future large changes into smaller, "
            f"focused PRs for faster human and AI review."
        )
    if compiled_artifacts:
        shown = ", ".join(f"`{p}`" for p in compiled_artifacts[:5])
        extra = f" (+{len(compiled_artifacts) - 5} more)" if len(compiled_artifacts) > 5 else ""
        notes.append(
            f"> - Compiled/bundled artifact(s) modified ({shown}{extra}). "
            f"Consider gitignoring build outputs and generating them in CI/Docker."
        )
    return "\n".join(notes)


def parse_diff_files(diff_content: str) -> list[tuple[str, str]]:
    """Splits a unified git diff into (filepath, file_patch) tuples along `diff --git` headers."""
    pattern = re.compile(
        r'^diff --git (?:"a/(.+?)"|a/(.+?)) (?:"b/(.+?)"|b/(.+?))$',
        re.MULTILINE,
    )
    matches = list(pattern.finditer(diff_content))
    if not matches:
        return [("<diff>", diff_content)]

    files: list[tuple[str, str]] = []
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(diff_content)
        filepath = match.group(3) or match.group(4) or match.group(1) or match.group(2) or "<unknown>"
        files.append((filepath, diff_content[start:end].rstrip("\n")))
    return files


def filter_diff_files(
    files: list[tuple[str, str]], extra_patterns: list[str]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Partitions diff files into (kept_files, excluded_files)."""
    kept: list[tuple[str, str]] = []
    excluded: list[tuple[str, str]] = []
    for filepath, patch in files:
        if filepath != "<diff>" and should_exclude_path(filepath, extra_patterns):
            excluded.append((filepath, patch))
        else:
            kept.append((filepath, patch))
    return kept, excluded


def annotate_diff_with_line_numbers(
    files: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], dict[str, set[int]]]:
    """Prefixes right-side diff lines with `[L<n>]` and returns valid target line sets per file."""
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    annotated_files: list[tuple[str, str]] = []
    valid_lines: dict[str, set[int]] = {}

    for filepath, patch in files:
        norm_fp = filepath.replace("\\", "/").lstrip("./")
        file_line_set = valid_lines.setdefault(norm_fp, set())
        out_lines: list[str] = []
        in_hunk = False
        new_line: int | None = None

        for line in patch.splitlines():
            hunk_match = hunk_re.match(line)
            if hunk_match:
                new_line = int(hunk_match.group(1))
                in_hunk = True
                out_lines.append(line)
            elif in_hunk and new_line is not None:
                if line.startswith("+") or line.startswith(" "):
                    out_lines.append(f"[L{new_line}] {line}")
                    file_line_set.add(new_line)
                    new_line += 1
                elif line.startswith("-") or line.startswith("\\"):
                    out_lines.append(line)
                else:
                    in_hunk = False
                    out_lines.append(line)
            else:
                out_lines.append(line)

        annotated_files.append((filepath, "\n".join(out_lines)))

    return annotated_files, valid_lines


def build_inline_comments_instruction() -> str:
    """Returns prompt instructions for line-by-line diff citations and structured inline comments."""
    return (
        "5. Line-by-Line Pinpointing & Inline Diff Comments:\n"
        "   - Target file lines in the diff are prefixed with `[L<line_number>]`.\n"
        "   - Whenever you find a bug, missing requirement, edge case, or security issue, cite the exact `path:L<line_number>` in a `#### 📍 Line-by-Line Findings` section.\n"
        "   - Before your final `[APPROVED]` or `[CHANGES_REQUESTED]` token, include a fenced JSON block labeled ```` ```json:inline-comments ```` containing an array of line-level findings (or `[]` if none):\n"
        '     [{"path": "relative/file.py", "line": 42, "severity": "error|warning|info", "comment": "Actionable issue description", "suggestion": "optional exact replacement line(s)"}]'
    )


def _insert_before_verdict_token(text: str, extra_section: str) -> str:
    """Inserts a markdown section immediately before the trailing verdict token if present."""
    for token in ("[CHANGES_REQUESTED]", "[APPROVED]"):
        idx = text.rfind(token)
        if idx != -1:
            before = text[:idx].rstrip()
            after = text[idx:].lstrip()
            return f"{before}\n\n{extra_section}\n\n{after}"
    return f"{text.rstrip()}\n\n{extra_section}"


def extract_inline_comments(
    review_text: str,
    valid_diff_lines: dict[str, set[int]],
    reviewer_label: str,
    used_model: str,
    provider_display_name: str,
) -> tuple[str, list[dict], list[str]]:
    """Extracts and validates ```json:inline-comments``` blocks into GitHub PR review comment payloads."""
    matches = list(INLINE_COMMENTS_BLOCK_RE.finditer(review_text))
    if not matches:
        return review_text, [], []

    cleaned_text = INLINE_COMMENTS_BLOCK_RE.sub("", review_text)
    cleaned_text = re.sub(r"\n{3,}", "\n\n", cleaned_text).strip()

    bare_model = _clean_model_name(used_model)
    source_name = provider_display_name or (
        used_model.split(" via ", 1)[1] if " via " in used_model else "Google AI Studio"
    )
    label = reviewer_label or "AI Reviewer"

    inline_comments: list[dict] = []
    unanchored_notes: list[str] = []

    for match in matches:
        raw_json = match.group(1).strip()
        if not raw_json:
            continue
        try:
            parsed = json.loads(raw_json)
        except Exception:
            continue
        if not isinstance(parsed, list):
            continue

        for item in parsed:
            if not isinstance(item, dict):
                continue
            raw_path = str(item.get("path") or "").strip()
            comment_text = str(item.get("comment") or "").strip()
            try:
                line_num = int(item.get("line"))
            except (TypeError, ValueError):
                continue
            if not raw_path or not comment_text or line_num <= 0:
                continue

            norm_path = raw_path.replace("\\", "/").lstrip("./")
            if norm_path not in valid_diff_lines and norm_path[:2] in ("a/", "b/") and norm_path[2:] in valid_diff_lines:
                norm_path = norm_path[2:]

            file_valid_lines = valid_diff_lines.get(norm_path, set())
            target_line: int | None = None
            if line_num in file_valid_lines:
                target_line = line_num
            elif file_valid_lines:
                nearest = min(file_valid_lines, key=lambda v: abs(v - line_num))
                if abs(nearest - line_num) <= 3:
                    target_line = nearest

            severity_badge = str(item.get("severity") or "issue").strip().upper()
            if target_line is not None:
                header_line = (
                    f"🤖 **{label}** (`{bare_model}` via **{source_name}**) · `{severity_badge}`"
                )
                body_parts = [header_line, "", comment_text]
                suggestion = str(item.get("suggestion") or "").strip("\r\n")
                if suggestion:
                    body_parts.extend(["", f"```suggestion\n{suggestion}\n```"])
                inline_comments.append(
                    {
                        "path": norm_path,
                        "line": target_line,
                        "side": "RIGHT",
                        "body": "\n".join(body_parts),
                    }
                )
            else:
                unanchored_notes.append(f"- **{norm_path}:L{line_num}** ({severity_badge}): {comment_text}")

    if unanchored_notes:
        cleaned_text = _insert_before_verdict_token(
            cleaned_text,
            "#### 📌 Additional Line Notes\n" + "\n".join(unanchored_notes),
        )

    return cleaned_text, inline_comments, unanchored_notes


def build_diff_overview(diff_stat_text: str, excluded_files: list[tuple[str, str]]) -> str:
    """Builds a compact diff summary block including `git diff --stat` and excluded artifacts."""
    sections: list[str] = []
    trimmed_stat = (diff_stat_text or "").strip()
    if trimmed_stat:
        if len(trimmed_stat) > 6000:
            trimmed_stat = trimmed_stat[:6000] + "\n... [diff --stat truncated]"
        sections.append(f"### Git Diff Stat\n```text\n{trimmed_stat}\n```")

    if excluded_files:
        excluded_lines = [
            f"- `{path}` ({len(patch):,} chars excluded)" for path, patch in excluded_files
        ]
        sections.append(
            "### Excluded Generated / Lock / Vendor Files (Not Sent in Full)\n"
            + "\n".join(excluded_lines)
        )
    return "\n\n".join(sections)


def chunk_diff_files(
    files: list[tuple[str, str]], chunk_target_chars: int, max_chunks: int
) -> tuple[list[list[tuple[str, str]]], list[str]]:
    """Packs file patches cleanly along file boundaries into bounded chunks."""
    if not files:
        return [], []

    chunks: list[list[tuple[str, str]]] = []
    current_chunk: list[tuple[str, str]] = []
    current_chars = 0
    omitted_files: list[str] = []

    for idx, (filepath, raw_patch) in enumerate(files):
        patch = raw_patch
        if len(patch) > chunk_target_chars:
            suffix = (
                f"\n\n... [File diff truncated: {len(raw_patch):,} chars exceed "
                f"{chunk_target_chars:,} chars per-chunk limit] ..."
            )
            keep = max(0, chunk_target_chars - len(suffix))
            patch = (patch[:keep] + suffix) if keep > 0 else patch[:chunk_target_chars]

        projected = current_chars + len(patch) + (1 if current_chunk else 0)
        if current_chunk and projected > chunk_target_chars:
            chunks.append(current_chunk)
            if len(chunks) >= max_chunks:
                omitted_files.extend(path for path, _ in files[idx:])
                current_chunk = []
                break
            current_chunk = [(filepath, patch)]
            current_chars = len(patch)
        else:
            current_chunk.append((filepath, patch))
            current_chars = projected

    if current_chunk:
        if len(chunks) < max_chunks:
            chunks.append(current_chunk)
        else:
            omitted_files.extend(path for path, _ in current_chunk)

    return chunks, omitted_files


def attempt_review(client: genai.Client, model: str, prompt: str, system_instruction: str) -> str:
    """Generates code review for a given model."""
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.1,
        safety_settings=[
            types.SafetySetting(
                category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"
            ),
            types.SafetySetting(
                category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"
            ),
            types.SafetySetting(
                category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"
            ),
            types.SafetySetting(
                category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"
            ),
        ],
    )
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=config,
    )
    return response.text or "No review generated."


@dataclass(frozen=True)
class OpenAIFallbackConfig:
    """Configuration for optional OpenAI-compatible cross-provider PR review fallback."""

    base_url: str = ""
    api_key: str = ""
    model: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.base_url.strip() and self.api_key.strip() and self.model.strip())


@dataclass(frozen=True)
class ProviderSpec:
    """Configuration for an AI review provider (Gemini or OpenAI-compatible)."""

    provider_id: str
    display_name: str
    kind: str  # "gemini" or "openai"
    api_key: str
    models: tuple[str, ...]
    base_url: str = ""

    @property
    def enabled(self) -> bool:
        if not self.api_key.strip() or not self.models:
            return False
        if self.kind == "openai" and not self.base_url.strip():
            return False
        return True


def build_provider_chain(
    env: dict[str, str] | None = None,
    openai_fallback: OpenAIFallbackConfig | None = None,
) -> list[ProviderSpec]:
    """Builds the ordered list of enabled AI review providers from environment variables."""
    source_env = os.environ if env is None else env
    available: dict[str, ProviderSpec] = {}

    available["gemini"] = ProviderSpec(
        provider_id="gemini",
        display_name="Google AI Studio",
        kind="gemini",
        api_key=source_env.get("GEMINI_API_KEY", "").strip(),
        models=tuple(
            build_candidate_models(
                source_env.get("GEMINI_MODEL", DEFAULT_PRIMARY_MODEL),
                source_env.get("GEMINI_FALLBACK_MODEL", DEFAULT_FALLBACK_MODELS),
            )
        ),
    )

    preset_defs = (
        ("groq", "Groq", "GROQ_API_KEY", "GROQ_BASE_URL", DEFAULT_GROQ_BASE_URL, "GROQ_MODEL", DEFAULT_GROQ_MODELS),
        (
            "openrouter",
            "OpenRouter",
            "OPENROUTER_API_KEY",
            "OPENROUTER_BASE_URL",
            DEFAULT_OPENROUTER_BASE_URL,
            "OPENROUTER_MODEL",
            DEFAULT_OPENROUTER_MODELS,
        ),
        (
            "cerebras",
            "Cerebras",
            "CEREBRAS_API_KEY",
            "CEREBRAS_BASE_URL",
            DEFAULT_CEREBRAS_BASE_URL,
            "CEREBRAS_MODEL",
            DEFAULT_CEREBRAS_MODELS,
        ),
        (
            "mistral",
            "Mistral",
            "MISTRAL_API_KEY",
            "MISTRAL_BASE_URL",
            DEFAULT_MISTRAL_BASE_URL,
            "MISTRAL_MODEL",
            DEFAULT_MISTRAL_MODELS,
        ),
    )
    for pid, display, key_env, base_env, default_base, model_env, default_models in preset_defs:
        available[pid] = ProviderSpec(
            provider_id=pid,
            display_name=display,
            kind="openai",
            api_key=source_env.get(key_env, "").strip(),
            base_url=source_env.get(base_env, default_base).strip() or default_base,
            models=tuple(parse_model_list(source_env.get(model_env, ""), default_models)),
        )

    custom_cfg = (
        openai_fallback
        if openai_fallback is not None
        else OpenAIFallbackConfig(
            base_url=source_env.get("FALLBACK_OPENAI_BASE_URL", ""),
            api_key=source_env.get("FALLBACK_OPENAI_API_KEY", ""),
            model=source_env.get("FALLBACK_OPENAI_MODEL", ""),
        )
    )
    available["custom-openai"] = ProviderSpec(
        provider_id="custom-openai",
        display_name="OpenAI-compatible fallback",
        kind="openai",
        api_key=custom_cfg.api_key.strip(),
        base_url=custom_cfg.base_url.strip(),
        models=tuple(parse_model_list(custom_cfg.model)),
    )

    raw_order = source_env.get("PROVIDER_ORDER", DEFAULT_PROVIDER_ORDER)
    requested_order: list[str] = []
    for item in str(raw_order or "").split(","):
        pid = item.strip().lower()
        if pid and pid not in requested_order:
            requested_order.append(pid)
    for pid in DEFAULT_PROVIDER_ORDER.split(","):
        if pid not in requested_order:
            requested_order.append(pid)

    return [available[pid] for pid in requested_order if pid in available and available[pid].enabled]


def attempt_openai_compatible_review(
    cfg: OpenAIFallbackConfig, prompt: str, system_instruction: str, timeout_sec: int
) -> str:
    """Calls an OpenAI-compatible chat completions endpoint via Python stdlib HTTP."""
    base = cfg.base_url.strip().rstrip("/")
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Invalid OpenAI-compatible base URL: {cfg.base_url!r}")

    endpoint = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
    payload = json.dumps(
        {
            "model": cfg.model.strip(),
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.api_key.strip()}",
            "User-Agent": "pr-council/1.3",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw_body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = str(e)
        raise RuntimeError(f"HTTP {e.code} from OpenAI-compatible fallback: {err_body}") from e

    data = json.loads(raw_body)
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise RuntimeError("OpenAI-compatible fallback returned no choices.")
    content = ((choices[0].get("message") or {}).get("content") or "").strip()
    return content or "No review generated."


def _format_provider_model_label(provider: ProviderSpec, model: str) -> str:
    """Formats the model attribution label returned by invoke_with_fallback."""
    if provider.kind == "gemini":
        return model
    if provider.provider_id == "custom-openai":
        return f"{model} (OpenAI-compatible fallback)"
    return f"{model} via {provider.display_name}"


def invoke_with_fallback(
    client: genai.Client | None,
    candidate_models: list[str],
    prompt: str,
    system_instruction: str,
    max_retries: int,
    base_delay: int,
    api_key: str = "",
    request_timeout_sec: int = 60,
    openai_fallback: OpenAIFallbackConfig | None = None,
    providers: list[ProviderSpec] | None = None,
    extra_secrets: tuple[str, ...] = (),
) -> tuple[str | None, str, str]:
    """Invokes candidate models across configured providers in priority order."""
    if providers is None:
        active_providers: list[ProviderSpec] = []
        if client is not None or api_key:
            active_providers.append(
                ProviderSpec(
                    provider_id="gemini",
                    display_name="Google AI Studio",
                    kind="gemini",
                    api_key=api_key,
                    models=tuple(candidate_models),
                )
            )
        if openai_fallback and openai_fallback.enabled:
            active_providers.append(
                ProviderSpec(
                    provider_id="custom-openai",
                    display_name="OpenAI-compatible fallback",
                    kind="openai",
                    api_key=openai_fallback.api_key.strip(),
                    base_url=openai_fallback.base_url.strip(),
                    models=tuple(parse_model_list(openai_fallback.model)),
                )
            )
    else:
        active_providers = list(providers)

    if not active_providers:
        fallback_name = candidate_models[0] if candidate_models else DEFAULT_PRIMARY_MODEL
        return None, fallback_name, "No AI review provider configured."

    secrets_list: list[str] = []
    for secret in [
        api_key,
        openai_fallback.api_key if openai_fallback else "",
        *(p.api_key for p in active_providers),
        *extra_secrets,
    ]:
        if secret and secret not in secrets_list:
            secrets_list.append(secret)

    total_candidate_count = sum(len(p.models) for p in active_providers)
    per_model_attempts = (
        max(1, min(2, max_retries)) if total_candidate_count > 1 else max(1, max_retries)
    )
    used_model = _format_provider_model_label(active_providers[0], active_providers[0].models[0])
    last_err = ""

    for provider_idx, provider in enumerate(active_providers):
        fatal_auth = False
        for model_idx, current_model in enumerate(provider.models):
            used_model = _format_provider_model_label(provider, current_model)
            print(f"Attempting review with model: {current_model} ({provider.display_name})")

            for attempt in range(1, per_model_attempts + 1):
                print(
                    f"[{attempt}/{per_model_attempts}] Sending review request to {current_model} "
                    f"via {provider.display_name}..."
                )
                try:
                    if provider.kind == "gemini":
                        if client is None:
                            raise RuntimeError("Gemini client is not initialized.")
                        text = attempt_review(client, current_model, prompt, system_instruction)
                    else:
                        cfg = OpenAIFallbackConfig(
                            base_url=provider.base_url,
                            api_key=provider.api_key,
                            model=current_model,
                        )
                        text = attempt_openai_compatible_review(
                            cfg, prompt, system_instruction, request_timeout_sec
                        )
                    return text, used_model, ""
                except Exception as e:
                    sanitized = sanitize_error(str(e), *secrets_list)
                    last_err = sanitized

                    if is_fatal_auth_error(sanitized):
                        print(
                            f"Fatal authentication/permission error on {provider.display_name} "
                            f"({current_model}): {sanitized}"
                        )
                        if provider_idx >= len(active_providers) - 1:
                            return None, used_model, sanitized
                        fatal_auth = True
                        break

                    if is_model_unavailable_error(sanitized):
                        print(
                            f"Model {current_model} unavailable on {provider.display_name} "
                            f"({sanitized}). Failing over immediately..."
                        )
                        break

                    if is_transient_error(sanitized):
                        if attempt < per_model_attempts:
                            delay = min(base_delay * attempt, 90)
                            print(
                                f"Transient error on {current_model} ({sanitized}). "
                                f"Waiting {delay}s before retry ({attempt}/{per_model_attempts})..."
                            )
                            time.sleep(delay)
                        else:
                            print(f"Exhausted retries on {current_model} ({sanitized}).")
                    else:
                        print(f"Non-transient error on {current_model} ({sanitized}).")
                        break

            if fatal_auth:
                break
            if model_idx < len(provider.models) - 1:
                print(
                    f"Switching to fallback model: {provider.models[model_idx + 1]} "
                    f"({provider.display_name})..."
                )

        if provider_idx < len(active_providers) - 1:
            next_provider = active_providers[provider_idx + 1]
            print(
                f"Switching to fallback provider: {next_provider.display_name} "
                f"({next_provider.models[0]})..."
            )

    return None, used_model, last_err


def build_issue_context_and_rule(
    linked_issues: list[dict],
    candidate_issue_nums: list[int],
    issue_perm_denied: bool,
    bypass_active: bool,
    bypass_keyword: str,
) -> tuple[str, str]:
    """Builds the original-issue specification block and matching review rule."""
    if linked_issues:
        return (
            format_issues_for_prompt(linked_issues),
            "1. Original Issue Alignment & Feature Completeness (HIGHEST PRIORITY):\n"
            "   - Treat the ORIGINAL GITHUB ISSUE(S) provided below as the single source of truth — NOT the PR description.\n"
            "   - Development agents or developers may misinterpret the original issue and write a PR description that matches their flawed implementation. You must verify the code diff directly against the original issue requirements and comments.\n"
            "   - Include a dedicated '#### 📋 Issue Requirements Checklist' section in your review that lists every feature, requirement, and acceptance criterion from the original issue(s) and marks each as `✅ Implemented`, `⚠️ Partial`, or `❌ Missing / Misinterpreted`.\n"
            "   - If ANY requirement from the original issue is missing, only partially implemented, or misinterpreted, you MUST output `[CHANGES_REQUESTED]`.",
        )

    if candidate_issue_nums and issue_perm_denied:
        issue_refs_str = ", ".join(f"#{n}" for n in candidate_issue_nums)
        return (
            f"PR explicitly links to Issue {issue_refs_str}, though full issue body could not be fetched because the "
            f"PR branch's workflow permissions did not yet include `issues: read`.",
            f"1. Completeness (Linked to Issue {issue_refs_str}):\n"
            "   - The developer HAS linked the issue (`" + issue_refs_str + "`). Do NOT penalize for missing issue link.\n"
            "   - Verify that all features and acceptance criteria described in the PR for this issue are completely implemented in the diff.",
        )

    if bypass_active:
        return (
            f"No GitHub Issue is linked (developer explicitly bypassed strict issue linking via `{bypass_keyword}`).",
            "1. Completeness: Ensure the code changes completely and accurately solve the goal described in the PR title and description.",
        )

    return (
        "ERROR: No original GitHub Issue is linked to this PR, and no bypass keyword was provided.",
        "1. Missing Linked Issue (STRICT VIOLATION):\n"
        "   - This repository requires every PR to link its original GitHub Issue so features can be verified against the original specification.\n"
        "   - Still perform a full, thorough review of the code diff below for quality, testing, and security so the developer receives complete feedback, wrapped up with `[CHANGES_REQUESTED]`.",
    )


def run_chunked_review(
    client: genai.Client | None,
    candidate_models: list[str],
    chunks: list[list[tuple[str, str]]],
    omitted_files: list[str],
    diff_overview: str,
    issue_context_block: str,
    pr_title: str,
    pr_body: str,
    system_instruction: str,
    max_retries: int,
    base_delay: int,
    api_key: str,
    chunk_pause: int,
    request_timeout_sec: int = 60,
    openai_fallback: OpenAIFallbackConfig | None = None,
    providers: list[ProviderSpec] | None = None,
    extra_context: str = "",
    extra_secrets: tuple[str, ...] = (),
) -> tuple[str | None, str, str]:
    """Runs Map-Reduce review across file-boundary chunks and synthesizes one final verdict."""
    chunk_system_instruction = (
        "You are an expert Code Reviewer analyzing ONE chunk of a large pull request.\n"
        "For the files in this chunk, concisely list:\n"
        "1. Key changes and which Original Issue requirements they address.\n"
        "2. Any bugs, edge cases, architectural violations, or security issues (secrets/keys).\n"
        "3. Missing or weak unit tests for the code in this chunk.\n"
        "Do NOT output [APPROVED] or [CHANGES_REQUESTED] yet—your notes will be synthesized across all chunks."
    )

    chunk_notes: list[str] = []
    failed_notes: list[str] = []
    last_model = (
        candidate_models[0]
        if candidate_models
        else (providers[0].models[0] if providers else DEFAULT_PRIMARY_MODEL)
    )
    last_err = ""

    for idx, chunk_files in enumerate(chunks, start=1):
        chunk_paths = ", ".join(path for path, _ in chunk_files)
        chunk_diff = "\n".join(patch for _, patch in chunk_files)
        print(f"Reviewing chunk {idx}/{len(chunks)} ({len(chunk_files)} file(s), {len(chunk_diff):,} chars): {chunk_paths}")

        chunk_prompt = f"""
# Original GitHub Issue Specification (Source of Truth)
{issue_context_block}

# Pull Request Metadata
**PR Title:** {pr_title}

# Chunk {idx} of {len(chunks)} — Files: {chunk_paths}
```diff
{chunk_diff}
```
"""
        chunk_text, chunk_model, chunk_err = invoke_with_fallback(
            client,
            candidate_models,
            chunk_prompt,
            chunk_system_instruction,
            max_retries,
            base_delay,
            api_key=api_key,
            request_timeout_sec=request_timeout_sec,
            openai_fallback=openai_fallback,
            providers=providers,
            extra_secrets=extra_secrets,
        )
        last_model = chunk_model
        if chunk_text is None:
            last_err = chunk_err
            if is_fatal_auth_error(chunk_err):
                return None, chunk_model, chunk_err
            failed_notes.append(
                f"### Chunk {idx}/{len(chunks)} ({chunk_paths})\n"
                f"⚠️ Automated review unavailable for this chunk ({chunk_err})."
            )
        else:
            chunk_notes.append(
                f"### Chunk {idx}/{len(chunks)} ({chunk_paths}) [reviewed by {chunk_model}]\n"
                f"{chunk_text}"
            )

        if chunk_pause > 0 and idx < len(chunks):
            time.sleep(chunk_pause)

    if not chunk_notes:
        return None, last_model, last_err

    coverage_warnings: list[str] = []
    if omitted_files:
        omitted_list = "\n".join(f"- `{path}`" for path in omitted_files)
        coverage_warnings.append(
            "### Unreviewed Files (Chunk Limit Reached)\n"
            "Do NOT claim these files were verified:\n" + omitted_list
        )
    if failed_notes:
        coverage_warnings.extend(failed_notes)

    overview_section = f"\n{diff_overview}\n" if diff_overview else ""
    warnings_section = ("\n\n" + "\n\n".join(coverage_warnings)) if coverage_warnings else ""
    extra_section = f"\n{extra_context}\n" if extra_context else ""
    combined_notes = "\n\n---\n\n".join(chunk_notes)

    reduce_prompt = f"""
# Original GitHub Issue Specification (Source of Truth)
{issue_context_block}

# Pull Request Metadata (Developer / Agent Claim)
**PR Title:** {pr_title}
**PR Description:**
{pr_body}
{overview_section}{extra_section}
# Chunk Review Findings ({len(chunk_notes)}/{len(chunks)} chunks reviewed)
Synthesize the chunk findings below into a single coherent code review against the Original Issue and critical rules.
{warnings_section}

{combined_notes}
"""
    return invoke_with_fallback(
        client,
        candidate_models,
        reduce_prompt,
        system_instruction,
        max_retries,
        base_delay,
        api_key=api_key,
        request_timeout_sec=request_timeout_sec,
        openai_fallback=openai_fallback,
        providers=providers,
        extra_secrets=extra_secrets,
    )


@dataclass(frozen=True)
class ProviderReviewResult:
    """Outcome of running a code review on a single AI provider."""

    provider_id: str
    display_name: str
    used_model: str
    review_text: str | None
    error: str = ""
    slot_label: str = ""
    fallback_from: tuple[str, ...] = ()
    subagents_used: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.review_text is not None

    @property
    def action(self) -> str:
        if not self.review_text:
            return "comment"
        return resolve_review_action(self.review_text)


def _clean_model_name(used_model: str) -> str:
    """Extracts the bare model identifier from a provider-annotated model label."""
    cleaned = used_model.replace(" (OpenAI-compatible fallback)", "")
    if " via " in cleaned:
        cleaned = cleaned.split(" via ", 1)[0]
    return cleaned.strip()


def run_single_provider_review(
    provider: ProviderSpec,
    client: genai.Client | None,
    prompt: str,
    system_instruction: str,
    max_retries: int,
    base_delay: int,
    api_key: str,
    request_timeout_sec: int,
    chunks: list[list[tuple[str, str]]] | None = None,
    omitted_files: list[str] | None = None,
    diff_overview: str = "",
    issue_context_block: str = "",
    pr_title: str = "",
    pr_body: str = "",
    chunk_pause: int = 0,
    extra_context: str = "",
    extra_secrets: tuple[str, ...] = (),
) -> ProviderReviewResult:
    """Executes a full review (single-shot or chunked) against a single provider."""
    provider_api_key = provider.api_key if provider.kind == "gemini" else api_key
    if chunks:
        text, used_model, err = run_chunked_review(
            client=client,
            candidate_models=list(provider.models),
            chunks=chunks,
            omitted_files=omitted_files or [],
            diff_overview=diff_overview,
            issue_context_block=issue_context_block,
            pr_title=pr_title,
            pr_body=pr_body,
            system_instruction=system_instruction,
            max_retries=max_retries,
            base_delay=base_delay,
            api_key=provider_api_key,
            chunk_pause=chunk_pause,
            request_timeout_sec=request_timeout_sec,
            providers=[provider],
            extra_context=extra_context,
            extra_secrets=extra_secrets,
        )
    else:
        text, used_model, err = invoke_with_fallback(
            client,
            list(provider.models),
            prompt,
            system_instruction,
            max_retries,
            base_delay,
            api_key=provider_api_key,
            request_timeout_sec=request_timeout_sec,
            providers=[provider],
            extra_secrets=extra_secrets,
        )
    return ProviderReviewResult(
        provider_id=provider.provider_id,
        display_name=provider.display_name,
        used_model=used_model,
        review_text=text,
        error=err,
    )


def run_multi_reviewer_pipeline(
    providers: list[ProviderSpec],
    client: genai.Client | None,
    prompt: str,
    system_instruction: str,
    max_retries: int,
    base_delay: int,
    api_key: str,
    request_timeout_sec: int,
    main_reviewers_count: int = DEFAULT_MAIN_REVIEWERS,
    extra_providers_mode: str = DEFAULT_EXTRA_PROVIDERS_MODE,
    chunks: list[list[tuple[str, str]]] | None = None,
    omitted_files: list[str] | None = None,
    diff_overview: str = "",
    issue_context_block: str = "",
    pr_title: str = "",
    pr_body: str = "",
    chunk_pause: int = 0,
) -> list[ProviderReviewResult]:
    """Runs up to main_reviewers_count main reviewers, using extra providers as fallbacks or subagents."""
    target_count = max(1, main_reviewers_count)
    all_secrets = tuple(p.api_key for p in providers if p.api_key)

    if extra_providers_mode == "subagents" and len(providers) > target_count:
        main_candidates = providers[:target_count]
        subagent_candidates = providers[target_count:]
        subagent_system_instruction = (
            "You are a Subagent Code Reviewer assisting the primary PR reviewers.\n"
            "Analyze the PR diff against the Original Issue and concisely report:\n"
            "1. Exact `path:L<line_number>` locations of any bugs, edge cases, missing requirements, or security issues.\n"
            "2. Missing or weak unit tests.\n"
            "Keep your findings factual and concise—the Main Reviewers will verify and incorporate valid findings."
        )
        subagent_results = [
            run_single_provider_review(
                provider=provider,
                client=client,
                prompt=prompt,
                system_instruction=subagent_system_instruction,
                max_retries=max_retries,
                base_delay=base_delay,
                api_key=api_key,
                request_timeout_sec=request_timeout_sec,
                chunks=chunks,
                omitted_files=omitted_files,
                diff_overview=diff_overview,
                issue_context_block=issue_context_block,
                pr_title=pr_title,
                pr_body=pr_body,
                chunk_pause=chunk_pause,
                extra_secrets=all_secrets,
            )
            for provider in subagent_candidates
        ]
        succeeded_subagents = [r for r in subagent_results if r.succeeded]
        subagent_labels = tuple(r.used_model for r in succeeded_subagents)
        subagent_reports_block = ""
        if succeeded_subagents:
            report_sections = "\n\n---\n\n".join(
                f"### Subagent Report from {r.used_model} (Provider: {r.display_name})\n{r.review_text}"
                for r in succeeded_subagents
            )
            subagent_reports_block = (
                "\n\n# Subagent Reviewer Reports\n"
                "Subagent reviewers analyzed the PR first and reported these findings for your verification:\n\n"
                f"{report_sections}\n"
            )
        augmented_prompt = f"{prompt}{subagent_reports_block}" if prompt else ""

        succeeded_main: list[ProviderReviewResult] = []
        failed_results: list[ProviderReviewResult] = []
        failed_names: list[str] = []

        for provider in main_candidates:
            res = run_single_provider_review(
                provider=provider,
                client=client,
                prompt=augmented_prompt,
                system_instruction=system_instruction,
                max_retries=max_retries,
                base_delay=base_delay,
                api_key=api_key,
                request_timeout_sec=request_timeout_sec,
                chunks=chunks,
                omitted_files=omitted_files,
                diff_overview=diff_overview,
                issue_context_block=issue_context_block,
                pr_title=pr_title,
                pr_body=pr_body,
                chunk_pause=chunk_pause,
                extra_context=subagent_reports_block,
                extra_secrets=all_secrets,
            )
            if res.succeeded:
                slot_num = len(succeeded_main) + 1
                succeeded_main.append(
                    ProviderReviewResult(
                        provider_id=res.provider_id,
                        display_name=res.display_name,
                        used_model=res.used_model,
                        review_text=res.review_text,
                        error=res.error,
                        slot_label=f"Main Reviewer #{slot_num}",
                        fallback_from=tuple(failed_names),
                        subagents_used=subagent_labels,
                    )
                )
                failed_names = []
            else:
                failed_results.append(res)
                failed_names.append(provider.display_name)

        for sub_res in succeeded_subagents:
            if len(succeeded_main) >= target_count:
                break
            slot_num = len(succeeded_main) + 1
            succeeded_main.append(
                ProviderReviewResult(
                    provider_id=sub_res.provider_id,
                    display_name=sub_res.display_name,
                    used_model=sub_res.used_model,
                    review_text=sub_res.review_text,
                    error=sub_res.error,
                    slot_label=f"Main Reviewer #{slot_num}",
                    fallback_from=tuple(failed_names),
                )
            )
            failed_names = []

        return succeeded_main or (failed_results + [r for r in subagent_results if not r.succeeded])

    succeeded_main = []
    failed_results = []
    failed_since_last_slot: list[str] = []

    for provider in providers:
        if len(succeeded_main) >= target_count:
            break
        res = run_single_provider_review(
            provider=provider,
            client=client,
            prompt=prompt,
            system_instruction=system_instruction,
            max_retries=max_retries,
            base_delay=base_delay,
            api_key=api_key,
            request_timeout_sec=request_timeout_sec,
            chunks=chunks,
            omitted_files=omitted_files,
            diff_overview=diff_overview,
            issue_context_block=issue_context_block,
            pr_title=pr_title,
            pr_body=pr_body,
            chunk_pause=chunk_pause,
            extra_secrets=all_secrets,
        )
        if res.succeeded:
            slot_num = len(succeeded_main) + 1
            succeeded_main.append(
                ProviderReviewResult(
                    provider_id=res.provider_id,
                    display_name=res.display_name,
                    used_model=res.used_model,
                    review_text=res.review_text,
                    error=res.error,
                    slot_label=f"Main Reviewer #{slot_num}",
                    fallback_from=tuple(failed_since_last_slot),
                )
            )
            failed_since_last_slot = []
        else:
            failed_results.append(res)
            failed_since_last_slot.append(provider.display_name)

    return succeeded_main or failed_results


def build_consensus_summary_table(results: list[ProviderReviewResult]) -> str:
    """Builds a markdown summary table of individual provider verdicts in consensus mode."""
    lines = [
        "#### 🤝 Multi-Provider Consensus Summary",
        "| Provider | Model | Verdict |",
        "|---|---|---|",
    ]
    for r in results:
        model_col = f"`{_clean_model_name(r.used_model)}`"
        if not r.succeeded:
            verdict = "⚠️ Unavailable"
        elif r.action == "approve":
            verdict = "✅ Approved"
        elif r.action == "request-changes":
            verdict = "❌ Changes Requested"
        else:
            verdict = "💬 Comment"
        lines.append(f"| **{r.display_name}** | {model_col} | {verdict} |")
    return "\n".join(lines)


def run_consensus_review(
    providers: list[ProviderSpec],
    client: genai.Client | None,
    prompt: str,
    system_instruction: str,
    max_retries: int,
    base_delay: int,
    api_key: str,
    request_timeout_sec: int,
    consensus_max_providers: int = DEFAULT_CONSENSUS_MAX_PROVIDERS,
    chunks: list[list[tuple[str, str]]] | None = None,
    omitted_files: list[str] | None = None,
    diff_overview: str = "",
    issue_context_block: str = "",
    pr_title: str = "",
    pr_body: str = "",
    chunk_pause: int = 0,
) -> tuple[str | None, str, str]:
    """Queries up to consensus_max_providers providers and synthesizes their findings."""
    selected = providers[: max(1, consensus_max_providers)]
    all_secrets = tuple(p.api_key for p in providers if p.api_key)
    results = [
        run_single_provider_review(
            provider=provider,
            client=client,
            prompt=prompt,
            system_instruction=system_instruction,
            max_retries=max_retries,
            base_delay=base_delay,
            api_key=api_key,
            request_timeout_sec=request_timeout_sec,
            chunks=chunks,
            omitted_files=omitted_files,
            diff_overview=diff_overview,
            issue_context_block=issue_context_block,
            pr_title=pr_title,
            pr_body=pr_body,
            chunk_pause=chunk_pause,
            extra_secrets=all_secrets,
        )
        for provider in selected
    ]
    succeeded = [r for r in results if r.succeeded]
    if not succeeded:
        return None, results[-1].used_model, results[-1].error

    table_md = build_consensus_summary_table(results)
    if len(succeeded) == 1:
        return f"{table_md}\n\n{succeeded[0].review_text}", succeeded[0].used_model, ""

    overview_section = f"\n{diff_overview}\n" if diff_overview else ""
    findings_blocks = "\n\n---\n\n".join(
        f"### Findings from {r.used_model}\n{r.review_text}" for r in succeeded
    )
    synthesis_prompt = f"""
# Original GitHub Issue Specification (Source of Truth)
{issue_context_block}

# Pull Request Metadata (Developer / Agent Claim)
**PR Title:** {pr_title}
**PR Description:**
{pr_body}
{overview_section}
# Individual AI Provider Review Findings ({len(succeeded)}/{len(selected)} providers succeeded)
Synthesize the independent code review findings below into one cohesive, non-redundant final review against the Original Issue and critical rules.
Preserve the `#### 📋 Issue Requirements Checklist` section, include all valid bugs, edge cases, missing tests, or security issues identified by any provider, and output `[CHANGES_REQUESTED]` if any valid requirement, bug, or security concern remains unaddressed.

{findings_blocks}
"""
    synth_text, _, _ = invoke_with_fallback(
        client,
        list(selected[0].models),
        synthesis_prompt,
        system_instruction,
        max_retries,
        base_delay,
        api_key=api_key,
        request_timeout_sec=request_timeout_sec,
        providers=selected,
        extra_secrets=all_secrets,
    )
    if synth_text is None:
        synth_text = findings_blocks

    if any(r.action == "request-changes" for r in succeeded):
        if "[APPROVED]" in synth_text:
            synth_text = synth_text.replace("[APPROVED]", "[CHANGES_REQUESTED]")
        elif "[CHANGES_REQUESTED]" not in synth_text:
            synth_text = synth_text.rstrip() + "\n\n[CHANGES_REQUESTED]\n"

    consensus_label = f"Consensus: {', '.join(r.used_model for r in succeeded)}"
    return f"{table_md}\n\n{synth_text}", consensus_label, ""


def format_review_header(
    used_model: str,
    chunk_count: int = 0,
    slot_label: str = "",
    provider_display_name: str = "",
    fallback_from: tuple[str, ...] = (),
    subagents_used: tuple[str, ...] = (),
) -> str:
    """Builds the markdown header attributing the reviewer slot, source provider, and AI model."""
    header_suffix = f" · {chunk_count}-chunk review" if chunk_count > 1 else ""
    if (
        "(OpenAI-compatible fallback)" in used_model
        or " via " in used_model
        or used_model.startswith("Consensus:")
    ):
        h3 = f"### AI Code Review ({used_model}{header_suffix})"
    else:
        h3 = f"### AI Code Review ({used_model} via Google AI Studio{header_suffix})"

    if not slot_label and not provider_display_name and not fallback_from and not subagents_used:
        return h3 + "\n\n"

    bare_model = _clean_model_name(used_model)
    source_name = provider_display_name or (
        used_model.split(" via ", 1)[1] if " via " in used_model else "Google AI Studio"
    )
    badges: list[str] = []
    if slot_label:
        badges.append(f"**Reviewer:** `{slot_label}`")
    badges.append(f"**Source Provider:** `{source_name}`")
    badges.append(f"**AI Model:** `{bare_model}`")
    if fallback_from:
        badges.append(f"**Fallback For:** `{', '.join(fallback_from)}`")
    if subagents_used:
        badges.append(f"**Subagents Consulted:** `{', '.join(subagents_used)}`")
    return f"{h3}\n> {' · '.join(badges)}\n\n"


def resolve_review_action(review_text: str) -> str:
    """Determines the GitHub review action from the LLM response text."""
    if "[APPROVED]" in review_text:
        return "approve"
    if "[CHANGES_REQUESTED]" in review_text:
        return "request-changes"
    return "comment"


def finalize_review_body(
    review_text: str,
    used_model: str,
    api_succeeded: bool,
    missing_issue_strict: bool,
    unlinked_review_count: int,
    bypass_keyword: str,
    pr_size_callout: str,
    chunk_count: int = 0,
    slot_label: str = "",
    provider_display_name: str = "",
    fallback_from: tuple[str, ...] = (),
    subagents_used: tuple[str, ...] = (),
) -> tuple[str, str]:
    """Applies strict missing-issue policy, resolves review action, and prepends header & PR size callout."""
    if missing_issue_strict:
        banner = build_missing_issue_banner(unlinked_review_count, bypass_keyword)
        if "[APPROVED]" in review_text:
            review_text = review_text.replace("[APPROVED]", "[CHANGES_REQUESTED]")
        elif "[CHANGES_REQUESTED]" not in review_text:
            review_text = review_text.rstrip() + "\n\n[CHANGES_REQUESTED]\n"
        review_text = banner + review_text
        review_action = "request-changes"
    elif api_succeeded:
        review_action = resolve_review_action(review_text)
    else:
        review_action = "comment"

    header = format_review_header(
        used_model,
        chunk_count=chunk_count,
        slot_label=slot_label,
        provider_display_name=provider_display_name,
        fallback_from=fallback_from,
        subagents_used=subagents_used,
    )
    size_prefix = f"{pr_size_callout}\n\n" if pr_size_callout else ""
    return header + size_prefix + review_text, review_action


def cleanup_parallel_review_outputs() -> None:
    """Removes stale parallel review and payload artifacts from previous runs in the current directory."""
    for pattern in ("review_output_extra_*.md", "review_payload_*.json"):
        for artifact_path in glob.glob(pattern):
            try:
                os.remove(artifact_path)
            except OSError:
                pass
    if os.path.exists("review_outputs.json"):
        try:
            os.remove("review_outputs.json")
        except OSError:
            pass


def build_github_review_payload(body: str, action: str, comments: list[dict]) -> dict:
    """Builds a GitHub Pull Request Review API payload with optional inline line comments."""
    return {
        "event": ACTION_TO_GH_EVENT.get(action, "COMMENT"),
        "body": body,
        "comments": comments,
    }


def write_review_outputs(output_body: str, review_action: str) -> None:
    """Writes review_output.md and review_action.txt for the composite action post-step."""
    with open("review_output.md", "w") as f:
        f.write(output_body)
    with open("review_action.txt", "w") as f:
        f.write(review_action)


def write_parallel_review_outputs(
    extra_bodies: list[str],
    metadata: list[dict],
    payloads: list[dict] | None = None,
) -> None:
    """Writes extra provider review markdown files, API payloads, and summary metadata."""
    cleanup_parallel_review_outputs()
    for idx, body in enumerate(extra_bodies, start=1):
        with open(f"review_output_extra_{idx}.md", "w") as f:
            f.write(body)
    for idx, payload in enumerate(payloads or [], start=1):
        with open(f"review_payload_{idx}.json", "w") as f:
            json.dump(payload, f, indent=2)
    with open("review_outputs.json", "w") as f:
        json.dump(metadata, f, indent=2)


def _emit_reviewer_outputs(
    results: list[ProviderReviewResult],
    inline_comments_enabled: bool,
    valid_diff_lines: dict[str, set[int]],
    missing_issue_strict: bool,
    unlinked_review_count: int,
    bypass_keyword: str,
    pr_size_callout: str,
    chunk_count: int,
) -> None:
    """Extracts inline comments, finalizes review bodies, and emits files & GitHub API payloads."""
    bodies: list[str] = []
    actions: list[str] = []
    payloads: list[dict] = []
    metadata: list[dict] = []

    for idx, res in enumerate(results, start=1):
        raw_text = res.review_text or ""
        slot = res.slot_label or f"Main Reviewer #{idx}"
        if inline_comments_enabled:
            cleaned_text, comments, _ = extract_inline_comments(
                raw_text,
                valid_diff_lines,
                reviewer_label=slot,
                used_model=res.used_model,
                provider_display_name=res.display_name,
            )
        else:
            cleaned_text, comments = raw_text, []

        body, act = finalize_review_body(
            review_text=cleaned_text,
            used_model=res.used_model,
            api_succeeded=True,
            missing_issue_strict=missing_issue_strict,
            unlinked_review_count=unlinked_review_count,
            bypass_keyword=bypass_keyword,
            pr_size_callout=pr_size_callout,
            chunk_count=chunk_count,
            slot_label=res.slot_label,
            provider_display_name=res.display_name,
            fallback_from=res.fallback_from,
            subagents_used=res.subagents_used,
        )
        bodies.append(body)
        actions.append(act)
        payloads.append(build_github_review_payload(body, act, comments))
        metadata.append(
            {
                "slot_label": slot,
                "provider_id": res.provider_id,
                "display_name": res.display_name,
                "used_model": res.used_model,
                "action": act,
                "file": "review_output.md" if idx == 1 else f"review_output_extra_{idx - 1}.md",
                "payload_file": f"review_payload_{idx}.json",
                "inline_comment_count": len(comments),
            }
        )

    if "request-changes" in actions:
        aggregated_action = "request-changes"
    elif "approve" in actions:
        aggregated_action = "approve"
    else:
        aggregated_action = "comment"

    write_review_outputs(bodies[0], aggregated_action)
    write_parallel_review_outputs(bodies[1:], metadata, payloads=payloads)


def main():
    cleanup_parallel_review_outputs()
    max_retries = int(os.environ.get("GEMINI_MAX_RETRIES", "1"))
    base_delay = int(os.environ.get("GEMINI_RETRY_DELAY", "10"))
    request_timeout_sec = max(10, int(os.environ.get("GEMINI_REQUEST_TIMEOUT", "60")))
    single_shot_max_chars = int(os.environ.get("GEMINI_SINGLE_SHOT_MAX_CHARS", "80000"))
    chunk_target_chars = int(os.environ.get("GEMINI_CHUNK_TARGET_CHARS", "45000"))
    max_chunks = max(1, int(os.environ.get("GEMINI_MAX_CHUNKS", "8")))
    chunk_pause = max(0, int(os.environ.get("GEMINI_CHUNK_PAUSE", "2")))
    pr_size_warn_lines = max(1, int(os.environ.get("PR_SIZE_WARN_LINES", "800")))
    pr_size_warn_files = max(1, int(os.environ.get("PR_SIZE_WARN_FILES", "12")))
    review_mode = parse_review_mode(os.environ.get("REVIEW_MODE", DEFAULT_REVIEW_MODE))
    extra_providers_mode = parse_extra_providers_mode(
        os.environ.get("EXTRA_PROVIDERS_MODE", ""), review_mode
    )
    main_reviewers_count = max(
        1, int(os.environ.get("MAIN_REVIEWERS", str(DEFAULT_MAIN_REVIEWERS)))
    )
    inline_comments_enabled = parse_bool_env(os.environ.get("INLINE_COMMENTS", "true"), default=True)
    consensus_max_providers = max(
        1, int(os.environ.get("CONSENSUS_MAX_PROVIDERS", str(DEFAULT_CONSENSUS_MAX_PROVIDERS)))
    )

    openai_fallback = OpenAIFallbackConfig(
        base_url=os.environ.get("FALLBACK_OPENAI_BASE_URL", ""),
        api_key=os.environ.get("FALLBACK_OPENAI_API_KEY", ""),
        model=os.environ.get("FALLBACK_OPENAI_MODEL", ""),
    )
    providers = build_provider_chain(openai_fallback=openai_fallback)
    if not providers:
        print(
            "No AI review provider configured. Provide GEMINI_API_KEY, or configure "
            "GROQ_API_KEY, OPENROUTER_API_KEY, CEREBRAS_API_KEY, MISTRAL_API_KEY, "
            "or FALLBACK_OPENAI_*."
        )
        msg = (
            "> [!WARNING]\n"
            "> Automated AI review skipped: No AI review provider configured.\n"
            "> Please configure `GEMINI_API_KEY` (or `GROQ_API_KEY`, `OPENROUTER_API_KEY`, "
            "`CEREBRAS_API_KEY`, `MISTRAL_API_KEY`) in repository secrets."
        )
        write_review_outputs(msg, "comment")
        return

    gemini_provider = next((p for p in providers if p.kind == "gemini"), None)
    api_key = gemini_provider.api_key if gemini_provider else ""
    candidate_models = list(gemini_provider.models) if gemini_provider else list(providers[0].models)

    diff_path = os.environ.get("DIFF_FILE", "pr_diff.txt")
    diff_stat_path = os.environ.get("DIFF_STAT_FILE", "pr_diff_stat.txt")
    extra_exclude_patterns = parse_exclude_patterns(os.environ.get("EXCLUDE_PATHS", ""))
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    bypass_keyword = os.environ.get("BYPASS_KEYWORD", "BYPASS-LINK-ISSUE")

    if not os.path.exists(diff_path):
        print(f"Diff file not found at {diff_path}.")
        write_review_outputs(f"Diff file not found at `{diff_path}`.", "comment")
        return

    with open(diff_path, "r") as f:
        raw_diff_content = f.read()

    if not raw_diff_content.strip():
        print("No diff found. Exiting.")
        write_review_outputs("No code changes detected in this PR.", "comment")
        return

    diff_stat_text = ""
    if diff_stat_path and os.path.exists(diff_stat_path):
        with open(diff_stat_path, "r") as f:
            diff_stat_text = f.read()

    all_files = parse_diff_files(raw_diff_content)
    kept_files, excluded_files = filter_diff_files(all_files, extra_exclude_patterns)
    if excluded_files:
        print(
            f"Excluded {len(excluded_files)} generated/lock/vendor file(s) from full diff: "
            + ", ".join(path for path, _ in excluded_files)
        )

    excluded_only_fallback = False
    if not kept_files and excluded_files:
        print("All changed files matched exclusion rules; reviewing truncated excluded diff instead.")
        kept_files = excluded_files
        excluded_files = []
        excluded_only_fallback = True

    pr_size_callout = build_pr_size_callout(
        kept_files, excluded_files, warn_lines=pr_size_warn_lines, warn_files=pr_size_warn_files
    )
    diff_overview = build_diff_overview(diff_stat_text, excluded_files)
    if inline_comments_enabled:
        prompt_files, valid_diff_lines = annotate_diff_with_line_numbers(kept_files)
    else:
        prompt_files, valid_diff_lines = kept_files, {}

    filtered_diff = "\n".join(patch for _, patch in prompt_files)
    if excluded_only_fallback and len(filtered_diff) > single_shot_max_chars:
        filtered_diff = (
            filtered_diff[:single_shot_max_chars]
            + f"\n\n... [Diff truncated: excluded-only PR diff ({len(filtered_diff):,} chars) exceeds {single_shot_max_chars:,} chars limit] ..."
        )

    # Fetch PR metadata & linked issues
    pr_context = fetch_pr_context(repo, pr_number)
    bypass_active = has_bypass_keyword(pr_context, bypass_keyword)
    candidate_issue_nums = extract_linked_issue_numbers(pr_context)
    linked_issues, issue_perm_denied = fetch_issue_details(repo, candidate_issue_nums)

    has_verified_or_perm_blocked_issue = (len(linked_issues) > 0) or (
        len(candidate_issue_nums) > 0 and issue_perm_denied
    )
    missing_issue_strict = (not has_verified_or_perm_blocked_issue) and (not bypass_active)
    prior_missing_count = count_prior_missing_issue_reviews(pr_context.get("reviews", []))
    unlinked_review_count = prior_missing_count + 1 if missing_issue_strict else 0

    if linked_issues:
        print(f"Found linked original issue(s): {[i.get('number') for i in linked_issues]}")
    elif candidate_issue_nums and issue_perm_denied:
        print(
            f"Detected linked issue reference(s) {candidate_issue_nums}, but workflow GITHUB_TOKEN lacked "
            f"'issues: read' permission to fetch full issue text. Proceeding without missing-issue penalty."
        )
    elif bypass_active:
        print(f"No linked issue found, but '{bypass_keyword}' is present in PR description. Bypassing strict issue check.")
    else:
        print(
            f"Strict Mode: No linked issue found (unlinked review #{unlinked_review_count}). "
            f"Will request changes until issue is linked or bypassed."
        )

    current_date = datetime.now().strftime("%Y-%m-%d")
    issue_context_block, issue_rule = build_issue_context_and_rule(
        linked_issues,
        candidate_issue_nums,
        issue_perm_denied,
        bypass_active,
        bypass_keyword,
    )

    inline_rule = f"\n    {build_inline_comments_instruction()}" if inline_comments_enabled else ""
    system_instruction = f"""
    You are an expert, strict, and highly capable Code Reviewer.
    Your job is to review pull request code changes (diffs) against the original issue specification and provide actionable feedback.
    The current date is {current_date}.

    CRITICAL RULES YOU MUST ENFORCE:
    {issue_rule}
    2. Code Quality: Code must align with architectural standards, handle edge cases, and be clean and maintainable.
    3. Testing: Unit tests must be added or updated to cover new features, bug fixes, and edge cases.
    4. Security: Absolutely NO hardcoded secrets, API keys, passwords, or security vulnerabilities.{inline_rule}

    INSTRUCTIONS:
    - First, evaluate alignment with the Original Issue (if linked) before trusting the PR description.
    - Analyze the provided DIFF carefully against all critical rules above.
    - Point out any missing features, misinterpreted requirements, bugs, or rule violations clearly.
    - End your review with exactly one of these tokens on a new line: [APPROVED] or [CHANGES_REQUESTED]
    """

    pr_title = pr_context.get("title") or "(No PR title)"
    pr_body = (pr_context.get("body") or "").strip() or "(No PR description provided)"

    # HttpOptions.timeout in google-genai is in milliseconds.
    client = (
        genai.Client(
            api_key=api_key,
            http_options={"api_version": "v1beta", "timeout": request_timeout_sec * 1000},
        )
        if gemini_provider is not None
        else None
    )

    chunks: list[list[tuple[str, str]]] | None = None
    omitted_files: list[str] = []
    chunk_count = 0
    prompt = ""
    if len(filtered_diff) > single_shot_max_chars:
        chunks, omitted_files = chunk_diff_files(prompt_files, chunk_target_chars, max_chunks)
        chunk_count = len(chunks)
        print(
            f"Large PR diff detected ({len(filtered_diff):,} chars across {len(prompt_files)} file(s)). "
            f"Running file-aware chunked review across {chunk_count} chunk(s)..."
        )
    else:
        overview_section = f"\n{diff_overview}\n" if diff_overview else ""
        prompt = f"""
    # Original GitHub Issue Specification (Source of Truth)
    {issue_context_block}

    # Pull Request Metadata (Developer / Agent Claim)
    **PR Title:** {pr_title}
    **PR Description:**
    {pr_body}
    {overview_section}
    # Git Diff (Actual Implementation)
    Please review the following git diff against the Original Issue and critical rules:

    ```diff
    {filtered_diff}
    ```
    """

    if review_mode == "consensus" and len(providers) > 1:
        review_text, used_model, last_err = run_consensus_review(
            providers=providers,
            client=client,
            prompt=prompt,
            system_instruction=system_instruction,
            max_retries=max_retries,
            base_delay=base_delay,
            api_key=api_key,
            request_timeout_sec=request_timeout_sec,
            consensus_max_providers=consensus_max_providers,
            chunks=chunks,
            omitted_files=omitted_files,
            diff_overview=diff_overview,
            issue_context_block=issue_context_block,
            pr_title=pr_title,
            pr_body=pr_body,
            chunk_pause=chunk_pause,
        )
        if review_text is not None:
            _emit_reviewer_outputs(
                [
                    ProviderReviewResult(
                        provider_id="consensus",
                        display_name="Multi-Provider Consensus",
                        used_model=used_model,
                        review_text=review_text,
                        slot_label="Consensus Review",
                    )
                ],
                inline_comments_enabled=inline_comments_enabled,
                valid_diff_lines=valid_diff_lines,
                missing_issue_strict=missing_issue_strict,
                unlinked_review_count=unlinked_review_count,
                bypass_keyword=bypass_keyword,
                pr_size_callout=pr_size_callout,
                chunk_count=chunk_count,
            )
            return
    else:
        target_main_count = len(providers) if review_mode == "parallel" else main_reviewers_count
        results = run_multi_reviewer_pipeline(
            providers=providers,
            client=client,
            prompt=prompt,
            system_instruction=system_instruction,
            max_retries=max_retries,
            base_delay=base_delay,
            api_key=api_key,
            request_timeout_sec=request_timeout_sec,
            main_reviewers_count=target_main_count,
            extra_providers_mode=extra_providers_mode,
            chunks=chunks,
            omitted_files=omitted_files,
            diff_overview=diff_overview,
            issue_context_block=issue_context_block,
            pr_title=pr_title,
            pr_body=pr_body,
            chunk_pause=chunk_pause,
        )
        succeeded = [r for r in results if r.succeeded]
        if succeeded:
            _emit_reviewer_outputs(
                succeeded,
                inline_comments_enabled=inline_comments_enabled,
                valid_diff_lines=valid_diff_lines,
                missing_issue_strict=missing_issue_strict,
                unlinked_review_count=unlinked_review_count,
                bypass_keyword=bypass_keyword,
                pr_size_callout=pr_size_callout,
                chunk_count=chunk_count,
            )
            return
        used_model = results[-1].used_model
        last_err = results[-1].error

    if is_fatal_auth_error(last_err) and len(providers) == 1:
        cred_hint = "GEMINI_API_KEY" if providers[0].kind == "gemini" else f"{providers[0].display_name} credentials"
        review_text = (
            f"> [!WARNING]\n"
            f"> Automated AI review failed due to authentication or permissions ({last_err}). "
            f"Please verify `{cred_hint}` and proceed with manual review."
        )
    else:
        tested = [m for p in providers for m in p.models]
        err_detail = f" Last error: `{last_err}`." if last_err else ""
        review_text = (
            f"> [!WARNING]\n"
            f"> Automated AI review temporarily unavailable after testing "
            f"models ({', '.join(tested)}).{err_detail} Please proceed with manual review."
        )

    output_body, review_action = finalize_review_body(
        review_text=review_text,
        used_model=used_model,
        api_succeeded=False,
        missing_issue_strict=missing_issue_strict,
        unlinked_review_count=unlinked_review_count,
        bypass_keyword=bypass_keyword,
        pr_size_callout=pr_size_callout,
        chunk_count=chunk_count,
    )
    write_review_outputs(output_body, review_action)


if __name__ == "__main__":
    main()
