from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import types
import unittest
import urllib.error
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


_CLEAN_PROVIDER_ENV = {
    "ENABLE_GITHUB_MODELS": "false",
    "GH_TOKEN": "",
    "GITHUB_TOKEN": "",
    "GITHUB_MODELS_TOKEN": "",
    "GROQ_API_KEY": "",
    "OPENROUTER_API_KEY": "",
    "CEREBRAS_API_KEY": "",
    "MISTRAL_API_KEY": "",
    "FALLBACK_OPENAI_BASE_URL": "",
    "FALLBACK_OPENAI_API_KEY": "",
    "FALLBACK_OPENAI_MODEL": "",
    "PROVIDER_ORDER": gemini_review.DEFAULT_PROVIDER_ORDER,
    "REVIEW_MODE": "fallback",
    "EXTRA_PROVIDERS_MODE": "fallback",
    "MAIN_REVIEWERS": "2",
    "INLINE_COMMENTS": "true",
    "CONSENSUS_MAX_PROVIDERS": "3",
}


class _MainRunnerMixin:
    def _run_main_in_tempdir(
        self,
        diff_text: str,
        env_overrides: dict[str, str],
        side_effect_fn,
        pr_body: str = "Fixes #1",
        linked_issues: list[dict] | None = None,
        extra_artifacts_out: dict[str, str] | None = None,
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
                    **_CLEAN_PROVIDER_ENV,
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
                if extra_artifacts_out is not None:
                    for p in sorted(Path(".").glob("review_output_extra_*.md")):
                        extra_artifacts_out[p.name] = p.read_text(encoding="utf-8")
                    for p in sorted(Path(".").glob("review_payload_*.json")):
                        extra_artifacts_out[p.name] = p.read_text(encoding="utf-8")
                    if Path("review_outputs.json").exists():
                        extra_artifacts_out["review_outputs.json"] = Path("review_outputs.json").read_text(
                            encoding="utf-8"
                        )
                return output_md, action_txt, recorded_client_kwargs, recorded_calls
            finally:
                os.chdir(old_cwd)


class TestMainEndToEnd(_MainRunnerMixin, unittest.TestCase):
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

    def test_fast_failover_with_max_retries_one_has_zero_sleeps(self):
        def responder(model: str, _contents: str):
            if model in ("gemini-3.8-flash", "gemini-3.6-flash"):
                raise RuntimeError("503 UNAVAILABLE")
            return "#### 📋 Issue Requirements Checklist\n- ✅ Implemented\n\n[APPROVED]"

        output_md, action_txt, _, calls = self._run_main_in_tempdir(
            _make_file_patch("app.py", 150),
            {"GEMINI_MAX_RETRIES": "1"},
            responder,
        )
        self.assertEqual(
            [m for m, _ in calls],
            ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"],
        )
        self._last_sleep_mock.assert_not_called()
        self.assertEqual(action_txt, "approve")
        self.assertIn("> **PR Size:** `S`", output_md)

    def test_openai_compatible_fallback_succeeds_when_gemini_fails(self):
        def responder(_model: str, _contents: str):
            raise RuntimeError("503 UNAVAILABLE")

        fake_body = json.dumps(
            {
                "choices": [
                    {"message": {"content": "#### 📋 Issue Requirements Checklist\n- ✅ Implemented\n\n[APPROVED]"}}
                ]
            }
        ).encode("utf-8")

        fake_resp = mock.MagicMock()
        fake_resp.read.return_value = fake_body
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = False

        with mock.patch.object(gemini_review.urllib.request, "urlopen", return_value=fake_resp) as urlopen_mock:
            output_md, action_txt, _, calls = self._run_main_in_tempdir(
                _make_file_patch("app.py", 150),
                {
                    "GEMINI_MAX_RETRIES": "1",
                    "FALLBACK_OPENAI_BASE_URL": "https://api.groq.com/openai/v1",
                    "FALLBACK_OPENAI_API_KEY": "gsk_secret_fallback_token",
                    "FALLBACK_OPENAI_MODEL": "openai/gpt-oss-120b",
                },
                responder,
            )

        self.assertEqual(len(calls), 3)
        urlopen_mock.assert_called_once()
        req = urlopen_mock.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(action_txt, "approve")
        self.assertIn("### AI Code Review (openai/gpt-oss-120b (OpenAI-compatible fallback))", output_md)
        self.assertNotIn("via Google AI Studio", output_md)

    def test_openai_compatible_fallback_http_error_scrubs_both_keys(self):
        def responder(_model: str, _contents: str):
            raise RuntimeError("503 UNAVAILABLE")

        http_err = urllib.error.HTTPError(
            url="https://api.groq.com/openai/v1/chat/completions",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b"Invalid key gsk_secret_fallback_token and AIzaSyDummyTestKey999"),
        )

        with mock.patch.object(gemini_review.urllib.request, "urlopen", side_effect=http_err):
            output_md, action_txt, _, _ = self._run_main_in_tempdir(
                _make_file_patch("app.py", 150),
                {
                    "GEMINI_MAX_RETRIES": "1",
                    "FALLBACK_OPENAI_BASE_URL": "https://api.groq.com/openai/v1",
                    "FALLBACK_OPENAI_API_KEY": "gsk_secret_fallback_token",
                    "FALLBACK_OPENAI_MODEL": "openai/gpt-oss-120b",
                },
                responder,
            )

        self.assertEqual(action_txt, "comment")
        self.assertNotIn("gsk_secret_fallback_token", output_md)
        self.assertNotIn("AIzaSyDummyTestKey999", output_md)
        self.assertIn("***", output_md)


