"""Kamandal-owned launchd job registry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo


CENTRAL = ZoneInfo("America/Chicago")
LABEL_PREFIX = "com.kamandal.v2"


@dataclass(frozen=True)
class JobSchedule:
    fixed_times: tuple[time, ...] = ()
    cadence_minutes: int | None = None
    window_start: time | None = None
    window_end: time | None = None
    weekday: int | None = None


@dataclass(frozen=True)
class LaunchdJob:
    job: str
    label_suffix: str
    schedule: JobSchedule
    purpose: str
    script: str | None = None
    risk_class: str = "observed_external"
    available_actions: tuple[str, ...] = ()
    action_requirements: dict[str, dict[str, object]] | None = None

    @property
    def label(self) -> str:
        return f"{LABEL_PREFIX}.{self.label_suffix}"

    @property
    def schedule_label(self) -> str:
        return schedule_label(self.schedule)


SCRIPT_JOBS = {
    "x-bookmarks": "run_x_bookmark_extraction.sh",
    "youtube": "run_youtube_extraction.sh",
    "my-ideas": "run_my_ideas_import.sh",
    "live-reconciliation": "run_live_reconciliation.sh",
    "live-approved-orders": "run_live_approved_orders.sh",
    "unified-planning": "run_unified_planning.sh",
    "unified-lifecycle-management": "run_unified_lifecycle_management.sh",
    "earnings": "run_earnings_capture.sh",
    "iv": "run_iv_capture.sh",
    "iv-afternoon": "run_iv_capture.sh",
    "weekly-reviewer": "run_weekly_reviewer.sh",
}

JOB_LABEL_SUFFIXES = {
    "x-bookmarks": "x_bookmarks",
    "youtube": "youtube",
    "my-ideas": "my_ideas",
    "live-reconciliation": "live_reconciliation",
    "live-approved-orders": "live_approved_orders",
    "unified-planning": "unified_planning",
    "unified-lifecycle-management": "unified_lifecycle_management",
    "live-health-report": "live_health_report",
    "scheduled-job-health": "scheduled_job_health",
    "daily-report": "daily_report",
    "earnings": "earnings",
    "iv": "iv",
    "iv-afternoon": "iv_afternoon",
    "weekly-reviewer": "weekly_reviewer",
}

JOB_SCHEDULES = {
    "x-bookmarks": JobSchedule(fixed_times=(time(8, 15),)),
    "youtube": JobSchedule(fixed_times=(time(9, 15), time(11, 45), time(14, 0))),
    "my-ideas": JobSchedule(fixed_times=(time(8, 5), time(9, 20))),
    "live-reconciliation": JobSchedule(fixed_times=(time(8, 35), time(10, 30), time(12, 30), time(14, 10))),
    "unified-planning": JobSchedule(
        fixed_times=(time(8, 50), time(9, 25), time(11, 55), time(14, 15))
    ),
    "live-approved-orders": JobSchedule(cadence_minutes=5, window_start=time(8, 30), window_end=time(15, 15)),
    "unified-lifecycle-management": JobSchedule(
        cadence_minutes=5,
        window_start=time(8, 30),
        window_end=time(15, 15),
    ),
    "live-health-report": JobSchedule(fixed_times=(time(9, 10), time(11, 45), time(14, 45), time(15, 20))),
    "scheduled-job-health": JobSchedule(cadence_minutes=15, window_start=time(9, 15), window_end=time(15, 45)),
    "daily-report": JobSchedule(
        fixed_times=(time(9, 10), time(11, 45), time(14, 45), time(15, 25))
    ),
    "earnings": JobSchedule(fixed_times=(time(8, 40),)),
    "iv": JobSchedule(fixed_times=(time(8, 45),)),
    "iv-afternoon": JobSchedule(fixed_times=(time(13, 45),)),
    "weekly-reviewer": JobSchedule(fixed_times=(time(10, 0),), weekday=4),
}

JOB_PURPOSES = {
    "x-bookmarks": "Import X bookmarks into the idea pipeline.",
    "youtube": "Import YouTube/transcript intelligence.",
    "my-ideas": "Import operator ideas.",
    "live-reconciliation": "Reconcile local live groups against broker state.",
    "unified-planning": "Build isolated live and shadow portfolio books through one policy compiler.",
    "live-approved-orders": "Submit approved live entry intents.",
    "unified-lifecycle-management": "Evaluate all lifecycle management before guarded close execution.",
    "live-health-report": "Summarize live health and notify only when attention is needed.",
    "scheduled-job-health": "Watch Kamandal launchd logs for stale, missing, or failed runs.",
    "earnings": "Refresh earnings/event data.",
    "iv": "Refresh IV data.",
    "iv-afternoon": "Refresh afternoon IV data.",
    "weekly-reviewer": "Review rejected candidates and propose playbook tuning.",
    "daily-report": "Kamandal daily report and exact strategy-evidence packet; final passive run follows lifecycle management.",
}

DISABLED_BY_DEFAULT: set[str] = set()

JOB_RISK_CLASSES = {
    "live-reconciliation": "trading_observation",
    "unified-planning": "trading_advisory",
    "live-approved-orders": "trading_submit",
    "unified-lifecycle-management": "trading_management",
    "live-health-report": "trading_health",
    "scheduled-job-health": "ops_health",
}

JOB_AVAILABLE_ACTIONS = {
    "x-bookmarks": ("retry-job",),
    "youtube": ("retry-job",),
    "live-health-report": ("live-health-report-now",),
    "scheduled-job-health": ("scheduled-job-health-now",),
    "daily-report": ("daily-report-now",),
}

JOB_ACTION_REQUIREMENTS = {
    "x-bookmarks": {
        "retry-job": {
            "requires_confirmation": False,
            "reason": "Retry X bookmark intelligence ingestion. This does not submit, cancel, or modify broker orders.",
            "command_args": ["--job", "x-bookmarks"],
        }
    },
    "youtube": {
        "retry-job": {
            "requires_confirmation": False,
            "reason": "Retry YouTube/transcript intelligence ingestion. This does not submit, cancel, or modify broker orders.",
            "command_args": ["--job", "youtube"],
        }
    },
    "live-health-report": {
        "live-health-report-now": {
            "requires_confirmation": False,
            "reason": "Read-only health report; alert sending is controlled by alert mode.",
        }
    },
    "scheduled-job-health": {
        "scheduled-job-health-now": {
            "requires_confirmation": False,
            "reason": "Read-only scheduled job health check.",
        }
    },
}

MONITORED_JOBS = [
    "x-bookmarks",
    "youtube",
    "my-ideas",
    "live-reconciliation",
    "unified-planning",
    "live-approved-orders",
    "unified-lifecycle-management",
    "live-health-report",
    "earnings",
    "iv",
    "iv-afternoon",
    "weekly-reviewer",
    "daily-report",
]

ALL_JOBS = sorted([*SCRIPT_JOBS, "live-health-report", "scheduled-job-health", "daily-report"])


def launchd_jobs() -> list[LaunchdJob]:
    return [
        LaunchdJob(
            job=job,
            label_suffix=JOB_LABEL_SUFFIXES[job],
            schedule=JOB_SCHEDULES[job],
            purpose=JOB_PURPOSES[job],
            script=SCRIPT_JOBS.get(job),
            risk_class=JOB_RISK_CLASSES.get(job, "observed_external"),
            available_actions=JOB_AVAILABLE_ACTIONS.get(job, ()),
            action_requirements=JOB_ACTION_REQUIREMENTS.get(job),
        )
        for job in ALL_JOBS
    ]


def launchd_job(job: str) -> LaunchdJob:
    jobs = {item.job: item for item in launchd_jobs()}
    return jobs[job]


def schedule_label(schedule: JobSchedule) -> str:
    if schedule.weekday is not None and schedule.fixed_times:
        return "Fridays " + ", ".join(_fmt_time(item) for item in schedule.fixed_times) + " CT"
    if schedule.cadence_minutes and schedule.window_start and schedule.window_end:
        label = (
            f"Weekdays every {schedule.cadence_minutes} minutes, "
            f"{_fmt_time(schedule.window_start)}-{_fmt_time(schedule.window_end)} CT"
        )
        if schedule.fixed_times:
            label += "; plus " + ", ".join(_fmt_time(item) for item in schedule.fixed_times) + " CT"
        return label
    if schedule.fixed_times:
        return "Weekdays " + ", ".join(_fmt_time(item) for item in schedule.fixed_times) + " CT"
    return "unscheduled"


def _fmt_time(value: time) -> str:
    return value.strftime("%H:%M")
