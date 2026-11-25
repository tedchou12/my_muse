"""Durable task scheduler (once / interval / cron) with run history.

Jobs live in SQLite, so they survive restarts. A single asyncio loop claims due
jobs, moves ``next_run_at`` forward before executing, then runs the agent with
the job's prompt. Results are stored per task.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

from croniter import croniter

from .audit import AuditLog
from .db import Database, now_ts

KINDS = ("once", "interval", "cron")
INTERVAL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class ScheduleError(ValueError):
    pass


def _zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception:
        raise ScheduleError("Unknown timezone %r." % tz_name)


def parse_interval(value: str) -> int:
    raw = str(value).strip().lower()
    if not raw:
        raise ScheduleError("Interval is required.")
    multiplier = 1
    if raw[-1] in INTERVAL_UNITS:
        multiplier = INTERVAL_UNITS[raw[-1]]
        raw = raw[:-1]
    try:
        seconds = int(float(raw) * multiplier)
    except ValueError:
        raise ScheduleError("Interval must be a number like 3600, 30m or 2h.")
    if seconds < 60:
        raise ScheduleError("Interval must be at least 60 seconds.")
    if seconds > 365 * 86400:
        raise ScheduleError("Interval must be at most 365 days.")
    return seconds


def parse_once(schedule: str, tz_name: str) -> float:
    raw = str(schedule).strip()
    if not raw:
        raise ScheduleError("A date/time is required.")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        raise ScheduleError("Expected an ISO date/time such as 2026-12-01T09:30.")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_zone(tz_name))
    return moment.timestamp()


def compute_next_run(
    kind: str, schedule: str, tz_name: str, base_ts: Optional[float] = None
) -> float:
    """Return the next run time as a UTC epoch, or raise ScheduleError."""
    if kind not in KINDS:
        raise ScheduleError("kind must be one of: %s" % ", ".join(KINDS))
    base = float(base_ts if base_ts is not None else time.time())
    if kind == "once":
        return parse_once(schedule, tz_name)
    if kind == "interval":
        return base + parse_interval(schedule)
    zone = _zone(tz_name)
    base_dt = datetime.fromtimestamp(base, tz=zone)
    try:
        nxt = croniter(str(schedule).strip(), base_dt).get_next(datetime)
    except Exception as exc:
        raise ScheduleError("Invalid cron expression: %s" % exc)
    return float(nxt.timestamp())


class Scheduler:
    def __init__(
        self,
        db: Database,
        audit: AuditLog,
        timezone: str = "UTC",
        tick_seconds: float = 2.0,
        max_concurrent: int = 2,
        enabled: bool = True,
    ) -> None:
        self.db = db
        self.audit = audit
        self.timezone = timezone
        self.tick_seconds = max(0.25, float(tick_seconds))
        self.max_concurrent = max(1, int(max_concurrent))
        self.enabled = enabled
        self.runner: Optional[Callable[[Dict[str, Any]], Awaitable[str]]] = None
        self.notifier: Optional[
            Callable[[Dict[str, Any], str, str], Awaitable[None]]
        ] = None
        self._loop_task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._in_flight: Set[int] = set()

    # -- persistence ------------------------------------------------------
    def get(self, task_id: int) -> Dict[str, Any]:
        row = self.db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not row:
            raise ScheduleError("Task %s not found." % task_id)
        return row

    def list_tasks(self) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT t.id, t.title, t.prompt, t.kind, t.schedule, t.timezone, "
            "t.active, t.next_run_at, t.last_run_at, t.last_status, "
            "t.last_result, t.run_count, t.created_at, t.agent_id, "
            "a.name AS agent_name FROM tasks t "
            "LEFT JOIN agents a ON a.id = t.agent_id "
            "ORDER BY t.active DESC, "
            "COALESCE(t.next_run_at, 1e18) ASC, t.id DESC"
        )

    def create(
        self,
        title: str,
        prompt: str,
        kind: str,
        schedule: str,
        timezone: Optional[str] = None,
        agent_id: Optional[int] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        title = (title or "").strip()
        prompt = (prompt or "").strip()
        if not title or len(title) > 120:
            raise ScheduleError("Title is required (max 120 characters).")
        if not prompt:
            raise ScheduleError("Prompt is required.")
        tz_name = (timezone or self.timezone or "UTC").strip()
        context = str(context or "").strip()[:6000]
        next_run = compute_next_run(kind, schedule, tz_name)
        task_id = self.db.insert(
            "INSERT INTO tasks (title, prompt, kind, schedule, timezone, active, "
            "next_run_at, created_at, agent_id, context) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (
                title,
                prompt,
                kind,
                str(schedule).strip(),
                tz_name,
                next_run,
                now_ts(),
                int(agent_id) if agent_id else None,
                context,
            ),
        )
        self.audit.log(
            "user",
            "task_create",
            {
                "id": task_id,
                "title": title,
                "kind": kind,
                "schedule": schedule,
                "agent_id": int(agent_id) if agent_id else None,
            },
        )
        return self.get(task_id)

    def set_active(self, task_id: int, active: bool) -> Dict[str, Any]:
        task = self.get(task_id)
        if active:
            next_run = task["next_run_at"]
            if next_run is None or next_run < time.time():
                if task["kind"] == "once":
                    next_run = time.time()
                else:
                    next_run = compute_next_run(
                        task["kind"], task["schedule"], task["timezone"]
                    )
            self.db.execute(
                "UPDATE tasks SET active = 1, next_run_at = ? WHERE id = ?",
                (next_run, task_id),
            )
        else:
            self.db.execute(
                "UPDATE tasks SET active = 0, next_run_at = NULL WHERE id = ?",
                (task_id,),
            )
        self.audit.log("user", "task_%s" % ("resume" if active else "pause"), task_id)
        return self.get(task_id)

    def trigger_now(self, task_id: int) -> Dict[str, Any]:
        self.get(task_id)
        self.db.execute(
            "UPDATE tasks SET active = 1, next_run_at = ? WHERE id = ?",
            (time.time(), task_id),
        )
        self.audit.log("user", "task_run_now", task_id)
        return self.get(task_id)

    def delete(self, task_id: int) -> None:
        self.get(task_id)
        self.db.execute("DELETE FROM task_runs WHERE task_id = ?", (task_id,))
        self.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self.audit.log("user", "task_delete", task_id)

    def runs(self, task_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        return self.db.query(
            "SELECT id, task_id, started_at, finished_at, status, result "
            "FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (task_id, max(1, min(int(limit), 100))),
        )

    # -- runtime ----------------------------------------------------------
    async def start(self) -> None:
        if not self.enabled or self.runner is None:
            return
        if self._stop is None:
            self._stop = asyncio.Event()
        else:
            self._stop.clear()
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._recover()
        self._loop_task = asyncio.create_task(self._loop(), name="muse-scheduler")
        self.audit.log(
            "system",
            "scheduler_start",
            {"tick_seconds": self.tick_seconds, "max_concurrent": self.max_concurrent},
        )

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
            self._loop_task = None

    def _recover(self) -> None:
        """Called on boot: rebuild schedules and skip missed cron bursts."""
        now = time.time()
        self.db.execute(
            "UPDATE task_runs SET status = 'interrupted', finished_at = ?, "
            "result = COALESCE(result, 'Interrupted by restart.') "
            "WHERE status = 'running'",
            (now,),
        )
        for task in self.db.query("SELECT * FROM tasks WHERE active = 1"):
            try:
                if task["next_run_at"] is None:
                    next_run = (
                        now
                        if task["kind"] == "once"
                        else compute_next_run(
                            task["kind"], task["schedule"], task["timezone"]
                        )
                    )
                    self.db.execute(
                        "UPDATE tasks SET next_run_at = ? WHERE id = ?",
                        (next_run, task["id"]),
                    )
                elif task["next_run_at"] < now and task["kind"] != "once":
                    next_run = compute_next_run(
                        task["kind"], task["schedule"], task["timezone"], base_ts=now
                    )
                    self.db.execute(
                        "UPDATE tasks SET next_run_at = ? WHERE id = ?",
                        (next_run, task["id"]),
                    )
            except ScheduleError as exc:
                self.db.execute(
                    "UPDATE tasks SET active = 0, next_run_at = NULL WHERE id = ?",
                    (task["id"],),
                )
                self.audit.log(
                    "system",
                    "task_disabled",
                    {"id": task["id"], "error": str(exc)},
                    "error",
                )

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the scheduler alive
                self.audit.log("system", "scheduler_poll_error", str(exc), "error")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_seconds)
            except asyncio.TimeoutError:
                pass

    async def poll_once(self) -> List[int]:
        """Claim and launch every due task. Returns the task ids started."""
        if self.runner is None:
            return []
        now = time.time()
        due = self.db.query(
            "SELECT * FROM tasks WHERE active = 1 AND next_run_at IS NOT NULL "
            "AND next_run_at <= ? ORDER BY next_run_at LIMIT 10",
            (now,),
        )
        started: List[int] = []
        for task in due:
            if task["id"] in self._in_flight:
                continue
            if self._claim(task, now):
                started.append(int(task["id"]))
        return started

    def _claim(self, task: Dict[str, Any], now: float) -> bool:
        try:
            if task["kind"] == "once":
                next_run = None
                active = 0
            else:
                next_run = compute_next_run(
                    task["kind"], task["schedule"], task["timezone"], base_ts=now
                )
                active = 1
        except ScheduleError as exc:
            self.db.execute(
                "UPDATE tasks SET active = 0, next_run_at = NULL WHERE id = ?",
                (task["id"],),
            )
            self.audit.log(
                "system",
                "task_disabled",
                {"id": task["id"], "error": str(exc)},
                "error",
            )
            return False

        self.db.execute(
            "UPDATE tasks SET active = ?, next_run_at = ? WHERE id = ?",
            (active, next_run, task["id"]),
        )
        run_id = self.db.insert(
            "INSERT INTO task_runs (task_id, started_at, status) VALUES (?, ?, ?)",
            (task["id"], now, "running"),
        )
        self._in_flight.add(int(task["id"]))
        asyncio.create_task(self._execute(task, int(run_id)))
        return True

    async def _execute(self, task: Dict[str, Any], run_id: int) -> None:
        status = "error"
        result = ""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)
        async with self._semaphore:
            try:
                if self.runner is None:  # pragma: no cover - guarded by poll
                    raise RuntimeError("Scheduler has no runner.")
                result = str(await self.runner(task))
                status = "ok"
            except asyncio.CancelledError:
                result = "Cancelled."
                status = "cancelled"
                raise
            except Exception as exc:
                result = "Error: %s" % exc
                status = "error"
            finally:
                finished = time.time()
                self.db.execute(
                    "UPDATE task_runs SET finished_at = ?, status = ?, result = ? "
                    "WHERE id = ?",
                    (finished, status, result[:8000], run_id),
                )
                self.db.execute(
                    "UPDATE tasks SET last_run_at = ?, last_status = ?, "
                    "last_result = ?, run_count = run_count + 1 WHERE id = ?",
                    (finished, status, result[:4000], task["id"]),
                )
                self._in_flight.discard(int(task["id"]))
                self.audit.log(
                    "task:%s" % task["id"],
                    "task_run",
                    {"title": task["title"], "status": status},
                    status,
                )
        if self.notifier is not None and status != "cancelled":
            try:
                await self.notifier(task, status, result)
            except Exception as exc:  # delivery must never kill the scheduler
                self.audit.log(
                    "system",
                    "task_notify_error",
                    {"id": task["id"], "error": "%s: %s" % (type(exc).__name__, exc)},
                    "error",
                )
