"""Flexible, as-code report sections — the heart of the redesign.

Each ``Section`` declares its features, owned paths + char limits, croppable view, and
triggered tasks. The engine and store stay domain-neutral; everything specific lives in
a section module here.
"""

from __future__ import annotations

from .base import (
    AGE_BONUS,
    BASE_PRIORITY,
    Kind,
    OnEntry,
    OnFileOverflow,
    OnFinish,
    OnInit,
    OnLaunch,
    OnTimePass,
    RuleContext,
    Section,
    Task,
    TaskRule,
    Trigger,
    ViewBlock,
    ViewContext,
)
from .store import FileMeta, StoreError, VStore

__all__ = [
    "AGE_BONUS", "BASE_PRIORITY", "Kind", "OnEntry", "OnFileOverflow", "OnFinish",
    "OnInit", "OnLaunch", "OnTimePass", "RuleContext", "Section", "Task", "TaskRule",
    "Trigger", "ViewBlock", "ViewContext", "FileMeta", "StoreError", "VStore",
]
