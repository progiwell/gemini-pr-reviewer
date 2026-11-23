from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _install_genai_stub() -> None:
    """Installs lightweight stubs for `google.genai` when the SDK is absent locally."""
    try:
        import google.genai  # noqa: F401
        return
    except ImportError:
        pass

    google_mod = sys.modules.setdefault("google", types.ModuleType("google"))
    genai_mod = types.ModuleType("google.genai")
    types_mod = types.ModuleType("google.genai.types")

    class _DummyClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.models = mock.MagicMock()

    class _DummyObj:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    genai_mod.Client = _DummyClient
    genai_mod.types = types_mod
    types_mod.GenerateContentConfig = _DummyObj
    types_mod.SafetySetting = _DummyObj

    google_mod.genai = genai_mod
    sys.modules["google.genai"] = genai_mod
    sys.modules["google.genai.types"] = types_mod


_install_genai_stub()

import gemini_review  # noqa: E402


def _make_file_patch(path: str, body_size: int = 120) -> str:
    payload = "+" + ("x" * max(1, body_size))
    return (
        f"diff --git a/{path} b/{path}\n"
        f"new file mode 100644\n"
        f"--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,1 @@\n"
        f"{payload}"
    )


class TestCandidateModels(unittest.TestCase):
    def test_single_and_comma_separated_fallbacks(self):
        self.assertEqual(
            gemini_review.build_candidate_models("gemini-3.8-flash", "gemini-2.5-flash"),
            ["gemini-3.8-flash", "gemini-2.5-flash"],
        )
        self.assertEqual(
            gemini_review.build_candidate_models(
                "gemini-3.8-flash",
                " gemini-3.6-flash , gemini-3.5-flash-lite , gemini-2.5-flash ",
            ),
            [
                "gemini-3.8-flash",
                "gemini-3.6-flash",
                "gemini-3.5-flash-lite",
                "gemini-2.5-flash",
            ],
        )

    def test_deduplication_and_empty_items(self):
        self.assertEqual(
            gemini_review.build_candidate_models(
                "gemini-3.8-flash",
                "gemini-3.8-flash, , gemini-3.6-flash, gemini-3.6-flash",
            ),
            ["gemini-3.8-flash", "gemini-3.6-flash"],
        )


class TestErrorClassificationAndSanitization(unittest.TestCase):
    def test_transient_errors(self):
        for err in [
            "503 UNAVAILABLE: model overloaded",
            "429 RESOURCE_EXHAUSTED: rate limit reached",
            "Server disconnected without sending a response.",
            "httpx.ReadTimeout: timed out waiting for response",
            "Connection reset by peer (RemoteProtocolError)",
        ]:
            with self.subTest(err=err):
                self.assertTrue(gemini_review.is_transient_error(err))
                self.assertTrue(gemini_review.is_transient_busy_error(err))

    def test_model_unavailable_and_fatal_auth(self):
        self.assertTrue(gemini_review.is_model_unavailable_error("404 NOT_FOUND: model deprecated"))
        self.assertFalse(gemini_review.is_fatal_auth_error("404 NOT_FOUND: model deprecated"))

        self.assertTrue(gemini_review.is_fatal_auth_error("401 API_KEY_INVALID: API key not valid"))
        self.assertTrue(gemini_review.is_fatal_auth_error("403 PERMISSION_DENIED"))
        self.assertFalse(gemini_review.is_transient_error("401 API_KEY_INVALID"))

    def test_sanitize_error_scrubs_api_key_and_truncates(self):
        secret = "AIzaSySecretTestKey12345"
        msg = f"HTTP 401   error\nwith key={secret}   " + ("z" * 300)
        sanitized = gemini_review.sanitize_error(msg, secret)
        self.assertNotIn(secret, sanitized)
        self.assertIn("***", sanitized)
        self.assertLessEqual(len(sanitized), 240)
        self.assertTrue(sanitized.endswith("..."))


