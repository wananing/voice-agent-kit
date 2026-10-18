"""Context and session (spec 05): local record, state block, persona,
history projection, session rebuilder, summariser, modality monitor,
snapshot."""

from vak.core.context.modality import ModalityMonitor
from vak.core.context.persona import check_persona, load_persona, render_persona
from vak.core.context.projection import INTERRUPT_MARKER, ProjectionError, project_history
from vak.core.context.rebuilder import (
    IMMEDIATE_REASONS,
    PROACTIVE_REASONS,
    DeliveredRegistry,
    RebuildResult,
    SessionRebuilder,
    backoff_ms,
)
from vak.core.context.snapshot import Snapshot, SnapshotStore, apply_snapshot, take_snapshot
from vak.core.context.state_block import StateBlockStore, StateSchema, choose_injection_channel
from vak.core.context.store import ContextStore
from vak.core.context.summary import Summarizer

__all__ = [
    "IMMEDIATE_REASONS",
    "INTERRUPT_MARKER",
    "PROACTIVE_REASONS",
    "ContextStore",
    "DeliveredRegistry",
    "ModalityMonitor",
    "ProjectionError",
    "RebuildResult",
    "SessionRebuilder",
    "Snapshot",
    "SnapshotStore",
    "StateBlockStore",
    "StateSchema",
    "Summarizer",
    "apply_snapshot",
    "backoff_ms",
    "check_persona",
    "choose_injection_channel",
    "load_persona",
    "project_history",
    "render_persona",
    "take_snapshot",
]
