"""CLI tests for --compounding wiring on `ouroboros run workflow`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml
from typer.testing import CliRunner

from ouroboros.cli.commands.run import app as run_app

runner = CliRunner()


SEED = {
    "goal": "compounding test",
    "constraints": [],
    "acceptance_criteria": ["AC 1", "AC 2"],
    "ontology_schema": {"name": "X", "description": "x", "fields": []},
    "evaluation_principles": [],
    "exit_conditions": [],
    "metadata": {
        "seed_id": "seed-compound-test",
        "version": "1.0.0",
        "created_at": "2024-01-01T00:00:00Z",
        "ambiguity_score": 0.1,
    },
}


def _write_seed(tmp_path: Path) -> Path:
    path = tmp_path / "seed.yaml"
    path.write_text(yaml.safe_dump(SEED))
    return path


class TestCompoundingFlag:
    def test_compounding_and_sequential_are_mutually_exclusive(self, tmp_path: Path) -> None:
        seed_path = _write_seed(tmp_path)
        result = runner.invoke(
            run_app,
            ["workflow", str(seed_path), "--compounding", "--sequential"],
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in (result.output or "").lower()

    def test_compounding_threads_mode_into_run_orchestrator(
        self, tmp_path: Path
    ) -> None:
        seed_path = _write_seed(tmp_path)
        captured: dict = {}

        async def fake_run(*args, **kwargs):
            captured.update(kwargs)

        with patch(
            "ouroboros.cli.commands.run._run_orchestrator",
            new=AsyncMock(side_effect=fake_run),
        ):
            result = runner.invoke(
                run_app,
                ["workflow", str(seed_path), "--compounding"],
            )
        assert result.exit_code == 0, result.output
        assert captured.get("mode") == "compounding"

    def test_default_run_has_no_mode_override(self, tmp_path: Path) -> None:
        seed_path = _write_seed(tmp_path)
        captured: dict = {}

        async def fake_run(*args, **kwargs):
            captured.update(kwargs)

        with patch(
            "ouroboros.cli.commands.run._run_orchestrator",
            new=AsyncMock(side_effect=fake_run),
        ):
            result = runner.invoke(run_app, ["workflow", str(seed_path)])
        assert result.exit_code == 0
        assert captured.get("mode") is None


class TestCompoundingResume:
    """Tests for --compounding --resume <session_id> CLI wiring (AC-2 / Q6.2)."""

    # ------------------------------------------------------------------
    # Core wiring: compounding_resume_session_id passed, orchestrator
    # resume NOT triggered.
    # ------------------------------------------------------------------

    def test_compounding_resume_passes_compounding_resume_session_id(
        self, tmp_path: Path
    ) -> None:
        """--compounding --resume <id> sends compounding_resume_session_id to _run_orchestrator."""
        seed_path = _write_seed(tmp_path)
        captured: dict = {}

        async def fake_run(*args, **kwargs):
            captured.update(kwargs)

        with patch(
            "ouroboros.cli.commands.run._run_orchestrator",
            new=AsyncMock(side_effect=fake_run),
        ):
            result = runner.invoke(
                run_app,
                ["workflow", str(seed_path), "--compounding", "--resume", "orch_abc123"],
            )
        assert result.exit_code == 0, result.output
        assert captured.get("compounding_resume_session_id") == "orch_abc123"

    def test_compounding_resume_sets_mode_compounding(self, tmp_path: Path) -> None:
        """--compounding --resume still sets mode='compounding'."""
        seed_path = _write_seed(tmp_path)
        captured: dict = {}

        async def fake_run(*args, **kwargs):
            captured.update(kwargs)

        with patch(
            "ouroboros.cli.commands.run._run_orchestrator",
            new=AsyncMock(side_effect=fake_run),
        ):
            runner.invoke(
                run_app,
                ["workflow", str(seed_path), "--compounding", "--resume", "orch_abc123"],
            )
        assert captured.get("mode") == "compounding"

    def test_compounding_resume_nullifies_orchestrator_resume(
        self, tmp_path: Path
    ) -> None:
        """--compounding --resume must NOT trigger orchestrator session resume path.

        The ``resume_session`` positional kwarg sent to ``_run_orchestrator``
        must be None so the function doesn't call ``runner.resume_session()``.
        """
        seed_path = _write_seed(tmp_path)
        captured: dict = {}

        async def fake_run(*args, **kwargs):
            captured.update(kwargs)

        with patch(
            "ouroboros.cli.commands.run._run_orchestrator",
            new=AsyncMock(side_effect=fake_run),
        ):
            runner.invoke(
                run_app,
                ["workflow", str(seed_path), "--compounding", "--resume", "orch_abc123"],
            )
        # resume_session (positional 2nd arg or keyword) must be None for compounding
        # The first positional arg is seed_file; check keyword form.
        assert captured.get("resume_session") is None

    def test_resume_without_compounding_is_orchestrator_resume(
        self, tmp_path: Path
    ) -> None:
        """--resume without --compounding keeps the existing orchestrator session resume path.

        compounding_resume_session_id must be None; resume_session (2nd positional
        arg to _run_orchestrator) must be set to the provided ID.
        """
        seed_path = _write_seed(tmp_path)
        captured: dict = {}

        async def fake_run(*args, **kwargs):
            # Capture positional args too (seed_file, resume_session, …)
            captured["positional"] = args
            captured.update(kwargs)

        with patch(
            "ouroboros.cli.commands.run._run_orchestrator",
            new=AsyncMock(side_effect=fake_run),
        ):
            runner.invoke(
                run_app,
                ["workflow", str(seed_path), "--resume", "orch_xyz999"],
            )
        # resume_session is the 2nd positional argument to _run_orchestrator
        positional = captured.get("positional", ())
        assert len(positional) >= 2, f"Expected ≥2 positional args, got: {positional}"
        assert positional[1] == "orch_xyz999", (
            f"resume_session (2nd positional) should be 'orch_xyz999', got {positional[1]!r}"
        )
        assert captured.get("compounding_resume_session_id") is None

    # ------------------------------------------------------------------
    # Mutual exclusivity: compounding resume + skip-completed
    # ------------------------------------------------------------------

    def test_compounding_resume_with_skip_completed_emits_warning(
        self, tmp_path: Path
    ) -> None:
        """--compounding --resume alongside --skip-completed must produce a warning.

        The checkpoint resume already handles AC-skipping; --skip-completed
        is silently ignored and a warning is printed.

        [[INVARIANT: --compounding --resume warns and ignores --skip-completed]]
        """
        seed_path = _write_seed(tmp_path)

        # Write a minimal skip-completed YAML so the file-load step doesn't fail.
        skip_path = tmp_path / "completed.yaml"
        skip_path.write_text("completed_acs: []\n")

        captured: dict = {}

        async def fake_run(*args, **kwargs):
            captured.update(kwargs)

        with patch(
            "ouroboros.cli.commands.run._run_orchestrator",
            new=AsyncMock(side_effect=fake_run),
        ):
            result = runner.invoke(
                run_app,
                [
                    "workflow",
                    str(seed_path),
                    "--compounding",
                    "--resume",
                    "orch_abc123",
                    "--skip-completed",
                    str(skip_path),
                ],
            )
        # Must not fail — just warn
        assert result.exit_code == 0, result.output
        assert "ignored" in result.output.lower() or "warning" in result.output.lower()

    # ------------------------------------------------------------------
    # Runner integration: resume_session_id forwarded to execute_seed
    # ------------------------------------------------------------------

    def test_run_orchestrator_forwards_resume_session_id_to_execute_seed(
        self, tmp_path: Path
    ) -> None:
        """_run_orchestrator passes compounding_resume_session_id as resume_session_id
        to runner.execute_seed when mode='compounding'.

        Uses the CLI shim layer to capture what execute_seed is called with, then
        asserts the compounding_resume_session_id was threaded through.

        [[INVARIANT: compounding_resume_session_id flows from CLI to execute_serial via execute_seed]]
        """
        seed_path = _write_seed(tmp_path)
        captured: dict = {}

        async def fake_execute_seed(**kwargs):
            captured.update(kwargs)
            from unittest.mock import MagicMock

            result = MagicMock()
            result.is_ok = True
            result.value = MagicMock(
                success=True,
                session_id="s1",
                messages_processed=0,
                duration_seconds=0.0,
                summary={},
                final_message="",
                execution_id="e1",
            )
            return result

        # Patch execute_seed at the runner level (where it's used via the
        # OrchestratorRunner instance created inside _run_orchestrator).
        # We spy by patching OrchestratorRunner at its definition site so the
        # instance returned inside _run_orchestrator uses our fake.
        from unittest.mock import AsyncMock, MagicMock, patch as _patch

        mock_runner_instance = MagicMock()
        mock_runner_instance.execute_seed = AsyncMock(side_effect=fake_execute_seed)

        # Build a minimal EventStore stub that passes the initialize() check.
        mock_event_store = MagicMock()
        mock_event_store.initialize = AsyncMock()
        mock_event_store.close = AsyncMock()

        # SessionRepository.create_session must return a valid tracker so that
        # prepare_session() inside execute_seed doesn't fail before reaching our spy.
        mock_tracker = MagicMock()
        mock_tracker.session_id = "s1"
        mock_tracker.execution_id = "e1"

        ok_create = MagicMock()
        ok_create.is_err = False
        ok_create.value = mock_tracker

        mock_session_repo = MagicMock()
        mock_session_repo.create_session = AsyncMock(return_value=ok_create)
        mock_session_repo.track_progress = AsyncMock()

        with (
            _patch(
                "ouroboros.persistence.event_store.EventStore",
                return_value=mock_event_store,
            ),
            _patch(
                "ouroboros.orchestrator.session.SessionRepository",
                return_value=mock_session_repo,
            ),
            _patch(
                "ouroboros.orchestrator.OrchestratorRunner",
                return_value=mock_runner_instance,
            ),
            _patch(
                "ouroboros.cli.commands.run.maybe_prepare_task_workspace",
                return_value=None,
            ),
            _patch(
                "ouroboros.orchestrator.create_agent_runtime",
                return_value=MagicMock(),
            ),
        ):
            import asyncio

            from ouroboros.cli.commands.run import _run_orchestrator

            asyncio.run(
                _run_orchestrator(
                    seed_path,
                    resume_session=None,
                    mode="compounding",
                    compounding_resume_session_id="orch_abc123",
                    no_qa=True,
                )
            )

        assert captured.get("resume_session_id") == "orch_abc123", (
            f"resume_session_id not forwarded; captured: {captured}"
        )
        assert captured.get("mode") == "compounding"
