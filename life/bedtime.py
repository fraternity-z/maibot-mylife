"""按日程维护入睡推迟与醒后重新入睡，不参与回复触发。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

BEDTIME_SILENCE_MINUTES = 10
BEDTIME_LOOP_SECONDS = 60
_SLEEP_PHASES = {"falling_asleep", "light_sleep", "deep_sleep"}


class BedtimeManager:
    def __init__(self, ctx: Any, store: Any, config: Any, environment: Any,
                 state_engine: Any, logger: Any) -> None:
        self.ctx = ctx; self.store = store; self.config = config
        self.environment = environment; self.state_engine = state_engine; self.logger = logger
        self._segment_cache: dict[str, Any] = {}

    def update_config(self, config: Any) -> None:
        self.config = config

    async def _current_segment(self, now: datetime) -> dict[str, Any]:
        nodes = await self.store.get_framework(now.strftime("%Y-%m-%d"))
        minute = now.hour * 60 + now.minute
        for node in nodes:
            start = int(node.get("start_minute") or 0); end = int(node.get("end_minute") or 0)
            if (start <= minute < end if start < end else minute >= start or minute < end):
                return node
        return {}

    @staticmethod
    def _segment_start_ts(now: datetime, segment: dict[str, Any]) -> float:
        start = int(segment.get("start_minute") or 0)
        moment = now.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
        if moment > now:
            moment -= timedelta(days=1)
        return moment.timestamp()

    def _sleep_period_floor(self, now: datetime) -> float:
        if str(self._segment_cache.get("kind") or "") == "sleep":
            return self._segment_start_ts(now, self._segment_cache)
        return 0.0

    def _still_resting(self, now: datetime) -> bool:
        return str(self._segment_cache.get("kind") or "") in {"sleep", "nap"}

    async def tick(self, now: datetime) -> None:
        self._segment_cache = await self._current_segment(now)
        runtime = await self.store.get_sleep_runtime()
        phase = str(runtime.get("phase") or "")
        await self._update_sleep_defer(now, runtime, phase)
        await self._check_resleep(now, runtime, phase)

    async def _update_sleep_defer(self, now: datetime, runtime: dict[str, Any], phase: str) -> None:
        defer = 0.0
        # 在进入睡眠段前保留最近对话的推迟点，避免状态循环先将相位推进为睡眠。
        if phase not in _SLEEP_PHASES and phase != "woken":
            users = await self.store.list_users()
            latest = max((float(user.get("last_user_message_at") or 0) for user in users), default=0.0)
            defer = max(self._sleep_period_floor(now), latest + BEDTIME_SILENCE_MINUTES * 60)
        if float(runtime.get("sleep_defer_until", 0) or 0) != defer:
            runtime["sleep_defer_until"] = defer
            await self.store.save_sleep_runtime(runtime)

    async def _check_resleep(self, now: datetime, runtime: dict[str, Any], phase: str) -> None:
        if phase != "woken" or float(runtime.get("awake_grace_until", 0) or 0) > now.timestamp():
            return
        if self._still_resting(now):
            await self.state_engine.mark_resleep(now)

    def approaching(self, now: datetime) -> bool:
        """日程睡眠段内提供睡前氛围；不改变放行概率。"""
        return str(self._segment_cache.get("kind") or "") == "sleep"