class TestDiffParsingFilteringAndChunking(unittest.TestCase):
    def test_parse_and_filter_excludes_generated_and_custom_paths(self):
        raw_diff = "\n".join(
            [
                _make_file_patch("src/dashboard.ts", 200),
                _make_file_patch("static/dist/dashboard.js", 5000),
                _make_file_patch("package-lock.json", 4000),
                _make_file_patch("assets/vendor.min.js", 1500),
                _make_file_patch("generated/schema.pb.ts", 900),
                _make_file_patch("tests/dashboard.test.ts", 300),
            ]
        )
        parsed = gemini_review.parse_diff_files(raw_diff)
        self.assertEqual(len(parsed), 6)

        extra = gemini_review.parse_exclude_patterns("*.pb.ts, docs/generated/*")
        kept, excluded = gemini_review.filter_diff_files(parsed, extra)

        self.assertEqual([p for p, _ in kept], ["src/dashboard.ts", "tests/dashboard.test.ts"])
        self.assertEqual(
            [p for p, _ in excluded],
            [
                "static/dist/dashboard.js",
                "package-lock.json",
                "assets/vendor.min.js",
                "generated/schema.pb.ts",
            ],
        )

    def test_chunk_diff_files_respects_boundaries_truncation_and_max_chunks(self):
        files = [
            ("a.py", _make_file_patch("a.py", 350)),
            ("b.py", _make_file_patch("b.py", 350)),
            ("c.py", _make_file_patch("c.py", 550)),
            ("huge.py", _make_file_patch("huge.py", 1800)),
            ("d.py", _make_file_patch("d.py", 400)),
        ]
        chunks, omitted = gemini_review.chunk_diff_files(files, chunk_target_chars=1000, max_chunks=3)

        self.assertEqual(len(chunks), 3)
        self.assertEqual([p for p, _ in chunks[0]], ["a.py", "b.py"])
        self.assertEqual([p for p, _ in chunks[1]], ["c.py"])
        self.assertEqual([p for p, _ in chunks[2]], ["huge.py"])
        self.assertLessEqual(len(chunks[2][0][1]), 1000)
        self.assertIn("File diff truncated", chunks[2][0][1])
        self.assertEqual(omitted, ["d.py"])


