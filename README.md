# 🤖 Gemini PR Reviewer

A robust, enterprise-grade GitHub Action for automated AI code reviews on Pull Requests powered by **Google Gemini via Google AI Studio**.

Includes **Strict Original Issue Verification**, a **3-tier multi-model fallback chain** (`gemini-3.8-flash` $\to$ `gemini-3.6-flash` $\to$ `gemini-3.5-flash-lite`), **bounded HTTP timeouts & socket-drop retries**, **smart diff exclusions**, and **file-aware Map-Reduce chunked reviews** for large PRs.

---

## 🚀 Quick Start

Add `.github/workflows/gemini_pr_reviewer.yml` to your repository. Omitting `primary-model` and `fallback-model` automatically inherits the recommended defaults from `progiwell/gemini-pr-reviewer@v1`:

```yaml
name: Gemini PR Reviewer

on:
  pull_request:
    types: [opened, synchronize, reopened, edited]

concurrency:
  group: gemini-pr-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  review:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: read
      issues: read
      pull-requests: write

    steps:
      - name: Checkout Repository
        uses: actions/checkout@v7
        with:
          fetch-depth: 0

      - name: Gemini AI Review
        uses: progiwell/gemini-pr-reviewer@v1
        with:
          gemini-api-key: ${{ secrets.GEMINI_API_KEY }}
```

### Customizing the Fallback Chain & Cross-Provider Backup (Optional)

You can pass a comma-separated list to `fallback-model` (for example, adding `gemini-2.5-flash`), exclude additional generated paths, or configure an OpenAI-compatible cross-provider backup (such as **Groq** or **OpenRouter `:free`**):

```yaml
      - name: Gemini AI Review
        uses: progiwell/gemini-pr-reviewer@v1
        with:
          gemini-api-key: ${{ secrets.GEMINI_API_KEY }}
          primary-model: 'gemini-3.8-flash'
          fallback-model: 'gemini-3.6-flash,gemini-3.5-flash-lite,gemini-2.5-flash'
          request-timeout: '60'
          exclude-paths: '*.pb.ts,docs/generated/*'
          fallback-openai-base-url: 'https://api.groq.com/openai/v1'
          fallback-openai-api-key: ${{ secrets.GROQ_API_KEY }}
          fallback-openai-model: 'openai/gpt-oss-120b'
```

---

## 🔗 Acceptable Issue Reference Formats & Bypass Keyword

The reviewer automatically detects linked issues from any of the following formats in the **PR description**, **PR title**, or **branch name**:

| Reference Type | Accepted Formats / Examples |
|---|---|
| **Closing Keywords** | `Fixes #70`, `Closes #70`, `Resolves #70` (plus native GitHub `closingIssuesReferences`) |
| **Implementation / Issue Keywords** | `Implements Issue #70`, `Implements #70`, `Issue #70`, `Issue 70`, `Refs #70`, `References #70` (case-insensitive, `#` optional) |
| **Standalone Hash Reference** | `#70` anywhere in the PR title or description (e.g., `feat: executive briefings (#70)`) |
| **Full GitHub Issue URL** | `https://github.com/<owner>/<repo>/issues/70` |
| **Branch Name Convention** | `feat/issue-70-executive-briefings`, `issue-70`, `issue_70`, `fix/70-bug`, `feat/#70` |
| **Bypass Keyword (No Issue)** | Add `BYPASS-LINK-ISSUE` anywhere in the PR description or title to bypass strict issue linking for minor chores/hotfixes |

---

## ⚙️ Inputs Reference

