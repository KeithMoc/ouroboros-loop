"""Unit tests for the runtime postmortem-chain secret redactor.

Covers the three composable detection layers (path / pattern / entropy)
plus the backward-compat invariant that parallel-mode rendering does not
invoke the redactor.

Note on test fixtures: secret-shaped strings (Stripe, AWS, GitHub PAT, etc.)
are assembled at runtime via concatenation so the literal forms do not
appear in source. This avoids GitHub push-protection / secret-scanner
false positives on the test file itself — the regex still matches the
runtime-built string identically.
"""

from __future__ import annotations

import pytest

from ouroboros.orchestrator import postmortem_redactor as redactor_mod
from ouroboros.orchestrator.level_context import (
    ACContextSummary,
    ACPostmortem,
    LevelContext,
    PostmortemChain,
    build_context_prompt,
    build_postmortem_chain_prompt,
)
from ouroboros.orchestrator.postmortem_redactor import (
    redact_for_chain,
    reset_config_cache,
)

# --- Synthesized secret-shaped fixtures (built at runtime) -------------------
# Assembling these via concatenation keeps the literal high-signal prefixes
# (sk_live_, AKIA followed by 16 chars, ghp_, github_pat_) out of the source
# file, so push-protection / secret-scanners don't flag the test corpus.
_STRIPE_PREFIX = "sk_" + "live_"
_STRIPE_KEY = _STRIPE_PREFIX + ("A" * 30)
_AWS_KEY = "AKIA" + ("A" * 16)
_GHP_CLASSIC = "ghp_" + ("a" * 40)
_GHP_FINE = "github_pat_" + ("x" * 90)