class TestMainEndToEnd(unittest.TestCase):
    def _run_main_in_tempdir(
        self,
        diff_text: str,
        env_overrides: dict[str, str],
        side_effect_fn,
        pr_body: str = "Fixes #1",
        linked_issues: list[dict] | None = None,
    ) -> tuple[str, str, list[dict], list[tuple[str, str]]]:
        recorded_client_kwargs: list[dict] = []
        recorded_calls: list[tuple[str, str]] = []

        class FakeClient:
            def __init__(self, **kwargs):
                recorded_client_kwargs.append(kwargs)
                self.models = self

            def generate_content(self, model: str, contents: str, config=None):
                recorded_calls.append((model, contents))
                result = side_effect_fn(model, contents)
                if isinstance(result, Exception):
                    raise result
                resp = mock.MagicMock()
                resp.text = result
                return resp

        if linked_issues is None:
            linked_issues = [
                {
                    "number": 1,
                    "title": "Sample issue",
                    "body": "- [ ] Implement feature",
                    "state": "OPEN",
                    "labels": [],
                    "comments": [],
                }
            ]

        pr_ctx = {
            "title": "feat: sample PR",
            "body": pr_body,
            "headRefName": "feat/issue-1-sample",
            "closingIssuesReferences": [],
            "reviews": [],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                Path("pr_diff.txt").write_text(diff_text, encoding="utf-8")
                Path("pr_diff_stat.txt").write_text(" sample.py | 2 +\n", encoding="utf-8")

                base_env = {
                    "GEMINI_API_KEY": "AIzaSyDummyTestKey999",
                    "GEMINI_MODEL": "gemini-3.8-flash",
                    "GEMINI_FALLBACK_MODEL": "gemini-3.6-flash,gemini-3.5-flash-lite",
                    "GEMINI_MAX_RETRIES": "2",
                    "GEMINI_RETRY_DELAY": "1",
                    "GEMINI_REQUEST_TIMEOUT": "90",
                    "GEMINI_CHUNK_PAUSE": "0",
                    "DIFF_FILE": "pr_diff.txt",
                    "DIFF_STAT_FILE": "pr_diff_stat.txt",
                    "GITHUB_REPOSITORY": "progiwell/sample",
                    "PR_NUMBER": "42",
                }
                base_env.update(env_overrides)

                with (
                    mock.patch.dict(os.environ, base_env, clear=False),
                    mock.patch.object(gemini_review.genai, "Client", FakeClient),
                    mock.patch.object(gemini_review, "fetch_pr_context", return_value=pr_ctx),
                    mock.patch.object(
                        gemini_review, "fetch_issue_details", return_value=(linked_issues, False)
                    ),
                    mock.patch.object(gemini_review.time, "sleep") as sleep_mock,
                ):
                    gemini_review.main()
                    self._last_sleep_mock = sleep_mock

                output_md = Path("review_output.md").read_text(encoding="utf-8")
                action_txt = Path("review_action.txt").read_text(encoding="utf-8")
                return output_md, action_txt, recorded_client_kwargs, recorded_calls
            finally:
                os.chdir(old_cwd)

    def test_single_shot_failover_and_timeout_ms(self):
        def responder(model: str, _contents: str):
            if model == "gemini-3.8-flash":
                raise RuntimeError("Server disconnected without sending a response.")
            if model == "gemini-3.6-flash":
                raise RuntimeError("404 NOT_FOUND: model not supported")
            return "#### 📋 Issue Requirements Checklist\n- ✅ Implemented\n\n[APPROVED]"

        diff_text = _make_file_patch("app.py", 200)
        output_md, action_txt, client_kwargs, calls = self._run_main_in_tempdir(
            diff_text, {}, responder
        )

        self.assertEqual(client_kwargs[0]["http_options"], {"api_version": "v1beta", "timeout": 90000})
        # 2 retries on gemini-3.8-flash, 1 immediate fail-over on 404 gemini-3.6-flash, 1 success on 3.5-flash-lite
        self.assertEqual(
            [m for m, _ in calls],
            ["gemini-3.8-flash", "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"],
        )
        self.assertEqual(action_txt, "approve")
        self.assertIn("### AI Code Review (gemini-3.5-flash-lite via Google AI Studio)", output_md)

    def test_fatal_auth_error_aborts_immediately_and_scrubs_key(self):
        def responder(_model: str, _contents: str):
            raise RuntimeError("401 API_KEY_INVALID for AIzaSyDummyTestKey999")

        output_md, action_txt, _, calls = self._run_main_in_tempdir(
            _make_file_patch("app.py", 150), {}, responder
        )
        self.assertEqual([m for m, _ in calls], ["gemini-3.8-flash"])
        self.assertEqual(action_txt, "comment")
        self.assertNotIn("AIzaSyDummyTestKey999", output_md)
        self.assertIn("authentication or permissions", output_md)

    def test_large_pr_triggers_map_reduce_chunking(self):
        # 3 source files + 1 excluded bundle; threshold lowered via env for test speed
        diff_text = "\n".join(
            [
                _make_file_patch("src/a.ts", 450),
                _make_file_patch("src/b.ts", 450),
                _make_file_patch("src/c.ts", 450),
                _make_file_patch("static/dist/bundle.js", 3000),
            ]
        )

        prompts_seen: list[str] = []

        def responder(_model: str, contents: str):
            prompts_seen.append(contents)
            if "# Chunk Review Findings" not in contents:
                return "- Verified chunk changes cleanly."
            return "#### 📋 Issue Requirements Checklist\n- ✅ Implemented\n\n[APPROVED]"

        output_md, action_txt, _, calls = self._run_main_in_tempdir(
            diff_text,
            {
                "GEMINI_SINGLE_SHOT_MAX_CHARS": "900",
                "GEMINI_CHUNK_TARGET_CHARS": "700",
                "GEMINI_MAX_CHUNKS": "5",
            },
            responder,
        )

        # 3 map chunks + 1 reduce synthesis = 4 calls, and static/dist/bundle.js excluded from map diffs
        self.assertEqual(len(calls), 4)
        self.assertEqual(action_txt, "approve")
        self.assertIn("· 3-chunk review", output_md)
        for map_prompt in prompts_seen[:3]:
            self.assertNotIn("static/dist/bundle.js", map_prompt)
        self.assertIn("static/dist/bundle.js", prompts_seen[3])

    def test_lockfile_only_pr_is_still_reviewed(self):
        diff_text = _make_file_patch("package-lock.json", 300)
        output_md, action_txt, _, calls = self._run_main_in_tempdir(
            diff_text,
            {},
            lambda _m, _c: "Lockfile changes look fine.\n\n[APPROVED]",
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(action_txt, "approve")
        self.assertIn("Lockfile changes look fine", output_md)

    def test_missing_linked_issue_strict_mode_forces_request_changes(self):
        output_md, action_txt, _, _ = self._run_main_in_tempdir(
            _make_file_patch("app.py", 150),
            {},
            lambda _m, _c: "Looks good.\n\n[APPROVED]",
            pr_body="No issue here",
            linked_issues=[],
        )
        self.assertEqual(action_txt, "request-changes")
        self.assertIn("No Linked GitHub Issue Found", output_md)
        self.assertIn("[CHANGES_REQUESTED]", output_md)


if __name__ == "__main__":
    unittest.main()