class TestPRSizeCallout(unittest.TestCase):
    @staticmethod
    def _multiline_patch(path: str, added_lines: int, deleted_mode: bool = False) -> str:
        mode_line = "deleted file mode 100644\n" if deleted_mode else "new file mode 100644\n"
        sign = "-" if deleted_mode else "+"
        body = "\n".join(f"{sign}line {i}" for i in range(added_lines))
        return (
            f"diff --git a/{path} b/{path}\n"
            f"{mode_line}"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            f"@@ -1,{added_lines} +1,{added_lines} @@\n"
            f"{body}"
        )

    def test_compute_stats_and_classification(self):
        kept = [
            ("src/a.py", self._multiline_patch("src/a.py", 120)),
            ("src/b.py", self._multiline_patch("src/b.py", 100)),
        ]
        adds, dels = gemini_review.compute_diff_line_stats(kept)
        self.assertEqual((adds, dels), (220, 0))
        self.assertEqual(gemini_review.classify_pr_size(50, 2), "S")
        self.assertEqual(gemini_review.classify_pr_size(220, 2), "M")
        self.assertEqual(gemini_review.classify_pr_size(650, 3), "L")
        self.assertEqual(gemini_review.classify_pr_size(850, 3), "XL")
        self.assertEqual(gemini_review.classify_pr_size(100, 15), "XL")

    def test_callout_warns_on_xl_and_added_compiled_artifacts_only(self):
        xl_kept = [("src/big.py", self._multiline_patch("src/big.py", 850))]
        added_dist = [("static/dist/dashboard.js", self._multiline_patch("static/dist/dashboard.js", 50))]
        xl_callout = gemini_review.build_pr_size_callout(xl_kept, added_dist, warn_lines=800, warn_files=12)
        self.assertIn("> [!NOTE]", xl_callout)
        self.assertIn("`XL`", xl_callout)
        self.assertIn("static/dist/dashboard.js", xl_callout)

        small_kept = [("Dockerfile", self._multiline_patch("Dockerfile", 10))]
        deleted_dist = [
            ("static/dist/dashboard.js", self._multiline_patch("static/dist/dashboard.js", 50, deleted_mode=True))
        ]
        delete_callout = gemini_review.build_pr_size_callout(small_kept, deleted_dist, warn_lines=800, warn_files=12)
        self.assertNotIn("> [!NOTE]", delete_callout)
        self.assertNotIn("Compiled/bundled artifact", delete_callout)
        self.assertIn("> **PR Size:** `S`", delete_callout)