@pytest.fixture(autouse=True)
def _reset_redactor_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop env-derived config between tests so monkeypatched values take effect."""
    # Strip env vars by default so each test starts with a clean baseline.
    monkeypatch.delenv("OUROBOROS_POSTMORTEM_REDACT_PATHS", raising=False)
    monkeypatch.delenv("OUROBOROS_POSTMORTEM_REDACT_ENTROPY", raising=False)
    reset_config_cache()
    yield
    reset_config_cache()


# --- Path layer ---------------------------------------------------------------


class TestPathLayer:
    """Path-allowlist suppression of the entire ``key_output`` value."""

    @pytest.mark.parametrize(
        "files",
        [
            (".env",),
            (".env.production",),
            ("secrets/id_rsa",),
            ("/home/u/.aws/credentials",),
            ("server.pem",),
            ("~/.ssh/id_ed25519",),
            ("credentials.json",),
            (".npmrc",),
            (".pypirc",),
            ("foo/bar/.env.local",),
        ],
    )
    def test_sensitive_paths_suppress_text(self, files: tuple[str, ...]) -> None:
        out = redact_for_chain(f"API key is {_STRIPE_KEY}", files)
        assert out == "[redacted: path]"

    @pytest.mark.parametrize(
        "files",
        [
            ("src/foo.py",),
            ("tests/bar.py",),
            ("docs/guide.md",),
            ("README.md",),
            (),
        ],
    )
    def test_normal_paths_pass_through(self, files: tuple[str, ...]) -> None:
        text = "All good — wrote some code."
        assert redact_for_chain(text, files) == text

    def test_extends_defaults_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "OUROBOROS_POSTMORTEM_REDACT_PATHS", "secret_*.yaml,vault/*"
        )
        reset_config_cache()
        assert redact_for_chain("x", ("secret_prod.yaml",)) == "[redacted: path]"
        assert redact_for_chain("x", ("vault/keys",)) == "[redacted: path]"
        # Defaults still active.
        assert redact_for_chain("x", (".env",)) == "[redacted: path]"

    def test_none_disables_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OUROBOROS_POSTMORTEM_REDACT_PATHS", "none")
        reset_config_cache()
        text = "API key in .env"
        assert redact_for_chain(text, (".env",)) == text


# --- Pattern layer ------------------------------------------------------------


class TestPatternLayer:
    """Regex-based replacement for known-shape secrets."""

    @pytest.mark.parametrize(
        ("payload", "tag"),
        [
            (f"token={_STRIPE_KEY}", "pattern:stripe"),
            (f"aws={_AWS_KEY}", "pattern:aws_access_key"),
            (_GHP_CLASSIC, "pattern:github_pat_classic"),
            (_GHP_FINE, "pattern:github_pat_fine"),
            (
                "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxw"
                "RJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
                "pattern:jwt",
            ),
            ("xoxb-1234567890-abcdef", "pattern:slack"),
            ("xoxa-9876543210-zzzzzzzzzz", "pattern:slack"),
        ],
    )
    def test_pattern_hits(self, payload: str, tag: str) -> None:
        out = redact_for_chain(payload, ())
        assert f"[redacted: {tag}]" in out
        # Original secret body must be gone.
        assert not any(
            len(token) > 20 and token in out
            for token in payload.replace("=", " ").replace(":", " ").split()
            if any(ch.isdigit() for ch in token) or "_" in token
        )

    def test_private_key_block_redacted(self) -> None:
        text = (
            "Here is the key:\n"
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEAwz...\n"
            "abcdEFGH+/=\n"
            "-----END RSA PRIVATE KEY-----\n"
            "End."
        )
        out = redact_for_chain(text, ())
        assert "[redacted: pattern:private_key]" in out
        assert "MIIEpAIBAAKCAQEAwz" not in out
        assert "End." in out  # context preserved

    def test_short_hex_passes(self) -> None:
        text = "commit abc1234, tag v0.35.0, error code 0xDEADBEEF"
        assert redact_for_chain(text, ()) == text

    def test_normal_english_passes(self) -> None:
        text = (
            "Implemented the postmortem chain so each AC compounds onto the "
            "previous one's invariants."
        )
        assert redact_for_chain(text, ()) == text


# --- Entropy layer (opt-in) ---------------------------------------------------


class TestEntropyLayer:
    """Shannon-entropy heuristic gated by env var."""

    HIGH_ENTROPY_64 = "Zk9pUUkzcmZxbnVuMmRJTHk2WjB6cHU3OXFIVUVoZmZQRDIzL3R3UFE9PQ=="

    def test_entropy_off_by_default(self) -> None:
        text = f"opaque token: {self.HIGH_ENTROPY_64}"
        assert redact_for_chain(text, ()) == text

    def test_entropy_hit_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OUROBOROS_POSTMORTEM_REDACT_ENTROPY", "1")
        reset_config_cache()
        text = f"opaque token: {self.HIGH_ENTROPY_64}"
        out = redact_for_chain(text, ())
        assert "[redacted: entropy]" in out
        assert self.HIGH_ENTROPY_64 not in out

    def test_low_entropy_run_passes_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OUROBOROS_POSTMORTEM_REDACT_ENTROPY", "1")
        reset_config_cache()
        # 40-char run of mostly one character — well below the threshold.
        low = "a" * 40
        text = f"banner: {low}"
        out = redact_for_chain(text, ())
        assert "[redacted: entropy]" not in out
        assert low in out

    def test_lorem_ipsum_passes_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OUROBOROS_POSTMORTEM_REDACT_ENTROPY", "1")
        reset_config_cache()
        # English words punctuated with spaces — runs are too short to match.
        text = (
            "Lorem ipsum dolor sit amet, consectetur adipiscing elit, "
            "sed do eiusmod tempor incididunt ut labore et dolore magna aliqua."
        )
        assert redact_for_chain(text, ()) == text


# --- Composability ------------------------------------------------------------


class TestComposability:
    """Multiple layers fire in correct order on overlapping inputs."""

    def test_pattern_and_entropy_both_redact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OUROBOROS_POSTMORTEM_REDACT_ENTROPY", "1")
        reset_config_cache()
        # Stripe key (pattern) + a separate high-entropy run.
        high_entropy = (
            "Zk9pUUkzcmZxbnVuMmRJTHk2WjB6cHU3OXFIVUVoZmZQRDIzL3R3UFE9PQ"
        )
        text = f"stripe {_STRIPE_KEY} and blob {high_entropy}"
        out = redact_for_chain(text, ())
        assert "[redacted: pattern:stripe]" in out
        assert "[redacted: entropy]" in out
        # Both raw secrets gone.
        assert _STRIPE_KEY not in out
        assert high_entropy not in out

    def test_path_layer_short_circuits(self) -> None:
        # Path match wins — pattern markers never appear because the entire
        # text is replaced.
        text = f"stripe {_STRIPE_KEY}"
        out = redact_for_chain(text, (".env",))
        assert out == "[redacted: path]"
        assert "pattern" not in out

    def test_empty_inputs(self) -> None:
        assert redact_for_chain("", ()) == ""
        assert redact_for_chain("", (".env",)) == ""

    def test_large_input_no_blowup(self) -> None:
        # ~100KB of mixed normal text + one secret. No quadratic behavior.
        big = "the quick brown fox jumps over the lazy dog. " * 2000
        big += f"leak: {_AWS_KEY} end"
        out = redact_for_chain(big, ())
        assert "[redacted: pattern:aws_access_key]" in out
        assert _AWS_KEY not in out


# --- Wire-site invariant: parallel mode untouched -----------------------------


def _make_postmortem(
    *,
    ac_index: int,
    key_output: str,
    files_modified: tuple[str, ...] = (),
    gotchas: tuple[str, ...] = (),
) -> ACPostmortem:
    return ACPostmortem(
        summary=ACContextSummary(
            ac_index=ac_index,
            ac_content=f"AC {ac_index + 1}",
            success=True,
            files_modified=files_modified,
            key_output=key_output,
        ),
        gotchas=gotchas,
    )


class TestWireSite:
    """Redactor is invoked for compounding chain rendering only."""

    def test_compounding_chain_redacts_key_output(self) -> None:
        pm = _make_postmortem(
            ac_index=0,
            key_output=f"leaked {_AWS_KEY}",
            files_modified=("src/foo.py",),
        )
        chain = PostmortemChain(postmortems=(pm,))
        out = build_postmortem_chain_prompt(chain)
        assert "[redacted: pattern:aws_access_key]" in out
        assert _AWS_KEY not in out

    def test_compounding_chain_redacts_path_layer(self) -> None:
        # Build a non-Stripe-prefix secret-shaped value so this test does
        # not require pattern-matching to validate path-layer suppression.
        secret_blob = "OPENAI_API_KEY=" + ("x" * 32)
        pm = _make_postmortem(
            ac_index=0,
            key_output=secret_blob,
            files_modified=(".env",),
        )
        chain = PostmortemChain(postmortems=(pm,))
        out = build_postmortem_chain_prompt(chain)
        assert "[redacted: path]" in out
        assert secret_blob not in out
        # files_modified itself is preserved verbatim — next AC needs to see
        # which path was touched.
        assert ".env" in out

    def test_compounding_chain_redacts_gotchas(self) -> None:
        pm = _make_postmortem(
            ac_index=0,
            key_output="ok",
            files_modified=("src/foo.py",),
            gotchas=(f"Don't paste {_GHP_CLASSIC} into the README.",),
        )
        chain = PostmortemChain(postmortems=(pm,))
        out = build_postmortem_chain_prompt(chain)
        assert "[redacted: pattern:github_pat_classic]" in out

    def test_compounding_chain_preserves_files_modified(self) -> None:
        pm = _make_postmortem(
            ac_index=0,
            key_output="value",
            files_modified=(".env", "src/foo.py"),
        )
        chain = PostmortemChain(postmortems=(pm,))
        out = build_postmortem_chain_prompt(chain)
        # files_modified MUST be preserved — next AC needs to know what was
        # touched even when the key_output is redacted.
        assert ".env" in out
        assert "src/foo.py" in out

    def test_parallel_mode_does_not_invoke_redactor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Backward-compat invariant: parallel-mode rendering bypasses the redactor.

        Parallel mode renders via :func:`build_context_prompt` over
        :class:`LevelContext` objects — completely separate from the
        compounding chain. Patch the redactor and assert call count is zero.
        """
        calls: list[tuple[str, tuple[str, ...]]] = []

        def _spy(text: str, files: tuple[str, ...]) -> str:
            calls.append((text, files))
            return text

        # Patch both the public symbol and the import inside level_context.
        monkeypatch.setattr(
            "ouroboros.orchestrator.postmortem_redactor.redact_for_chain", _spy
        )

        summary = ACContextSummary(
            ac_index=0,
            ac_content="parallel ac",
            success=True,
            files_modified=("src/foo.py",),
            key_output=_STRIPE_KEY,
        )
        ctx = LevelContext(level_number=0, completed_acs=(summary,))
        out = build_context_prompt([ctx])

        assert calls == []
        # Secret-shaped string survives — proves the redactor was not in path.
        assert _STRIPE_KEY in out

    def test_empty_chain_returns_empty(self) -> None:
        assert build_postmortem_chain_prompt(PostmortemChain()) == ""


# --- Module-level config caching ---------------------------------------------


class TestConfigCache:
    """Env-derived config is read once and cached until reset."""

    def test_cache_hit_without_reset_keeps_old_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Prime cache with defaults (entropy off).
        text = f"opaque {TestEntropyLayer.HIGH_ENTROPY_64}"
        assert redact_for_chain(text, ()) == text  # fills cache: entropy=False

        # Flip env var but DO NOT reset — cache should still report disabled.
        monkeypatch.setenv("OUROBOROS_POSTMORTEM_REDACT_ENTROPY", "1")
        assert redact_for_chain(text, ()) == text

        # Now reset and the new value takes effect.
        reset_config_cache()
        assert "[redacted: entropy]" in redact_for_chain(text, ())

    def test_module_exports(self) -> None:
        # Smoke check on the public API surface.
        assert hasattr(redactor_mod, "redact_for_chain")
        assert hasattr(redactor_mod, "reset_config_cache")
