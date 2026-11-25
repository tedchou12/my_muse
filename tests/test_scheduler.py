from __future__ import annotations

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.audit import AuditLog
from app.db import Database
from app.scheduler import (
    ScheduleError,
    Scheduler,
    compute_next_run,
    parse_interval,
    parse_once,
)


@pytest.fixture()
def scheduler(tmp_path):
    db = Database(tmp_path / "scheduler.db")
    return Scheduler(db, AuditLog(db), timezone="UTC", enabled=False)


def test_interval_parsing():
    assert parse_interval("90") == 90
    assert parse_interval("30m") == 1800
    assert parse_interval("2h") == 7200
    assert parse_interval("1d") == 86400
    with pytest.raises(ScheduleError):
        parse_interval("30")
    with pytest.raises(ScheduleError):
        parse_interval("soon")


def test_compute_next_run_interval_and_cron():
    base = 1_700_000_000.0
    assert compute_next_run("interval", "90", "UTC", base) == base + 90

    utc = ZoneInfo("UTC")
    base_dt = datetime(2026, 1, 5, 6, 30, tzinfo=utc)
    nxt = compute_next_run("cron", "0 8 * * *", "UTC", base_dt.timestamp())
    assert datetime.fromtimestamp(nxt, tz=utc) == datetime(
        2026, 1, 5, 8, 0, tzinfo=utc
    )

    with pytest.raises(ScheduleError):
        compute_next_run("cron", "not a cron", "UTC")
    with pytest.raises(ScheduleError):
        compute_next_run("cron", "0 8 * * *", "Mars/Olympus")


def test_parse_once_timezone():
    expected = datetime(
        2026, 12, 1, 9, 30, tzinfo=ZoneInfo("Europe/Berlin")
    ).timestamp()
    assert parse_once("2026-12-01T09:30", "Europe/Berlin") == expected
    with pytest.raises(ScheduleError):
        parse_once("tomorrow", "UTC")


def test_create_list_pause_resume_delete(scheduler):
    task = scheduler.create(
        "Morning briefing", "Summarise the news", "once", "2030-01-01T09:00"
    )
    assert task["active"] == 1
    assert task["next_run_at"] is not None
    assert [item["id"] for item in scheduler.list_tasks()] == [task["id"]]

    paused = scheduler.set_active(task["id"], False)
    assert paused["active"] == 0 and paused["next_run_at"] is None

    resumed = scheduler.set_active(task["id"], True)
    assert resumed["active"] == 1

    with pytest.raises(ScheduleError):
        scheduler.create("", "p", "once", "2030-01-01T09:00")
    with pytest.raises(ScheduleError):
        scheduler.create("t", "", "once", "2030-01-01T09:00")

    scheduler.delete(task["id"])
    with pytest.raises(ScheduleError):
        scheduler.get(task["id"])


async def test_poll_runs_due_once_task(scheduler):
    seen = []

    async def runner(task):
        seen.append(task["id"])
        return "finished: %s" % task["title"]

    scheduler.runner = runner
    task = scheduler.create("Old task", "Do it", "once", "2020-01-01T00:00")
    started = await scheduler.poll_once()
    assert started == [task["id"]]
    await asyncio.sleep(0.2)

    assert seen == [task["id"]]
    runs = scheduler.runs(task["id"])
    assert runs[0]["status"] == "ok"
    assert runs[0]["result"] == "finished: Old task"
    fresh = scheduler.get(task["id"])
    assert fresh["active"] == 0
    assert fresh["next_run_at"] is None
    assert fresh["run_count"] == 1


async def test_interval_task_reschedules_itself(scheduler):
    async def runner(task):
        return "tick"

    scheduler.runner = runner
    task = scheduler.create("Recurring", "Tick", "interval", "60")
    scheduler.db.execute(
        "UPDATE tasks SET next_run_at = ? WHERE id = ?",
        (time.time() - 1, task["id"]),
    )
    await scheduler.poll_once()
    await asyncio.sleep(0.2)

    fresh = scheduler.get(task["id"])
    assert fresh["active"] == 1
    assert fresh["next_run_at"] > time.time()
    assert fresh["run_count"] == 1


async def test_runner_failure_is_recorded(scheduler):
    async def runner(task):
        raise RuntimeError("boom")

    scheduler.runner = runner
    task = scheduler.create("Broken", "Fail", "once", "2020-01-01T00:00")
    await scheduler.poll_once()
    await asyncio.sleep(0.2)

    run = scheduler.runs(task["id"])[0]
    assert run["status"] == "error"
    assert "boom" in run["result"]
    assert scheduler.get(task["id"])["last_status"] == "error"


async def test_notifier_receives_finished_runs(scheduler):
    seen = []

    async def runner(task):
        return "finished: %s" % task["title"]

    async def notifier(task, status, result):
        seen.append((task["id"], status, result))

    scheduler.runner = runner
    scheduler.notifier = notifier
    task = scheduler.create("Ping", "Say hi", "once", "2026-01-01T00:00")
    scheduler.db.execute(
        "UPDATE tasks SET next_run_at = ? WHERE id = ?", (time.time() - 1, task["id"])
    )
    await scheduler.poll_once()
    await asyncio.sleep(0.2)

    assert seen == [(task["id"], "ok", "finished: Ping")]


async def test_notifier_reports_failures(scheduler):
    seen = []

    async def runner(task):
        raise RuntimeError("boom")

    async def notifier(task, status, result):
        seen.append((status, result))

    scheduler.runner = runner
    scheduler.notifier = notifier
    task = scheduler.create("Ping", "Say hi", "once", "2026-01-01T00:00")
    scheduler.db.execute(
        "UPDATE tasks SET next_run_at = ? WHERE id = ?", (time.time() - 1, task["id"])
    )
    await scheduler.poll_once()
    await asyncio.sleep(0.2)

    assert seen and seen[0][0] == "error"
    assert "boom" in seen[0][1]


def test_recover_moves_missed_cron_forward(scheduler):
    task = scheduler.create("Daily", "Report", "cron", "0 8 * * *")
    scheduler.db.execute(
        "UPDATE tasks SET next_run_at = ? WHERE id = ?",
        (time.time() - 86_400, task["id"]),
    )
    scheduler._recover()
    fresh = scheduler.get(task["id"])
    assert fresh["next_run_at"] > time.time()