def _make_urlopen_response(content: str):
    fake_body = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
    fake_resp = mock.MagicMock()
    fake_resp.read.return_value = fake_body
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False
    return fake_resp


def _make_http_error(url: str, code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url=url,
        code=code,
        msg="Error",
        hdrs=None,
        fp=io.BytesIO(body),
    )


class TestProviderRegistryAndMultiProviderFallback(_MainRunnerMixin, unittest.TestCase):
    def test_build_provider_chain_defaults_and_custom_order(self):
        env = {
            "GEMINI_API_KEY": "AIza-test",
            "ENABLE_GITHUB_MODELS": "true",
            "GH_TOKEN": "ghs-test",
            "GROQ_API_KEY": "gsk-test",
            "OPENROUTER_API_KEY": "sk-or-test",
            "CEREBRAS_API_KEY": "csk-test",
            "MISTRAL_API_KEY": "msk-test",
            "FALLBACK_OPENAI_BASE_URL": "https://custom.example/v1",
            "FALLBACK_OPENAI_API_KEY": "cust-test",
            "FALLBACK_OPENAI_MODEL": "custom-model-1, custom-model-2",
        }
        chain = gemini_review.build_provider_chain(env)
        self.assertEqual(
            [p.provider_id for p in chain],
            ["gemini", "github-models", "groq", "openrouter", "cerebras", "mistral", "custom-openai"],
        )
        self.assertEqual(chain[1].base_url, "https://models.github.ai/inference")
        self.assertEqual(chain[6].models, ("custom-model-1", "custom-model-2"))

        reordered = gemini_review.build_provider_chain(
            {**env, "PROVIDER_ORDER": "groq, github-models, openrouter"}
        )
        self.assertEqual(
            [p.provider_id for p in reordered],
            ["groq", "github-models", "openrouter", "gemini", "cerebras", "mistral", "custom-openai"],
        )

    def test_multi_provider_failover_gemini_to_github_models_to_groq(self):
        def gemini_responder(_model: str, _contents: str):
            raise RuntimeError("503 UNAVAILABLE")

        seen_urls: list[str] = []

        def fake_urlopen(req, timeout=60):
            seen_urls.append(req.full_url)
            if "models.github.ai" in req.full_url:
                raise _make_http_error(req.full_url, 429, b"Rate limit exceeded")
            return _make_urlopen_response("#### 📋 Issue Requirements Checklist\n- ✅ Implemented\n\n[APPROVED]")

        env = {
            "GEMINI_MAX_RETRIES": "1",
            "ENABLE_GITHUB_MODELS": "true",
            "GH_TOKEN": "ghs_test_token",
            "GROQ_API_KEY": "gsk_test_token",
        }
        with mock.patch.object(gemini_review.urllib.request, "urlopen", side_effect=fake_urlopen):
            output_md, action_txt, _, calls = self._run_main_in_tempdir(
                _make_file_patch("app.py", 150), env, gemini_responder
            )

        self.assertEqual(len(calls), 3)
        self.assertEqual(
            seen_urls,
            [
                "https://models.github.ai/inference/chat/completions",
                "https://api.groq.com/openai/v1/chat/completions",
            ],
        )
        self.assertEqual(action_txt, "approve")
        self.assertIn("### AI Code Review (openai/gpt-oss-120b via Groq)", output_md)

    def test_openrouter_multi_model_failover_within_provider(self):
        seen_models: list[str] = []

        def fake_urlopen(req, timeout=60):
            payload = json.loads(req.data.decode("utf-8"))
            seen_models.append(payload["model"])
            if payload["model"] == "qwen/qwen3-coder:free":
                raise _make_http_error(req.full_url, 429, b"Provider returned 429")
            return _make_urlopen_response("#### 📋 Issue Requirements Checklist\n- ✅ Implemented\n\n[APPROVED]")

        env = {
            "GEMINI_API_KEY": "",
            "GEMINI_MAX_RETRIES": "1",
            "OPENROUTER_API_KEY": "sk-or-test-123",
            "OPENROUTER_MODEL": "qwen/qwen3-coder:free,deepseek/deepseek-r1:free",
        }
        with mock.patch.object(gemini_review.urllib.request, "urlopen", side_effect=fake_urlopen):
            output_md, action_txt, client_kwargs, _ = self._run_main_in_tempdir(
                _make_file_patch("app.py", 150), env, lambda _m, _c: ""
            )

        self.assertEqual(client_kwargs, [])
        self.assertEqual(seen_models, ["qwen/qwen3-coder:free", "deepseek/deepseek-r1:free"])
        self.assertEqual(action_txt, "approve")
        self.assertIn("### AI Code Review (deepseek/deepseek-r1:free via OpenRouter)", output_md)

    def test_keyless_gemini_with_github_models_only(self):
        seen_auth: list[str] = []

        def fake_urlopen(req, timeout=60):
            seen_auth.append(req.get_header("Authorization"))
            return _make_urlopen_response("#### 📋 Issue Requirements Checklist\n- ✅ Implemented\n\n[APPROVED]")

        env = {
            "GEMINI_API_KEY": "",
            "ENABLE_GITHUB_MODELS": "true",
            "GH_TOKEN": "ghs_test_token",
        }
        with mock.patch.object(gemini_review.urllib.request, "urlopen", side_effect=fake_urlopen):
            output_md, action_txt, client_kwargs, calls = self._run_main_in_tempdir(
                _make_file_patch("app.py", 150), env, lambda _m, _c: ""
            )

        self.assertEqual(client_kwargs, [])
        self.assertEqual(calls, [])
        self.assertEqual(seen_auth, ["Bearer ghs_test_token"])
        self.assertEqual(action_txt, "approve")
        self.assertIn("### AI Code Review (openai/gpt-4.1-mini via GitHub Models)", output_md)

    def test_multi_provider_error_scrubs_all_provider_keys(self):
        def gemini_responder(_model: str, _contents: str):
            raise RuntimeError("503 UNAVAILABLE for AIzaSyDummyTestKey999")

        def fake_urlopen(req, timeout=60):
            raise _make_http_error(
                req.full_url,
                503,
                b"Downstream error leaking ghs_secret_gh gsk_secret_groq sk-or-secret-or AIzaSyDummyTestKey999",
            )

        env = {
            "GEMINI_MAX_RETRIES": "1",
            "ENABLE_GITHUB_MODELS": "true",
            "GH_TOKEN": "ghs_secret_gh",
            "GROQ_API_KEY": "gsk_secret_groq",
            "OPENROUTER_API_KEY": "sk-or-secret-or",
        }
        with mock.patch.object(gemini_review.urllib.request, "urlopen", side_effect=fake_urlopen):
            output_md, action_txt, _, _ = self._run_main_in_tempdir(
                _make_file_patch("app.py", 150), env, gemini_responder
            )

        self.assertEqual(action_txt, "comment")
        for secret in ("AIzaSyDummyTestKey999", "ghs_secret_gh", "gsk_secret_groq", "sk-or-secret-or"):
            self.assertNotIn(secret, output_md)
        self.assertIn("***", output_md)


