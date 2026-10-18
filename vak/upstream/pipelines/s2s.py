"""S2S pipeline: one `RealtimeS2S` provider behind the nine primitives, with
every degradation decided by the capability declaration (spec 04 §3).

The provider does protocol encode/decode and emits standardised events;
this class applies the §2.3 degradation table, fills `turn_id`,
`response_ref` and `origin` from its own bookkeeping, keeps the mirror,
re-classifies errors through `errors.code_map`, and reports every
confirmation timeout as an event. It never retries, reconnects or
recycles the session.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import asdict
from typing import Any, Literal

from vak.audio.silence import duration_ms
from vak.core.clock import Clock
from vak.core.defaults import UpstreamTimeouts
from vak.core.ids import IdGen
from vak.core.types import ErrorClass, ErrorScope, HistoryFormat, ReplyModality, TurnId
from vak.upstream.base import (
    Ack,
    AudioMeta,
    ConfigPatch,
    EventType,
    ResponseOrigin,
    ResponseOverrides,
    ResponseStatus,
    SessionInit,
    UpstreamEvent,
)
from vak.upstream.capabilities import Capabilities
from vak.upstream.pipelines.base import TIMEOUT, PipelineBase
from vak.upstream.providers.base import RealtimeS2S

log = logging.getLogger(__name__)

_Pred = Callable[[UpstreamEvent], bool]
_LIVE = ("live_full", "live_sparse")


class S2SUpstream(PipelineBase):
    """`S2SUpstream(provider, caps, clock, timeouts, ids)`."""

    def __init__(
        self,
        provider: RealtimeS2S,
        caps: Capabilities,
        clock: Clock | None = None,
        timeouts: UpstreamTimeouts | None = None,
        ids: IdGen | None = None,
    ) -> None:
        super().__init__(caps, clock, timeouts, ids)
        self._provider = provider
        self._pump: asyncio.Task[None] | None = None
        self._waiters: list[tuple[_Pred, asyncio.Future[UpstreamEvent]]] = []
        self._turn: TurnId | None = None
        self._turn_ms: dict[str, int] = {}
        self._seqs: dict[str, set[int]] = {}
        self._turns: dict[str, tuple[str, Ack]] = {}
        self._refs: dict[str, str] = {}
        self._pending_ref: tuple[str, TurnId | None] | None = None
        self._expect_auto_tool = False
        self._watch: dict[str, asyncio.Task[None]] = {}
        self._truncated: dict[str, int] = {}
        self._config: dict[str, Any] = {}
        self._config_version: str | None = None
        self.resume_handle: str | None = None

    # -- provider event pump ----------------------------------------------------

    async def _run_pump(self) -> None:
        try:
            async for ev in self._provider.events():
                self._on_provider_event(ev)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the stream itself broke: report once, do not reconnect
            err = self._errors.map(type(exc).__name__, str(exc), scope=ErrorScope.SESSION)
            self._emit(self._emitter.error(err))

    def _on_provider_event(self, ev: UpstreamEvent) -> None:
        if ev.turn_id is None:
            ev.turn_id = self._turn
        t = ev.type
        if t is not EventType.RESPONSE_STARTED and ev.response_id is not None and ev.response_ref is None:
            ev.response_ref = self._ref_of(ev.response_id)
        if t is EventType.RESPONSE_STARTED and ev.response_id is not None:
            self._on_started(ev)
        elif t is EventType.RESPONSE_DONE and ev.response_id is not None:
            self._stop_watch(ev.response_id)
            if not self._tracker.done(ev):
                log.debug("response.done for unknown/finished %s dropped", ev.response_id)
        elif t in (
            EventType.RESPONSE_TEXT_DELTA,
            EventType.RESPONSE_AUDIO_DELTA,
            EventType.RESPONSE_SENTENCE,
            EventType.TRANSCRIPT_ASSISTANT,
        ):
            if ev.response_id is not None:
                self._stop_watch(ev.response_id)
            self._emit(ev)
        elif t is EventType.TOOL_CALL:
            ev.before_commit = ev.turn_id is not None and str(ev.turn_id) not in self._turns
            self._emit(ev)
        elif t is EventType.ERROR and ev.error is not None:
            raw = ev.error
            if raw.raw_code is not None or raw.raw_message is not None:
                ev.error = self._errors.map(
                    raw.raw_code, raw.raw_message, scope=raw.scope, ref=raw.ref, session_open=self._open
                )
            self._emit(ev)
        elif t is EventType.SESSION_RESUMABLE:
            self.resume_handle = ev.handle
            self._emit(ev)
        elif t is EventType.SESSION_CLOSED:
            self._cancel_all_active(ev.reason or "server_closed")
            if ev.source is None:
                ev.source = "server"
            self._emit(ev)
        else:
            self._emit(ev)
        self._resolve_waiters(ev)

    def _on_started(self, ev: UpstreamEvent) -> None:
        rid = ev.response_id
        assert rid is not None
        if ev.origin is None:
            if self._pending_ref is not None:
                ev.origin = ResponseOrigin.CLIENT
            elif self._expect_auto_tool:
                ev.origin = ResponseOrigin.AUTO_TOOL
            else:
                ev.origin = ResponseOrigin.SERVER_VAD
        if ev.origin is ResponseOrigin.CLIENT and self._pending_ref is not None and ev.response_ref is None:
            ref, turn_id = self._pending_ref
            self._pending_ref = None
            ev.response_ref = ref
            if ev.turn_id is None:
                ev.turn_id = turn_id
            self._refs[ref] = rid
        if ev.origin is ResponseOrigin.AUTO_TOOL:
            self._expect_auto_tool = False
        if self._tracker.start(ev):
            self._watch[rid] = asyncio.create_task(self._first_delta_watch(rid, ev.turn_id))

    async def _first_delta_watch(self, rid: str, turn_id: TurnId | None) -> None:
        await self.clock.sleep(self.timeouts.first_delta_ms)
        if self._tracker.is_active(rid):
            self._emit_error(ErrorClass.TRANSIENT, "response_timeout", ErrorScope.RESPONSE, turn_id=turn_id, response_id=rid)

    def _ref_of(self, rid: str) -> str | None:
        return next((ref for ref, i in self._refs.items() if i == rid), None)

    def _stop_watch(self, rid: str) -> None:
        w = self._watch.pop(rid, None)
        if w is not None:
            w.cancel()

    # -- confirmation waiting ---------------------------------------------------

    def _expect(self, pred: _Pred) -> asyncio.Future[UpstreamEvent]:
        fut: asyncio.Future[UpstreamEvent] = asyncio.get_running_loop().create_future()
        self._waiters.append((pred, fut))
        return fut

    def _resolve_waiters(self, ev: UpstreamEvent) -> None:
        for pred, fut in list(self._waiters):
            if fut.done():
                self._waiters.remove((pred, fut))
            elif pred(ev):
                fut.set_result(ev)
                self._waiters.remove((pred, fut))

    async def _wait(
        self,
        fut: asyncio.Future[UpstreamEvent],
        ms: int,
        detail: str,
        scope: ErrorScope,
        *,
        error_class: ErrorClass = ErrorClass.TRANSIENT,
        turn_id: TurnId | None = None,
        response_id: str | None = None,
        item_id: str | None = None,
        call_id: str | None = None,
    ) -> UpstreamEvent | Any:
        res = await self._with_timeout(
            fut, ms, detail, scope, error_class=error_class, turn_id=turn_id,
            response_id=response_id, item_id=item_id, call_id=call_id,
        )
        self._waiters = [(p, f) for p, f in self._waiters if f is not fut]
        return res

    # -- open ----------------------------------------------------------------------

    async def open(self, init: SessionInit) -> Ack:
        async with self._lock:
            if self._closed:
                return Ack.invalid_state("closed")
            if self._open:
                return Ack.invalid_state("already_open")
            caps = self.capabilities
            if init.resume_handle is not None and caps.get("session.resumption") != "handle":
                return Ack.unsupported("session.resumption")
            if (bad := self._check_history(init)) is not None:
                return bad
            injection = caps.get("history.injection") or set()
            mechanism = "init_only" if "init_only" in injection else (sorted(injection)[0] if injection else None)
            self._config = {
                "instructions": init.instructions,
                "tools": [asdict(t) for t in init.tools],
                "voice": init.voice,
                "language": init.language,
                "output_default": init.output_default,
            }
            config = {
                **self._config,
                "user_session_id": init.user_session_id,
                "session_seq": init.session_seq,
                "state_block_version": init.state_block_version,
                "history": [asdict(h) for h in init.history],
                "history_format": init.history_format.value,
                "history_injection": mechanism,
                "resume_handle": init.resume_handle,
            }
            if init.turn_detection != "default":
                # ptt / local endpointing: ask the vendor to turn server detection off (spec 04 P0-2)
                config["turn_detection"] = init.turn_detection
            self._config_version = init.state_block_version
            res = await self._with_timeout(
                self._provider.connect(config), self.timeouts.open_connect_ms, "open_timeout", ErrorScope.SESSION
            )
            if res is TIMEOUT:
                await self._safe_disconnect()
                return Ack.timeout("open_connect")
            self._mirror.reset()
            self._mirror.seed([h.local_entry_id for h in init.history], [h.role for h in init.history])
            self.session_id = self.ids.new("us")
            self._emitter.session_id = self.session_id
            self._open = True
            opened = self._expect(lambda e: e.type is EventType.SESSION_OPENED)
            self._pump = asyncio.create_task(self._run_pump())
            effective = {"history_items_sent": len(init.history), "tools_sent": len(init.tools)}
            if caps.get("session.init_ready_signal") == "ack_event":
                ev = await self._wait(opened, self.timeouts.open_item_ack_ms, "open_timeout", ErrorScope.SESSION)
                if ev is TIMEOUT:
                    return await self._abort_open("open_ready")
            else:
                opened.cancel()
                await self.clock.sleep(self.timeouts.open_ready_wait_ms)
                self._emit(self._emitter.session_opened(self.session_id, effective))
            if init.history and caps.get("history.item_ack") == "yes":
                all_ok = self._expect(lambda _e: all(it.confirmed for it in self._mirror.items if it.seeded))
                if all(it.confirmed for it in self._mirror.items if it.seeded):
                    all_ok.cancel()
                else:
                    ev = await self._wait(all_ok, self.timeouts.open_item_ack_ms, "open_timeout", ErrorScope.SESSION)
                    if ev is TIMEOUT:
                        return await self._abort_open("open_item_ack")
            return Ack.success(session_id=self.session_id, effective=effective)

    def _check_history(self, init: SessionInit) -> Ack | None:
        """Spec 04 §3.1 degradation: validate, never fix (no trimming, no filler items)."""
        caps = self.capabilities
        items = init.history
        has_tools = any(h.role in ("tool_call", "tool_output") for h in items)
        tool_mode = caps.get("history.tool_calls")
        if has_tools and tool_mode == "drop":
            return Ack.unsupported("history.tool_calls")
        if has_tools and tool_mode == "as_text" and init.history_format is not HistoryFormat.PAIRS_TOOLS_AS_TEXT:
            return Ack.unsupported("history.tool_calls")
        if items and not caps.get("history.injection"):
            return Ack.unsupported("history.injection")
        limits = caps.get("history.limits") or {}
        max_items = limits.get("max_items")
        if max_items is not None and len(items) > max_items:
            return Ack.invalid_state(f"history_over_limit:max_items={max_items}")
        max_item_bytes = limits.get("max_item_bytes")
        if max_item_bytes is not None and any(len(h.text.encode("utf-8")) > max_item_bytes for h in items):
            return Ack.invalid_state(f"history_over_limit:max_item_bytes={max_item_bytes}")
        max_tokens = limits.get("max_tokens")
        if max_tokens is not None and sum(len(h.text) for h in items) // 4 > max_tokens:  # rough estimate
            return Ack.invalid_state(f"history_over_limit:max_tokens={max_tokens}")
        for rule in caps.get("history.constraints") or []:
            if rule == "starts_with_user" and items and items[0].role != "user":
                return Ack.invalid_state("history_constraint:starts_with_user")
            if rule == "ends_with_user" and items and items[-1].role != "user":
                return Ack.invalid_state("history_constraint:ends_with_user")
        return None

    async def _abort_open(self, detail: str) -> Ack:
        """Timeout while half-open: close what was built (spec 04 §3.1)."""
        self._open = False
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        await self._safe_disconnect()
        return Ack.timeout(detail)

    async def _safe_disconnect(self) -> None:
        try:
            await self._provider.disconnect()
        except Exception:  # best effort while tearing down
            log.debug("disconnect raised during abort", exc_info=True)

    # -- uplink -------------------------------------------------------------------

    async def send_audio(self, frame: bytes, meta: AudioMeta) -> Ack:
        if (bad := self._require_open()) is not None:
            return bad
        if meta.turn_id is None:
            return Ack.invalid_state("turn_id")
        key = str(meta.turn_id)
        if key in self._turns:
            return Ack.invalid_state("already_committed")
        seqs = self._seqs.setdefault(key, set())
        if meta.seq in seqs:
            return Ack.success(duplicate=True)
        seqs.add(meta.seq)
        self._turn = meta.turn_id
        self._turn_ms[key] = self._turn_ms.get(key, 0) + duration_ms(frame, meta.sample_rate)
        try:
            await self._provider.append_audio(frame, meta.sample_rate)
        except Exception as exc:
            err = self._errors.map("send_failed", str(exc), scope=ErrorScope.SESSION)
            self._emit(self._emitter.error(err, turn_id=meta.turn_id))
            return Ack.upstream_error(err.error_class, "send_failed")
        return Ack.success()

    async def commit_turn(self, turn_id: TurnId, mode: Literal["commit", "discard"]) -> Ack:
        if (bad := self._require_open()) is not None:
            return bad
        key = str(turn_id)
        prev = self._turns.get(key)
        if prev is not None:
            if prev[0] == "commit" and mode == "commit":
                return prev[1]
            return Ack.invalid_state("already_committed" if prev[0] == "commit" else "discarded")
        cm = self.capabilities.get("turn.commit_mode")
        if mode == "discard":
            if cm in ("commit_create", "activity_window", "whole_utterance"):
                await self._provider.clear_input()
                return self._record(key, "discard", Ack.success(discarded=True))
            return Ack.unsupported("turn.commit_mode")
        if cm in ("server_only", "none"):
            return self._record(key, "commit", Ack.success(committed_by_server=True))
        if cm in ("commit_create", "activity_window", "whole_utterance"):
            min_ms = self.capabilities.get("turn.commit_min_ms")
            if isinstance(min_ms, int) and self._turn_ms.get(key, 0) < min_ms:
                return Ack.invalid_state("too_short")
            committed = self._expect(lambda e: e.type is EventType.INPUT_COMMITTED)
            await self._provider.commit_input()
            ev = await self._wait(
                committed, self.timeouts.commit_ack_ms, "commit_timeout", ErrorScope.SESSION, turn_id=turn_id
            )
            ack = Ack.timeout("commit_ack") if ev is TIMEOUT else Ack.success()
            return self._record(key, "commit", ack)
        return Ack.unsupported("turn.commit_mode")

    async def send_text(self, turn_id: TurnId, text: str) -> Ack:
        if (bad := self._require_open()) is not None:
            return bad
        key = str(turn_id)
        prev = self._turns.get(key)
        if prev is not None:
            return prev[1] if prev[0] == "commit" else Ack.invalid_state("discarded")
        if not hasattr(self._provider, "add_user_text"):
            return Ack.unsupported("uplink.text_input")
        await self._provider.add_user_text(text)
        return self._record(key, "commit", Ack.success(text=text))

    def _record(self, key: str, mode: str, ack: Ack) -> Ack:
        self._turns[key] = (mode, ack)
        return ack

    # -- responses ------------------------------------------------------------------

    async def request_response(
        self, turn_id: TurnId, response_ref: str, overrides: ResponseOverrides | None = None
    ) -> Ack:
        if (bad := self._require_open()) is not None:
            return bad
        if response_ref in self._refs:
            return Ack.success(response_id=self._refs[response_ref])
        if self.capabilities.get("response.client_initiated") != "yes":
            return Ack.unsupported("response.client_initiated")
        if (bad := self._check_overrides(overrides)) is not None:
            return bad
        for rid in self._tracker.active_ids():  # server already started one: bind, origin tells the caller
            st = self._tracker.started_event(rid)
            if st is not None and st.response_ref is None and st.origin is not ResponseOrigin.CLIENT:
                self._tracker.bind_ref(rid, response_ref)
                self._mirror.bind_ref(rid, response_ref)
                self._refs[response_ref] = rid
                return Ack.success(response_id=rid, origin=st.origin.value if st.origin else "server")
        if self.capabilities.get("response.single_slot") == "yes" and self._tracker.active_ids():
            return Ack.slot_busy()
        self._pending_ref = (response_ref, turn_id)
        started = self._expect(lambda e: e.type is EventType.RESPONSE_STARTED and e.response_ref == response_ref)
        ov = None if overrides is None else {k: v for k, v in asdict(overrides).items() if v is not None}
        try:
            rid = await self._provider.create_response(ov)
        except Exception as exc:
            self._pending_ref = None
            started.cancel()
            err = self._errors.map(type(exc).__name__, str(exc), scope=ErrorScope.RESPONSE, session_open=self._open)
            self._emit(self._emitter.error(err, turn_id=turn_id))
            return Ack.upstream_error(err.error_class, err.raw_code)
        if rid is not None and rid in self._refs.values():
            started.cancel()
            return Ack.success(response_id=rid)
        ev = await self._wait(
            started, self.timeouts.response_started_ms, "response_timeout", ErrorScope.RESPONSE,
            turn_id=turn_id, response_id=rid,
        )
        if ev is TIMEOUT:
            self._pending_ref = None
            return Ack.timeout("response_started")
        assert ev.response_id is not None
        self._refs[response_ref] = ev.response_id
        return Ack.success(response_id=ev.response_id)

    async def cancel_response(self, response_id: str) -> Ack:
        if not self._tracker.is_active(response_id):
            return Ack.success()
        self._mark_cancelled(response_id)
        if self.capabilities.get("response.client_initiated") != "yes":
            # no cancel API on upstreams that cannot start responses either (spec 04 §3.5)
            self._finish_response(response_id, ResponseStatus.CANCELLED, ReplyModality.NONE, confirmed=False)
            return Ack.success(local_only=True)
        done = self._expect(lambda e: e.type is EventType.RESPONSE_DONE and e.response_id == response_id)
        await self._provider.cancel_response(response_id)
        ev = await self._wait(
            done, self.timeouts.cancel_ack_ms, "cancel_timeout", ErrorScope.RESPONSE,
            error_class=ErrorClass.BENIGN, response_id=response_id,
        )
        if ev is TIMEOUT:
            self._finish_response(response_id, ResponseStatus.CANCELLED, ReplyModality.NONE, confirmed=False)
            return Ack.success(confirmed=False)
        return Ack.success(confirmed=True)

    # -- truncate ---------------------------------------------------------------------

    async def truncate(self, item_id: str, audio_end_ms: int, played_text: str | None = None) -> Ack:
        if (bad := self._require_open()) is not None:
            return bad
        mode = self.capabilities.get("truncate.mode")
        if mode == "audio_ms":
            prev = self._truncated.get(item_id)
            if prev is not None and audio_end_ms >= prev:
                return Ack.success()  # only monotonically smaller values are sent
            confirmed = self._expect(
                lambda e: e.type is EventType.ITEM_ECHO and e.kind == "truncated" and e.item_id == item_id
            )
            await self._provider.truncate_item(item_id, audio_end_ms)  # 0: provider deletes the item
            self._truncated[item_id] = audio_end_ms
            ev = await self._wait(
                confirmed, self.timeouts.truncate_ack_ms, "truncate_timeout", ErrorScope.ITEM,
                error_class=ErrorClass.BENIGN, item_id=item_id,
            )
            return Ack.timeout("truncate_ack") if ev is TIMEOUT else Ack.success()
        if mode == "text_rewrite":
            if played_text is None:
                return Ack.invalid_state("played_text")
            # contract gap: RealtimeS2S has no delete_item/create_item; reported, not worked around
            return Ack.unsupported("truncate.mode")
        if mode == "local_only":
            return Ack.success(local_only=True)
        return Ack.unsupported("truncate.mode")

    # -- tool results (spec 04 §3.7 table) ---------------------------------------------

    async def send_tool_result(self, call_id: str, result: dict[str, Any], reply_required: bool) -> Ack:
        if (bad := self._require_open()) is not None:
            return bad
        if not self._mirror.has_call(call_id):
            return Ack.invalid_state("unknown_call")
        if self._mirror.result_sent(call_id):
            return Ack.success(duplicate=True)
        auto = self.capabilities.get("tools.auto_reply_after_result")  # unknown -> "yes" (conservative)
        silent = self.capabilities.get("tools.silent_close")
        if not reply_required and auto != "no" and silent != "flag":
            return Ack.unsupported("tools.silent_close")  # not sent; the tool layer chooses
        if reply_required and auto != "no":
            self._expect_auto_tool = True  # the server's own reply is bound as origin auto_tool
        echo = self._expect(lambda e: e.type is EventType.ITEM_ECHO and e.local_id == call_id)
        # `flag` would need a silent parameter on RealtimeS2S.add_tool_output (contract gap, reported)
        await self._provider.add_tool_output(call_id, json.dumps(result, ensure_ascii=False))
        self._mirror.mark_result_sent(call_id)
        if self.capabilities.get("history.item_ack") == "yes":
            ev = await self._wait(
                echo, self.timeouts.tool_result_ack_ms, "tool_result_timeout", ErrorScope.CALL,
                error_class=ErrorClass.BENIGN, call_id=call_id,
            )
            if ev is TIMEOUT:
                return Ack.timeout("tool_result_ack")
        else:
            echo.cancel()
        if reply_required and auto == "no":
            call = self._mirror.calls[call_id]
            turn = self._tracker.started_event(call.response_id or "")
            turn_id = turn.turn_id if turn else self._turn
            return await self.request_response(turn_id, f"tool:{call_id}", None)  # type: ignore[arg-type]
        return Ack.success()

    # -- update_config (spec 04 §3.8) ----------------------------------------------------

    async def update_config(
        self, patch: ConfigPatch, mode: Literal["live", "append", "next_session"]
    ) -> Ack:
        async with self._lock:
            if (bad := self._require_open()) is not None:
                return bad
            if mode == "next_session":
                return Ack.success(deferred=True)  # registered by the rebuilder, not stored here
            if mode == "append":
                return await self._append(patch)
            if patch.state_block_version is not None and patch.state_block_version == self._config_version:
                return Ack.success(duplicate=True)
            targets = {"instructions": patch.instructions, "tools": patch.tools, "voice": patch.voice}
            present = {k: v for k, v in targets.items() if v is not None}
            full = False
            for target in present:
                how = self.capabilities.get(f"config.update.{target}")
                if how not in _LIVE:
                    return Ack.unsupported(f"config.update.{target}")  # never reconnect to make it stick
                full = full or how == "live_full"
            if not present:
                return Ack.success()
            if "tools" in present:
                present["tools"] = [asdict(t) for t in present["tools"]]
            self._config.update(present)
            payload = dict(self._config) if full else present
            if patch.state_block_version is not None:
                payload["state_block_version"] = patch.state_block_version
            return await self._send_update(payload, patch.state_block_version)

    async def _append(self, patch: ConfigPatch) -> Ack:
        channels = self.capabilities.get("config.append_channels") or []
        if not channels:
            return Ack.unsupported("config.append_channels")
        text = patch.instructions or ""
        limits = [c.get("max_tokens") for c in channels if isinstance(c, dict) and c.get("max_tokens")]
        if limits and len(text) // 4 > min(limits):
            return Ack.invalid_state("too_long")
        channel = channels[0]["name"] if isinstance(channels[0], dict) else channels[0]
        return await self._send_update({"append": {"channel": channel, "text": text}}, patch.state_block_version)

    async def _send_update(self, payload: dict[str, Any], version: str | None) -> Ack:
        acked = self._expect(lambda e: e.type is EventType.ITEM_ECHO and e.kind == "session.updated")
        await self._provider.update_session(payload)
        if self.capabilities.get("config.update_ack") == "yes":
            ev = await self._wait(acked, self.timeouts.update_config_ack_ms, "update_config_timeout", ErrorScope.SESSION)
            if ev is TIMEOUT:
                return Ack.timeout("update_config_ack")
        else:
            acked.cancel()
        if version is not None:
            self._config_version = version
        return Ack.success()

    # -- close ------------------------------------------------------------------------------

    async def close(self, reason: str) -> Ack:
        async with self._lock:
            if self._closed:
                return Ack.success()
            self._closed = True
            for w in self._watch.values():
                w.cancel()
            self._watch.clear()
            self._cancel_all_active(reason)
            res: Any = None
            if self._open:
                res = await self._with_timeout(
                    self._provider.disconnect(), self.timeouts.close_grace_ms, "close_timeout", ErrorScope.SESSION
                )
            if self._pump is not None:
                self._pump.cancel()
            self._emit(self._emitter.session_closed(reason, "client"))
            self._emitter.close()
            return Ack.timeout("close_grace") if res is TIMEOUT else Ack.success()
