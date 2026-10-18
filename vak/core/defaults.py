"""All tunable values in one place, with the spec they come from.

Values marked "suggested" in the specs have not been measured on any
product; "practice" values come from one product's traces. Override per
agent in agent.yaml under `timers:` / `arbiter:` / `tools:` / `context:`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class TurnTimers:
    """Spec 01 §4. Sent to the client in the hello reply."""

    T_hold: int = 600  # suggested; cap 1000
    T_hold_max: int = 1000
    T_listen_max_A: int = 30_000  # screenless hardware
    T_listen_max_B: int = 60_000  # app with screen
    T_asr: int = 3_000
    T_filler_A: int = 2_000
    T_filler_B: int = 2_500
    T_respond: int = 8_000
    T_drain_extra: int = 2_000  # remaining unplayed + 2 s
    T_intr_receipt: int = 300
    T_link_client: int = 15_000  # practice
    T_write: int = 10_000
    T_hello: int = 10_000
    ping_interval: int = 5_000
    ping_miss_limit: int = 3
    resume_window: int = 30_000
    resume_window_background_B: int = 120_000
    lead_ms: int = 300  # downlink lead, 200–500
    play_buffer_max_ms: int = 1_200
    preroll_ms: int = 250
    short_press_ms: int = 250
    short_speech_ms: int = 150
    uninterruptible_max_ms: int = 3_000
    echo_tail_ms: int = 200  # 150–300
    aec_warmup_ms: int = 3_000
    barge_in_min_ms: int = 500
    false_interruption_timeout_ms: int = 2_000
    uplink_ring_ms: int = 2_000
    event_retry_base_ms: int = 2_000
    cmd_timeout_ms: int = 3_000
    plc_gap_max_ms: int = 100  # ≤ 60–100 ms: Opus PLC
    damaged_gap_max_ms: int = 1_000  # 100 ms–1 s: audio_damaged; > 1 s listening: failed{audio_gap}


@dataclass(slots=True)
class EndpointingParams:
    """Spec 01 §2.3 / handbook turn-detection. Starting points, scan on your own data."""

    vad_silence_ms: int = 250  # 200–300
    min_endpoint_ms: int = 500
    max_endpoint_ms: int = 3_000
    semantic_threshold: float = 0.6  # P(done) ≥ τ → commit
    supplement_window_ms: int = 600  # ptt holding when P(done) < τ


@dataclass(slots=True)
class ArbiterParams:
    """Spec 02 §4."""

    queue_max_A: int = 32
    queue_max_B: int = 16
    narration_window_silence_ms: int = 350
    narration_wakeup_ms: int = 1_000
    started_receipt_timeout_ms: int = 5_000  # after send complete
    ended_receipt_extra_ms: int = 2_000  # remaining unplayed + 2 s (= T_drain)
    flush_wait_max_ms: int = 1_000
    max_retries_result: int = 8
    max_retries_progress: int = 0
    claim_ttl_ms: int = 60_000
    closed_request_ids_keep: int = 256
    progress_first_after_s: int = 60
    progress_min_interval_s: int = 60
    progress_quiet_ms: int = 2_000
    progress_quiet_max_ms: int = 10_000


@dataclass(slots=True)
class ToolParams:
    """Spec 03 §4."""

    timeout_ms_default: dict[str, int] = field(
        default_factory=lambda: {"ms": 1_000, "seconds": 15_000, "background": 600_000}
    )
    watchdog_margin_ms: int = 2_000
    cue_short_max_ms: int = 300
    cue_long_ms: int = 760
    recent_turns_k: int = 4
    output_max_chars: int = 500
    alternatives_max: int = 2


@dataclass(slots=True)
class ContextParams:
    """Spec 05 §4."""

    state_block_max_chars: int = 260
    upstream_turns_retained: int = 20  # practice, one upstream
    turn_limit_threshold: int = 12  # 8 / 12 / 16, needs measurement
    idle_gap_ms: int = 1_000
    idle_wait_max_ms: int = 60_000
    handoff_buffer_max_ms: int = 10_000
    summary_trigger_turns_before_limit: int = 6
    recent_turns_k: int = 5  # 4–6
    snapshot_recent_turns: int = 12
    modality_anomaly_rebuild_after: int = 2
    rebuild_budget_per_turn: int = 3
    rebuild_backoff_first_ms: int = 500
    rebuild_backoff_cap_ms: int = 10_000
    rebuild_stable_reset_ms: int = 10_000
    recovery_window_ms: int = 30 * 60_000
    rebuild_cue_within_ms: int = 2_000


@dataclass(slots=True)
class TraceParams:
    """Spec 06 §4."""

    silence_window_ms: int = 10
    silence_threshold_dbfs: float = -45.0
    finalize_wait_ms: int = 5_000
    stuck_silence_ms: int = 2_500
    budget_sound_first_ms_A: int = 900
    budget_sound_first_ms_B: int = 1_000
    budget_content_first_ms: dict[str, int] = field(
        default_factory=lambda: {
            "chat": 1_000,
            "tool_verbatim": 1_100,
            "tool_compose": 1_300,
            "tool_compose_B": 1_500,
            "tool_write": 2_000,
        }
    )
    budget_cue_to_content_gap_ms: int = 1_500
    min_n_p50: int = 20
    min_n_p90: int = 50


@dataclass(slots=True)
class UpstreamTimeouts:
    """Spec 04 §4 (primitive-level, used by pipelines)."""

    open_connect_ms: int = 10_000
    open_item_ack_ms: int = 5_000
    open_ready_wait_ms: int = 500
    commit_ack_ms: int = 2_000
    stt_final_ms: int = 1_500
    response_started_ms: int = 2_000
    first_delta_ms: int = 3_000
    cancel_ack_ms: int = 1_000
    truncate_ack_ms: int = 2_000
    update_config_ack_ms: int = 3_000
    tool_result_ack_ms: int = 5_000
    close_grace_ms: int = 2_000
    guardrail_budget_ms: int = 50
    warmup_interval_ms: int = 30_000  # at most one LLM/TTS pre-connect per interval (handbook first-audio §4)
    tts_lookahead: int = 1  # sentences synthesised ahead of the one being emitted (handbook first-audio §4)


@dataclass(slots=True)
class Defaults:
    turn: TurnTimers = field(default_factory=TurnTimers)
    endpointing: EndpointingParams = field(default_factory=EndpointingParams)
    arbiter: ArbiterParams = field(default_factory=ArbiterParams)
    tools: ToolParams = field(default_factory=ToolParams)
    context: ContextParams = field(default_factory=ContextParams)
    trace: TraceParams = field(default_factory=TraceParams)
    upstream: UpstreamTimeouts = field(default_factory=UpstreamTimeouts)


DEFAULTS = Defaults()
