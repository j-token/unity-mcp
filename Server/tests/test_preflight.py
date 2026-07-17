from __future__ import annotations

import pytest


def _editor_state(*, sequence: int, stale: bool, blocked: bool = False) -> dict:
    return {
        "success": True,
        "data": {
            "sequence": sequence,
            "staleness": {"is_stale": stale},
            "compilation": {
                "is_compiling": blocked,
                "is_domain_reload_pending": False,
                "is_request_pending": False,
            },
            "tests": {"is_running": False, "current_job_id": None},
            "editor": {
                "play_mode": {"is_playing": False, "is_changing": False}
            },
            "assets": {
                "is_updating": False,
                "external_changes_dirty": False,
                "refresh": {"is_refresh_in_progress": False},
            },
        },
    }


@pytest.mark.asyncio
async def test_test_readiness_accepts_two_fresh_idle_snapshots_with_same_sequence(
    monkeypatch,
):
    import services.resources.editor_state as editor_state_module
    import services.tools.preflight as preflight_module

    snapshots = iter(
        [
            _editor_state(sequence=6, stale=False),
            _editor_state(sequence=6, stale=False),
        ]
    )

    async def fake_get_editor_state(ctx):
        return next(snapshots)

    async def no_sleep(delay):
        return None

    monkeypatch.setattr(preflight_module, "_in_pytest", lambda: False)
    monkeypatch.setattr(editor_state_module, "get_editor_state", fake_get_editor_state)
    monkeypatch.setattr(preflight_module.asyncio, "sleep", no_sleep)

    result = await preflight_module.preflight(
        object(), require_test_readiness=True, max_wait_s=1
    )

    assert result is None


@pytest.mark.asyncio
async def test_test_readiness_rejects_a_stale_idle_snapshot(monkeypatch):
    import services.resources.editor_state as editor_state_module
    import services.tools.preflight as preflight_module

    async def fake_get_editor_state(ctx):
        return _editor_state(sequence=6, stale=True)

    monkeypatch.setattr(preflight_module, "_in_pytest", lambda: False)
    monkeypatch.setattr(editor_state_module, "get_editor_state", fake_get_editor_state)

    result = await preflight_module.preflight(
        object(), require_test_readiness=True, max_wait_s=0
    )

    assert result is not None
    assert result.success is False
    assert result.data == {
        "reason": "editor_not_ready_for_tests",
        "retry_after_ms": 500,
    }


@pytest.mark.asyncio
async def test_test_readiness_requires_fresh_unblocked_samples_to_be_consecutive(
    monkeypatch,
):
    import services.resources.editor_state as editor_state_module
    import services.tools.preflight as preflight_module

    snapshots = iter(
        [
            _editor_state(sequence=6, stale=False),
            _editor_state(sequence=7, stale=False, blocked=True),
            _editor_state(sequence=8, stale=False),
            _editor_state(sequence=8, stale=False),
        ]
    )
    observed = 0

    async def fake_get_editor_state(ctx):
        nonlocal observed
        observed += 1
        return next(snapshots)

    async def no_sleep(delay):
        return None

    monkeypatch.setattr(preflight_module, "_in_pytest", lambda: False)
    monkeypatch.setattr(editor_state_module, "get_editor_state", fake_get_editor_state)
    monkeypatch.setattr(preflight_module.asyncio, "sleep", no_sleep)

    result = await preflight_module.preflight(
        object(), require_test_readiness=True, max_wait_s=1
    )

    assert result is None
    assert observed == 4
