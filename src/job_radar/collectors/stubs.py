from __future__ import annotations

from collections.abc import AsyncIterator

from ..models import CityConfig, JobRecord, SearchFilters
from .base import Collector


class PendingPlatformCollector(Collector):
    def __init__(self, platform_name: str):
        self.platform_name = platform_name

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def collect(
        self,
        city: CityConfig,
        keyword: str,
        filters: SearchFilters,
        pages: int,
        detail_limit: int,
    ) -> AsyncIterator[JobRecord]:
        raise NotImplementedError(
            f"{self.platform_name} 适配器尚未实现。第一版先验证 BOSS 数据链路和统一存储。"
        )
        yield  # pragma: no cover