class TestConsensusAndParallelModes(_MainRunnerMixin, unittest.TestCase):
    def test_consensus_mode_synthesizes_findings_and_enforces_changes_requested(self):
        def gemini_responder(_model: str, contents: str):
            if "# Individual AI Provider Review Findings" in contents:
                return "#### 📋 Issue Requirements Checklist\n- ✅ Implemented\n\nSynthesized findings.\n\n[APPROVED]"
            return "#### 📋 Issue Requirements Checklist\n- ✅ Implemented\n\n[APPROVED]"

        seen_urls: list[str] = []

        def fake_urlopen(req, timeout=60):
            seen_urls.append(req.full_url)
            if "models.github.ai" in req.full_url:
                return _make_urlopen_response("Found missing edge case test.\n\n[CHANGES_REQUESTED]")
            return _make_urlopen_response("Looks solid overall.\n\n[APPROVED]")

        env = {
            "GEMINI_MAX_RETRIES": "1",
            "REVIEW_MODE": "consensus",
            "CONSENSUS_MAX_PROVIDERS": "3",
            "ENABLE_GITHUB_MODELS": "true",
            "GH_TOKEN": "ghs_test_token",
            "GROQ_API_KEY": "gsk_test_token",
        }
        with mock.patch.object(gemini_review.urllib.request, "urlopen", side_effect=fake_urlopen):
            output_md, action_txt, _, calls = self._run_main_in_tempdir(
                _make_file_patch("app.py", 150), env, gemini_responder
            )

        self.assertEqual(len(calls), 2)
        self.assertIn("# Individual AI Provider Review Findings", calls[1][1])
        self.assertEqual(
            seen_urls,
            [
                "https://models.github.ai/inference/chat/completions",
                "https://api.groq.com/openai/v1/chat/completions",
            ],
        )
        self.assertIn("#### 🤝 Multi-Provider Consensus Summary", output_md)
        self.assertIn("### AI Code Review (Consensus:", output_md)
        self.assertIn("❌ Changes Requested", output_md)
        self.assertEqual(action_txt, "request-changes")

    def test_consensus_mode_handles_partial_provider_failure(self):
        def gemini_responder(_model: str, _contents: str):
            raise RuntimeError("503 UNAVAILABLE")

        def fake_urlopen(_req, timeout=60):
            return _make_urlopen_response("#### 📋 Issue Requirements Checklist\n- ✅ Implemented\n\n[APPROVED]")

        env = {
            "GEMINI_MAX_RETRIES": "1",
            "REVIEW_MODE": "consensus",
            "GROQ_API_KEY": "gsk_test_token",
        }
        with mock.patch.object(gemini_review.urllib.request, "urlopen", side_effect=fake_urlopen):
            output_md, action_txt, _, _ = self._run_main_in_tempdir(
                _make_file_patch("app.py", 150), env, gemini_responder
            )

        self.assertIn("⚠️ Unavailable", output_md)
        self.assertIn("**Google AI Studio**", output_md)
        self.assertIn("✅ Approved", output_md)
        self.assertIn("**Groq**", output_md)
        self.assertEqual(action_txt, "approve")

    def test_parallel_mode_emits_primary_and_extra_review_files(self):
        def gemini_responder(_model: str, _contents: str):
            return "#### 📋 Issue Requirements Checklist\n- ✅ Implemented\n\n[APPROVED]"

        def fake_urlopen(_req, timeout=60):
            return _make_urlopen_response("Missing unit test coverage.\n\n[CHANGES_REQUESTED]")

        artifacts: dict[str, str] = {}
        env = {
            "GEMINI_MAX_RETRIES": "1",
            "REVIEW_MODE": "parallel",
            "GROQ_API_KEY": "gsk_test_token",
        }
        with mock.patch.object(gemini_review.urllib.request, "urlopen", side_effect=fake_urlopen):
            output_md, action_txt, _, _ = self._run_main_in_tempdir(
                _make_file_patch("app.py", 150),
                env,
                gemini_responder,
                extra_artifacts_out=artifacts,
            )

        self.assertEqual(action_txt, "request-changes")
        self.assertIn("### AI Code Review (gemini-3.8-flash via Google AI Studio)", output_md)
        self.assertIn("review_output_extra_1.md", artifacts)
        self.assertIn("### AI Code Review (openai/gpt-oss-120b via Groq)", artifacts["review_output_extra_1.md"])
        self.assertIn("Missing unit test coverage.", artifacts["review_output_extra_1.md"])
        self.assertIn("review_outputs.json", artifacts)
        meta = json.loads(artifacts["review_outputs.json"])
        self.assertEqual([item["provider_id"] for item in meta], ["gemini", "groq"])
        self.assertEqual([item["action"] for item in meta], ["approve", "request-changes"])


