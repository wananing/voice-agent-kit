"""Per-turn trace collector implementing `TracePort` (spec 06 §3.1, §7).

Components write fragments with ABSOLUTE gateway-clock ms in every
`*_at_ms` / `at_ms` field; the turn machine writes `t0_ms` (absolute). At
finalize the collector subtracts T0, sets `t0_epoch_ms`, derives the
spec-mandated segments, checks invariants, null-fills missing required
fields, validates against the JSON Schema and exports.

Finalize is triggered by bus `turn.closed{turn_id}`; it waits up to
`params.finalize_wait_ms` for `mark_complete(turn_id, "usage")` and
`mark_complete(turn_id, "receipt")` (spec 06 §3.1 "收尾时机").
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from vak.core.bus import Bus
from vak.core.clock import Clock
from vak.core.defaults import TraceParams
from vak.core.trace.exporter import Exporter
from vak.core.trace.schema import TRACE_SCHEMA_VERSION, allowed_keys, missing_required, validate
from vak.core.types import Profile, TurnId
from vak.upstream.base import Usage

log = logging.getLogger(__name__)

COMPLETION_SIGNALS = frozenset({"usage", "receipt"})
T0_KEY = "t0_ms"  # written by the turn machine, absolute gateway ms; stripped at finalize


class UnknownTraceField(KeyError):
    """Raised by `write` / `append` / `incr` for a key not in the schema."""


@dataclass(slots=True)
class _Turn:
    record: dict[str, Any]
    first_write_ms: int
    closed_at_ms: int | None = None
    completed: set[str] = field(default_factory=set)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    finalizing: bool = False


# spec 06 §2.4: segments measured from T0 that writers record as absolute gateway time
_FROM_T0 = frozenset({"pre_tool_ms"})


def _is_time_point(key: str) -> bool:
    return key == "at_ms" or key.endswith("_at_ms") or key in _FROM_T0


def _rebase(value: Any, t0: int, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: _rebase(v, t0, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_rebase(v, t0, key) for v in value]
    if _is_time_point(key) and isinstance(value, int) and not isinstance(value, bool):
        return value - t0
    return value


def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


class TraceCollector:
    """One instance per connection session (architecture §4.4)."""

    def __init__(
        self,
        session_id: str,
        user_session_id: str,
        profile: Profile | str,
        pipeline: str,
        provider: str,
        model: str,
        prompt_version_fn: Callable[[], str],
        gateway_build: str,
        clock: Clock,
        params: TraceParams,
        exporter: Exporter,
        bus: Bus,
        device_id: str | None = None,
        user_id_hash: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.user_session_id = user_session_id
        self.profile = profile.value if isinstance(profile, Profile) else str(profile)
        self.pipeline = pipeline
        self.provider = provider
        self.model = model
        self.prompt_version_fn = prompt_version_fn
        self.gateway_build = gateway_build
        self.clock = clock
        self.params = params
        self.exporter = exporter
        self.bus = bus
        self.device_id = device_id
        self.user_id_hash = user_id_hash
        self._turns: dict[str, _Turn] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        bus.on("turn.closed", self._on_turn_closed)

    # --- TracePort -----------------------------------------------------------

    def now_ms(self) -> int:
        return self.clock.now_ms()

    def write(self, turn_id: TurnId | str, fragment: dict[str, Any]) -> None:
        self._check_keys(fragment, ())
        turn = self._turn(turn_id)
        _deep_merge(turn.record, fragment)

    def append(self, turn_id: TurnId | str, list_field: str, item: dict[str, Any]) -> None:
        path = tuple(list_field.split("."))
        self._check_path(path)
        self._check_keys(item, path)
        parent = self._container(self._turn(turn_id).record, path[:-1])
        lst = parent.get(path[-1])
        if lst is None:
            lst = parent[path[-1]] = []
        if not isinstance(lst, list):
            raise TypeError(f"{list_field} is not a list")
        lst.append(item)

    def incr(self, turn_id: TurnId | str, counter: str, by: int = 1) -> None:
        path = tuple(counter.split("."))
        self._check_path(path)
        parent = self._container(self._turn(turn_id).record, path[:-1])
        cur = parent.get(path[-1])
        parent[path[-1]] = (cur if isinstance(cur, int) else 0) + by

    # --- signals ---------------------------------------------------------------

    def mark_complete(self, turn_id: TurnId | str, what: str) -> None:
        """Arbiter signals "receipt" (last playback receipt or receipt timeout);
        upstream signals "usage". Finalize proceeds once both arrived or the wait expires."""
        if what not in COMPLETION_SIGNALS:
            raise ValueError(f"unknown completion signal {what!r}; expected one of {sorted(COMPLETION_SIGNALS)}")
        turn = self._turn(turn_id)
        turn.completed.add(what)
        if turn.closed_at_ms is not None and turn.completed >= COMPLETION_SIGNALS:
            turn.done.set()

    def on_usage(
        self, turn_id: TurnId | str, usage: Usage, interrupted: bool, response_status: str | None = None
    ) -> None:
        """spec 06 §3.10: one `usage.responses[]` item per upstream response;
        interrupted responses are recorded too. Marks "usage" complete."""
        item = {
            "response_id": usage.response_id,
            "response_status": response_status,
            "interrupted": interrupted,
            "input_text_tokens": usage.input_text_tokens,
            "input_audio_tokens": usage.input_audio_tokens,
            "output_text_tokens": usage.output_text_tokens,
            "output_audio_tokens": usage.output_audio_tokens,
        }
        for k in ("cached_input_tokens", "cached_text_tokens", "cached_audio_tokens"):
            v = getattr(usage, k)
            if v is not None:
                item[k] = v
        self.append(turn_id, "usage.responses", item)
        self.write(turn_id, {"usage": {"usage_semantics": usage.usage_semantics}})
        if usage.generated_audio_ms is not None:
            self.incr(turn_id, "usage.generated_audio_ms", usage.generated_audio_ms)
        self.mark_complete(turn_id, "usage")

    # --- lifecycle ------------------------------------------------------------

    def open_turns(self) -> list[str]:
        return list(self._turns)

    def peek(self, turn_id: TurnId | str) -> dict[str, Any] | None:
        t = self._turns.get(str(turn_id))
        return dict(t.record) if t else None

    async def close(self) -> None:
        """Session end: finalize every open turn without waiting."""
        for tid in list(self._turns):
            self._finalize(tid)
        for task in list(self._tasks):
            task.cancel()

    async def _on_turn_closed(self, payload: dict[str, Any]) -> None:
        turn_id = payload.get("turn_id")
        if turn_id is None:
            return
        turn = self._turn(turn_id)
        turn.closed_at_ms = self.clock.now_ms()
        if turn.completed >= COMPLETION_SIGNALS:
            turn.done.set()
        loop = asyncio.get_running_loop()
        # start the deadline now so the wait is measured from turn.closed, not from the task's first run
        sleeper = loop.create_task(self.clock.sleep(self.params.finalize_wait_ms))
        task = loop.create_task(self._wait_then_finalize(str(turn_id), turn, sleeper))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _wait_then_finalize(self, tid: str, turn: _Turn, sleeper: asyncio.Task[None]) -> None:
        # let the other turn.closed handlers (same tick) write their final fragments first
        await asyncio.sleep(0)
        if not turn.done.is_set() and self.params.finalize_wait_ms > 0:
            waiter = asyncio.ensure_future(turn.done.wait())
            try:
                await asyncio.wait({waiter, sleeper}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                if not waiter.done():
                    waiter.cancel()
        if not sleeper.done():
            sleeper.cancel()
        self._finalize(tid)

    # --- finalize ---------------------------------------------------------------

    def _finalize(self, tid: str) -> dict[str, Any] | None:
        turn = self._turns.pop(tid, None)
        if turn is None or turn.finalizing:
            return None
        turn.finalizing = True
        rec = turn.record
        violations: list[str] = []

        t0 = _int(rec.pop(T0_KEY, None))
        if t0 is None:
            t0 = turn.first_write_ms
            violations.append("t0_ms missing; used first write time as T0")
        rec = _rebase(rec, t0)
        rec["t0_epoch_ms"] = self.clock.epoch_ms() - (self.clock.now_ms() - t0)

        self._derive(rec, turn, t0)
        self._rollup_usage(rec)
        violations.extend(self._invariants(rec))

        missing: list[str] = []
        for _ in range(4):  # nested requireds appear once their parent exists
            found = missing_required(rec)
            if not found:
                break
            for parent, key in found:
                container = self._container(rec, parent)
                if key not in container:
                    container[key] = None
                    missing.append(".".join([*map(str, parent), key]))
        if missing:
            rec["missing_fields"] = sorted(set(missing))

        schema_errors = validate(rec)
        for e in schema_errors:
            violations.append(f"schema: {e}")
        if violations:
            rec["invariant_violations"] = violations
            log.warning("trace %s/%s: %s", self.session_id, tid, "; ".join(violations))
        try:
            self.exporter.export(rec)
        except Exception:  # noqa: BLE001
            log.exception("trace export failed for %s/%s", self.session_id, tid)
        return rec

    def _derive(self, rec: dict[str, Any], turn: _Turn, t0: int) -> None:
        """spec 06 §2.2, §2.4 derived quantities; never overwrite a value a component wrote."""
        anchors = rec.setdefault("anchors", {})
        lat = rec.setdefault("latency", {})
        raw_calls = [c for c in (rec.get("tool_calls") or []) if isinstance(c, dict)]
        if raw_calls:
            # the tool layer appends a fresh record when a settled call is relabelled
            # (spec 03 §2.7 row 4) or a late result is counted: last record per call_id wins
            latest: dict[str, dict[str, Any]] = {}
            for c in raw_calls:
                latest[str(c.get("call_id"))] = c
            rec["tool_calls"] = list(latest.values())
        calls = list(rec.get("tool_calls") or [])
        outputs = [o for o in (rec.get("outputs") or []) if isinstance(o, dict)]

        def put(d: dict[str, Any], key: str, value: Any) -> None:
            if d.get(key) is None and value is not None:
                d[key] = value

        ae, rel, ep = _int(anchors.get("audio_end_at_ms")), _int(anchors.get("release_at_ms")), _int(
            anchors.get("endpoint_at_ms")
        )
        if ae is not None and rel is not None:
            put(lat, "ptt_tail_silence_ms", rel - ae)  # §2.2 T_rel - T_ae
        if ae is not None and ep is not None:
            put(lat, "endpoint_ms", ep - ae)  # §2.2 T_ep - T_ae

        sf, cf = _int(lat.get("sound_first_at_ms")), _int(lat.get("content_first_at_ms"))
        ready = _int(lat.get("content_first_ready_at_ms"))
        if cf is not None and ready is not None:
            put(lat, "queue_wait_ms", cf - ready)  # §2.3 rule 1

        if calls:
            starts = [s for s in (_int(c.get("started_at_ms")) for c in calls) if s is not None]
            if starts:
                put(lat, "pre_tool_ms", min(starts))  # §2.4: T0 -> first tool starts executing
            if lat.get("tool_done_at_ms") is None:
                dones = [d for d in (_int(c.get("done_at_ms")) for c in calls) if d is not None]
                if cf is not None:
                    dones = [d for d in dones if d <= cf] or dones
                if dones:
                    lat["tool_done_at_ms"] = max(dones)  # §2.4: last call done before content
            td = _int(lat.get("tool_done_at_ms"))
            if td is not None and cf is not None:
                put(lat, "post_tool_ms", cf - td)

        fillers = sorted(
            (o for o in outputs if o.get("content_role") == "filler" and _int(o.get("sent_at_ms")) is not None),
            key=lambda o: o["sent_at_ms"],
        )
        if fillers and cf is not None and _int(fillers[0].get("played_ms")) is not None:
            put(lat, "cue_to_content_gap_ms", cf - (fillers[0]["sent_at_ms"] + fillers[0]["played_ms"]))

        if lat.get("content_kind") is None:
            content = sorted(
                (o for o in outputs if o.get("content_role") in ("answer", "progress")
                 and _int(o.get("sent_at_ms")) is not None),
                key=lambda o: o["sent_at_ms"],
            )
            lat["content_kind"] = content[0]["content_role"] if content and cf is not None else "none"

        # §3.9 stuck: no terminal, or silence beyond T_filler + 500 ms with no filler at all
        terminal = rec.get("turn_terminal")
        closed_rel = (turn.closed_at_ms - t0) if turn.closed_at_ms is not None else None
        silence = sf if sf is not None else closed_rel
        no_filler = not fillers
        stuck = terminal is None or (
            terminal in ("completed", "interrupted", "failed")
            and silence is not None
            and silence > self.params.stuck_silence_ms
            and no_filler
        )
        rec["stuck"] = bool(rec.get("stuck")) or stuck

    @staticmethod
    def _rollup_usage(rec: dict[str, Any]) -> None:
        """spec 06 §3.6: turn-level cached / uncached input tokens summed from responses."""
        usage = rec.get("usage") or {}
        responses = [r for r in (usage.get("responses") or []) if isinstance(r, dict)]
        cached = [r["cached_input_tokens"] for r in responses if _int(r.get("cached_input_tokens")) is not None]
        if not cached or rec.get("cached_input_tokens") is not None:
            return
        total_in = sum((r.get("input_text_tokens") or 0) + (r.get("input_audio_tokens") or 0) for r in responses)
        rec["cached_input_tokens"] = sum(cached)
        if usage.get("usage_semantics") == "gross":
            rec["uncached_input_tokens"] = max(0, total_in - sum(cached))
        else:
            rec["uncached_input_tokens"] = total_in

    @staticmethod
    def _invariants(rec: dict[str, Any]) -> list[str]:
        out: list[str] = []
        lat = rec.get("latency") or {}
        anchors = rec.get("anchors") or {}
        sf, cf = _int(lat.get("sound_first_at_ms")), _int(lat.get("content_first_at_ms"))
        if sf is not None and cf is not None and cf < sf:
            out.append(f"content_first_at_ms({cf}) < sound_first_at_ms({sf})")  # §2.3
        if lat.get("content_kind") == "none" and cf is not None:
            out.append("content_kind=none but content_first_at_ms is not null")
        primary = anchors.get("primary")
        if primary is not None:
            v = _int(anchors.get(f"{primary}_at_ms"))
            if v is None:
                out.append(f"anchors.{primary}_at_ms missing for primary={primary}")
            elif v != 0:
                out.append(f"anchors.{primary}_at_ms={v}, expected 0 (T0)")
        hanging = [
            str(c.get("call_id"))
            for c in (rec.get("tool_calls") or [])
            if isinstance(c, dict) and (c.get("closed_by") is None or _int(c.get("settled_at_ms")) is None)
        ]
        if hanging:
            out.append("hanging_calls: " + ",".join(hanging))  # §3.5 rule 6
        return out

    # --- helpers ------------------------------------------------------------------

    def _turn(self, turn_id: TurnId | str) -> _Turn:
        tid = str(turn_id)
        turn = self._turns.get(tid)
        if turn is None:
            now = self.clock.now_ms()
            record: dict[str, Any] = {
                # spec 06 §3.2, gateway-owned identity fields
                "trace_schema_version": TRACE_SCHEMA_VERSION,
                "profile": self.profile,
                "session_id": self.session_id,
                "user_session_id": self.user_session_id,
                "turn_id": tid,
                "pipeline": self.pipeline,
                "provider": self.provider,
                "model": self.model,
                "prompt_version": self.prompt_version_fn(),
                "gateway_build": self.gateway_build,
                "anchors": {},
                "latency": {},
                "usage": {},
            }
            if self.device_id is not None:
                record["device_id"] = self.device_id
            if self.user_id_hash is not None:
                record["user_id_hash"] = self.user_id_hash
            turn = self._turns[tid] = _Turn(record=record, first_write_ms=now)
        return turn

    @staticmethod
    def _container(record: dict[str, Any], path: tuple[Any, ...]) -> dict[str, Any]:
        cur: Any = record
        for p in path:
            if isinstance(cur, list):
                cur = cur[int(p)]
            else:
                nxt = cur.get(p)
                if nxt is None:
                    nxt = cur[p] = {}
                cur = nxt
        return cur

    @staticmethod
    def _check_path(path: tuple[str, ...]) -> None:
        for i in range(len(path)):
            allowed = allowed_keys(path[:i])
            if allowed is not None and path[i] not in allowed:
                raise UnknownTraceField(".".join(path[: i + 1]))

    @classmethod
    def _check_keys(cls, fragment: dict[str, Any], path: tuple[str, ...]) -> None:
        allowed = allowed_keys(path)
        if allowed is None:
            return  # free-form object (slots, args, entity_resolution)
        for k, v in fragment.items():
            if k == T0_KEY and not path:
                continue
            if k not in allowed:
                raise UnknownTraceField(".".join([*path, k]))
            if isinstance(v, dict):
                cls._check_keys(v, (*path, k))
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        cls._check_keys(item, (*path, k))