| Input | Description | Default | Required |
|---|---|---|---|
| `gemini-api-key` | Google AI Studio Gemini API Key | N/A | **Yes** |
| `github-token` | GitHub Token for fetching issues & posting reviews | `${{ github.token }}` | No |
| `primary-model` | Primary model to query | `gemini-3.8-flash` | No |
| `fallback-model` | Fallback model(s) to try in order when primary is congested (comma-separated) | `gemini-3.6-flash,gemini-3.5-flash-lite` | No |
| `max-retries` | Max retry attempts per model before failing over (capped at 2 when a fallback chain is configured) | `1` | No |
| `retry-delay` | Base backoff delay in seconds | `10` | No |
| `request-timeout` | HTTP timeout in seconds per API request | `60` | No |
| `exclude-paths` | Optional comma-separated extra glob patterns to exclude from diff review | `''` | No |
| `fallback-openai-base-url` | Optional OpenAI-compatible API base URL for cross-provider fallback | `''` | No |
| `fallback-openai-api-key` | Optional API key for OpenAI-compatible fallback | `''` | No |
| `fallback-openai-model` | Optional model name for OpenAI-compatible fallback | `''` | No |
| `pr-size-warn-lines` | Changed source line threshold for PR size warning callout | `800` | No |
| `pr-size-warn-files` | Changed source file count threshold for PR size warning callout | `12` | No |
| `python-version` | Python runtime version | `3.11` | No |
| `bypass-keyword` | Keyword in PR description to bypass strict issue requirement | `BYPASS-LINK-ISSUE` | No |

---

## 🛡️ Key Features

1. **Strict Original Issue Verification (Source of Truth)**:
   - Automatically detects linked GitHub Issues from the PR description, title, or branch name and fetches the **original GitHub Issue title, description, labels, and comments**.
   - Audits the code diff directly against the **original issue specification**—not just the PR description—catching cases where a developer or coding agent misinterpreted the issue.
   - Generates a structured **📋 Issue Requirements Checklist** verifying every requirement and acceptance criterion (`✅ Implemented`, `⚠️ Partial`, `❌ Missing / Misinterpreted`).
   - **Strict Linking Policy**: If no issue is linked, the reviewer requests changes (`[CHANGES_REQUESTED]`) and reminds the developer to link the issue. If the issue is still not linked after 2 reviews, the reviewer shares the `BYPASS-LINK-ISSUE` keyword for legitimate issue-less PRs (e.g., quick chores or hotfixes).
2. **3-Tier Multi-Model & Cross-Provider Fallback**:
   Supports a comma-separated Gemini fallback list (`gemini-3.8-flash` $\to$ `gemini-3.6-flash` $\to$ `gemini-3.5-flash-lite`) plus an optional stdlib OpenAI-compatible last-resort backup (`fallback-openai-*` for Groq, OpenRouter, etc.). Unknown/deprecated (`404`) models fail over immediately; fatal auth errors (`401`/`403`) abort immediately with dual-key-scrubbed logs.
3. **Client Timeout & Socket-Drop Resilience**:
   Enforces an explicit HTTP request timeout (`request-timeout`, default `60s`), treats socket disconnects (`Server disconnected`), connection resets, and timeouts as transient retryable errors, and logs exact sanitized API errors (`503` vs `429` vs timeout) instead of hanging indefinitely.
4. **Smart Diff Filtering, PR Size Callouts & File-Aware Chunked Review**:
   Automatically excludes noisy lockfiles, minified bundles, source maps, snapshots, and `dist/`/`build/` artifacts from the full diff while preserving a `git diff --stat` overview. Prepends an automatic **PR Size** badge (`S`/`M`/`L`/`XL`) with alerts when diffs exceed `pr-size-warn-lines`/`pr-size-warn-files` or add/modify compiled bundles, and splits PRs above 80,000 filtered chars along `diff --git` file boundaries via Map-Reduce synthesis.
5. **Smart Review Actions**:
   Inspects the model's recommendation and issues native GitHub review outcomes:
   - `[APPROVED]` $\to$ `--approve`
   - `[CHANGES_REQUESTED]` $\to$ `--request-changes`
   - Warnings/Errors $\to$ neutral `--comment`
6. **Zero Repository Boilerplate**:
   No python dependencies or diff parsing scripts needed in individual repos.

---

## 🔑 Setup API Key

1. Generate a free API key at [Google AI Studio](https://aistudio.google.com/).
2. Add it to your repository secrets:
   - Go to **Settings** > **Secrets and variables** > **Actions**.
   - Create secret `GEMINI_API_KEY`.