class TestMultiReviewerSubagentsAndInlineComments(_MainRunnerMixin, unittest.TestCase):
    def test_two_main_reviewers_with_third_provider_as_fallback(self):
        def gemini_responder(_model: str, _contents: str):
            return "#### 📋 Issue Requirements Checklist\n- ✅ Implemented\n\n[APPROVED]"

        seen_urls: list[str] = []

        def fake_urlopen(req, timeout=60):
            seen_urls.append(req.full_url)
            if "models.github.ai" in req.full_url:
                raise _make_http_error(req.full_url, 429, b"Rate limit exceeded")
            if "api.groq.com" in req.full_url:
                return _make_urlopen_response("Needs null check.\n\n[CHANGES_REQUESTED]")
            raise AssertionError(f"Unexpected 4th provider call to {req.full_url}")

        artifacts: dict[str, str] = {}
        env = {
            "GEMINI_MAX_RETRIES": "1",
            "MAIN_REVIEWERS": "2",
            "EXTRA_PROVIDERS_MODE": "fallback",
            "ENABLE_GITHUB_MODELS": "true",
            "GH_TOKEN": "ghs_test_token",
            "GROQ_API_KEY": "gsk_test_token",
            "OPENROUTER_API_KEY": "sk-or-unused-reserve",
        }
        with mock.patch.object(gemini_review.urllib.request, "urlopen", side_effect=fake_urlopen):
            output_md, action_txt, _, _ = self._run_main_in_tempdir(
                _make_file_patch("app.py", 150),
                env,
                gemini_responder,
                extra_artifacts_out=artifacts,
            )

        self.assertEqual(
            seen_urls,
            [
                "https://models.github.ai/inference/chat/completions",
                "https://api.groq.com/openai/v1/chat/completions",
            ],
        )
        self.assertEqual(action_txt, "request-changes")
        self.assertIn("**Reviewer:** `Main Reviewer #1`", output_md)
        self.assertIn("**Source Provider:** `Google AI Studio`", output_md)
        self.assertIn("**AI Model:** `gemini-3.8-flash`", output_md)

        extra_md = artifacts["review_output_extra_1.md"]
        self.assertIn("**Reviewer:** `Main Reviewer #2`", extra_md)
        self.assertIn("**Source Provider:** `Groq`", extra_md)
        self.assertIn("**AI Model:** `openai/gpt-oss-120b`", extra_md)
        self.assertIn("**Fallback For:** `GitHub Models`", extra_md)

    def test_two_main_reviewers_with_extra_providers_as_subagents(self):
        gemini_prompts: list[str] = []

        def gemini_responder(_model: str, contents: str):
            gemini_prompts.append(contents)
            return "#### 📋 Issue Requirements Checklist\n- ✅ Implemented\n\n[APPROVED]"

        gh_prompts: list[str] = []

        def fake_urlopen(req, timeout=60):
            payload = json.loads(req.data.decode("utf-8"))
            user_msg = payload["messages"][1]["content"]
            if "api.groq.com" in req.full_url:
                return _make_urlopen_response("Subagent found potential SQL injection at app.py:L1.")
            if "models.github.ai" in req.full_url:
                gh_prompts.append(user_msg)
                return _make_urlopen_response("Verified SQL injection.\n\n[CHANGES_REQUESTED]")
            raise AssertionError(f"Unexpected call to {req.full_url}")

        artifacts: dict[str, str] = {}
        env = {
            "GEMINI_MAX_RETRIES": "1",
            "MAIN_REVIEWERS": "2",
            "EXTRA_PROVIDERS_MODE": "subagents",
            "ENABLE_GITHUB_MODELS": "true",
            "GH_TOKEN": "ghs_test_token",
            "GROQ_API_KEY": "gsk_test_token",
        }
        with mock.patch.object(gemini_review.urllib.request, "urlopen", side_effect=fake_urlopen):
            output_md, action_txt, _, _ = self._run_main_in_tempdir(
                _make_file_patch("app.py", 150),
                env,
                gemini_responder,
                extra_artifacts_out=artifacts,
            )

        self.assertEqual(action_txt, "request-changes")
        self.assertIn("# Subagent Reviewer Reports", gemini_prompts[0])
        self.assertIn("Subagent found potential SQL injection at app.py:L1.", gemini_prompts[0])
        self.assertIn("# Subagent Reviewer Reports", gh_prompts[0])
        self.assertIn("**Subagents Consulted:** `openai/gpt-oss-120b via Groq`", output_md)
        self.assertIn("**Subagents Consulted:** `openai/gpt-oss-120b via Groq`", artifacts["review_output_extra_1.md"])

    def test_inline_line_by_line_comments_annotated_extracted_and_attributed(self):
        diff_text = (
            "diff --git a/src/auth.py b/src/auth.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/src/auth.py\n"
            "+++ b/src/auth.py\n"
            "@@ -10,3 +10,4 @@ def login(user):\n"
            " def get_user(cursor, user_id):\n"
            "-    return cursor.execute('SELECT * FROM users')\n"
            "+    query = f'SELECT * FROM users WHERE id = {user_id}'\n"
            "+    return cursor.execute(query)\n"
        )
        annotated, valid_lines = gemini_review.annotate_diff_with_line_numbers(
            gemini_review.parse_diff_files(diff_text)
        )
        self.assertEqual(valid_lines["src/auth.py"], {10, 11, 12})
        self.assertIn("[L11] +    query = f'SELECT * FROM users WHERE id = {user_id}'", annotated[0][1])

        def gemini_responder(_model: str, contents: str):
            self.assertIn("[L11] +    query =", contents)
            return (
                "#### 📋 Issue Requirements Checklist\n- ❌ SQL injection vulnerability\n\n"
                "#### 📍 Line-by-Line Findings\n- `src/auth.py:L11`: Unparameterized SQL query.\n\n"
                "```json:inline-comments\n"
                '[\n'
                '  {"path": "src/auth.py", "line": 11, "severity": "error", "comment": "Use parameterized query.", "suggestion": "    return cursor.execute(\\"SELECT * FROM users WHERE id = ?\\", (user_id,))"},\n'
                '  {"path": "src/other.py", "line": 99, "severity": "warning", "comment": "Unanchored note."}\n'
                ']\n'
                "```\n\n"
                "[CHANGES_REQUESTED]"
            )

        artifacts: dict[str, str] = {}
        output_md, action_txt, _, _ = self._run_main_in_tempdir(
            diff_text,
            {"INLINE_COMMENTS": "true"},
            gemini_responder,
            extra_artifacts_out=artifacts,
        )

        self.assertEqual(action_txt, "request-changes")
        self.assertNotIn("```json:inline-comments", output_md)
        self.assertIn("#### 📌 Additional Line Notes", output_md)
        self.assertIn("**src/other.py:L99** (WARNING): Unanchored note.", output_md)

        payload = json.loads(artifacts["review_payload_1.json"])
        self.assertEqual(payload["event"], "REQUEST_CHANGES")
        self.assertEqual(len(payload["comments"]), 1)
        c = payload["comments"][0]
        self.assertEqual(c["path"], "src/auth.py")
        self.assertEqual(c["line"], 11)
        self.assertEqual(c["side"], "RIGHT")
        self.assertIn("🤖 **Main Reviewer #1** (`gemini-3.8-flash` via **Google AI Studio**) · `ERROR`", c["body"])
        self.assertIn("```suggestion", c["body"])


if __name__ == "__main__":
    unittest.main()
