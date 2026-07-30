from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlencode

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError

from ..models import AutoChatPolicy, CityConfig, CollectionConfig, JobRecord, Platform, SearchFilters
from ..normalization import (
    active_days_from_desc,
    founded_year_from_intro,
    is_daily_rate,
    parse_salary,
    source_id_from_url,
    split_location,
)
from .base import Collector
from .browser import BrowserSession


ORIGIN = "https://www.zhipin.com"
API_PATH = "/wapi/zpgeek/search/joblist.json"
DETAIL_PATH = "/wapi/zpgeek/job/detail.json"
SEARCH_BASE = f"{ORIGIN}/web/geek/job"

# BOSS 的搜索页在加载过程中会短暂跳到 about:blank 再跳回，此时相对路径无法解析，
# 因此同源请求一律使用绝对地址，并在请求前等待页面回到 zhipin 域。
# 31/37 来自公开项目实测；36「您的账户存在异常行为」是本地实测命中的账号级风控，
# 比环境级更严重，出现后应停止自动访问并改用普通浏览器手动操作一段时间。
RISK_CODES = {31, 36, 37}
RISK_TEXTS = (
    "访问频繁",
    "安全校验",
    "环境存在异常",
    "账户存在异常",
    "操作过于频繁",
)


class RiskControlStop(RuntimeError):
    """命中平台风控。必须立刻停止整轮采集，不重试。"""

FILTER_CODES = {
    "salary": {
        "3K以下": "402", "3-5K": "403", "5-10K": "404", "10-20K": "405",
        "20-50K": "406", "50K以上": "407",
    },
    "experience": {
        "在校生": "108", "应届生": "102", "经验不限": "101", "1年以内": "103",
        "1-3年": "104", "3-5年": "105", "5-10年": "106", "10年以上": "107",
    },
    "degree": {"初中及以下": "209", "中专/中技": "208", "高中": "206", "大专": "202", "本科": "203", "硕士": "204", "博士": "205"},
    "scale": {"0-20人": "301", "20-99人": "302", "100-499人": "303", "500-999人": "304", "1000-9999人": "305", "10000人以上": "306"},
    "stage": {"未融资": "801", "天使轮": "802", "A轮": "803", "B轮": "804", "C轮": "805", "D轮及以上": "806", "已上市": "807", "不需要融资": "808"},
}


class BossCollector(Collector):
    def __init__(self, config: CollectionConfig):
        self.config = config
        self.browser_session = BrowserSession(config.browser)
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.chat_count = 0
        self.request_count = 0
        self.last_lid = ""
        self.owns_page = False

    async def start(self) -> None:
        self.context = await self.browser_session.start()
        # 优先复用浏览器里已经登录并加载好的 zhipin 标签页。
        # 实测直接打开 https://www.zhipin.com/ 会经 /{城市}/?seoRefer=index 最终跳到
        # about:blank，新开的空白页反而进不去；复用已有页签最稳。
        for existing in self.context.pages:
            if ORIGIN in (existing.url or ""):
                self.page = existing
                self.owns_page = False
                break
        if self.page is None:
            self.page = await self.context.new_page()
            self.owns_page = True
        self.page.set_default_timeout(self.config.browser.navigation_timeout_ms)

    async def close(self) -> None:
        # 只关闭自己开的页签，不动用户原有的登录页。
        if self.page and self.owns_page:
            await self.page.close()
        await self.browser_session.close()

    async def _goto_search(self, url: str, attempts: int = 3) -> None:
        """打开搜索页。页面可能瞬时停在 about:blank，重试即可恢复。"""
        assert self.page is not None
        for attempt in range(1, attempts + 1):
            await self.page.goto(url, wait_until="domcontentloaded")
            await self.page.wait_for_timeout(random.randint(2500, 4000))
            if ORIGIN in (self.page.url or ""):
                return
            if attempt < attempts:
                await asyncio.sleep(random.uniform(2.0, 4.0))
        raise RuntimeError(f"搜索页反复跳到 {self.page.url}，无法继续")

    @staticmethod
    def _filter_value(name: str, value: str | None) -> str | None:
        if not value:
            return None
        return FILTER_CODES.get(name, {}).get(value, value)

    def _params(self, city: CityConfig, keyword: str, filters: SearchFilters, page: int) -> dict[str, str | int]:
        if not city.boss_code:
            raise ValueError(f"城市 {city.name} 缺少 boss_code")
        params: dict[str, str | int] = {
            "scene": "1",
            "query": keyword,
            "city": city.boss_code,
            "page": page,
            "pageSize": self.config.page_size,
        }
        for name, value in filters.model_dump().items():
            code = self._filter_value(name, value)
            if code:
                params[name] = code
        return params

    async def _settle_on_origin(self, timeout_ms: int = 15_000) -> None:
        """等待页面停在 zhipin 域。搜索页初始化时会短暂停在 about:blank。"""
        assert self.page is not None
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if ORIGIN in (self.page.url or ""):
                return
            await self.page.wait_for_timeout(250)
        raise RuntimeError(f"页面未停留在 {ORIGIN}，当前为 {self.page.url}")

    async def _evaluate_fetch(self, url: str, attempts: int = 3) -> dict:
        """在页面里发同源请求。

        BOSS 的安全校验会在加载后触发一次跳转，正好撞上 evaluate 时 Playwright 会抛
        "Execution context was destroyed"。这不是风控，等页面稳定后重试即可。
        """
        assert self.page is not None
        script = """async (url) => {
            try {
                const response = await fetch(url, {credentials: 'include'});
                const text = await response.text();
                return {status: response.status, text};
            } catch (err) {
                return {status: -1, text: '', error: String(err)};
            }
        }"""
        for attempt in range(1, attempts + 1):
            if self.request_count >= self.config.max_requests_per_run:
                raise RiskControlStop(
                    f"已达单次运行请求上限 {self.config.max_requests_per_run}，主动停止。"
                )
            self.request_count += 1
            try:
                return await self.page.evaluate(script, url)
            except PlaywrightError as exc:
                if "Execution context was destroyed" not in str(exc) or attempt == attempts:
                    raise
                await self._settle_on_origin()
                await asyncio.sleep(random.uniform(1.5, 3.0))
        raise RuntimeError("同源请求重试耗尽")  # pragma: no cover

    async def _same_origin_fetch(self, path: str) -> dict:
        assert self.page is not None
        await self._settle_on_origin()
        result = await self._evaluate_fetch(
            f"{ORIGIN}{path}" if path.startswith("/") else path
        )
        status = int(result.get("status", 0))
        if result.get("error"):
            raise RuntimeError(f"同源请求失败: {result['error']}")
        raw_text = result.get("text") or ""
        if any(token in raw_text for token in RISK_TEXTS):
            raise RiskControlStop(f"响应命中风控关键字，HTTP {status}")
        try:
            body = json.loads(raw_text or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"BOSS 接口返回非 JSON，HTTP {status}") from exc
        code = body.get("code")
        message = str(body.get("message") or "")
        if status in (401, 403, 429) or code in RISK_CODES:
            raise RiskControlStop(f"命中平台风控 HTTP {status} code={code}: {message}")
        if code not in (None, 0):
            raise RuntimeError(f"BOSS 接口错误 code={code}: {message}")
        return body

    def _parse_api_job(self, raw: dict, keyword: str, rank: int) -> JobRecord:
        encrypt_job_id = str(raw.get("encryptJobId") or "")
        job_url = f"https://www.zhipin.com/job_detail/{encrypt_job_id}.html" if encrypt_job_id else ""
        city = str(raw.get("cityName") or "")
        district = str(raw.get("areaDistrict") or "")
        business = str(raw.get("businessDistrict") or "")
        salary = str(raw.get("salaryDesc") or "")
        salary_min, salary_max, salary_months = parse_salary(salary)
        return JobRecord(
            platform=Platform.BOSS,
            source_job_id=encrypt_job_id or source_id_from_url(job_url),
            job_url=job_url,
            company_url=(
                f"https://www.zhipin.com/gongsi/{raw.get('encryptBrandId')}.html"
                if raw.get("encryptBrandId") else ""
            ),
            title=str(raw.get("jobName") or ""),
            company_name=str(raw.get("brandName") or ""),
            city=city,
            district=district,
            business_district=business,
            location_text=split_location(city, district, business),
            salary_text=salary,
            salary_min_k=salary_min,
            salary_max_k=salary_max,
            salary_months=salary_months,
            experience=str(raw.get("jobExperience") or ""),
            education=str(raw.get("jobDegree") or ""),
            company_size=str(raw.get("brandScaleName") or ""),
            financing_stage=str(raw.get("brandStageName") or ""),
            industry=str(raw.get("brandIndustry") or ""),
            recruiter_name=str(raw.get("bossName") or ""),
            recruiter_title=str(raw.get("bossTitle") or ""),
            recruiter_active_status=str(raw.get("activeTimeDesc") or ("在线" if raw.get("bossOnline") else "")),
            skills=[str(v) for v in (raw.get("skills") or []) if v],
            labels=[str(v) for v in (raw.get("jobLabels") or []) if v],
            welfare=[str(v) for v in (raw.get("welfareList") or []) if v],
            matched_queries=[keyword],
            source_rank=rank,
            raw=raw,
        )

    def _should_keep(self, job: JobRecord) -> bool:
        title = job.title.lower()
        company = job.company_name.lower()
        if self.config.include_title_keywords and not any(
            word.lower() in title for word in self.config.include_title_keywords
        ):
            return False
        if any(word.lower() in title for word in self.config.exclude_title_keywords):
            return False
        if any(word.lower() in company for word in self.config.exclude_company_keywords):
            return False
        # 日薪岗基本都是实习/日结，即使标题没写“实习”也按薪资口径排除。
        if is_daily_rate(job.salary_text):
            return False
        return bool(job.title and job.job_url)

    async def _collect_detail(self, job: JobRecord) -> JobRecord:
        """从详情接口补全 JD 与招聘者活跃度。

        实测（2026-07-29）搜索列表接口不返回 activeTimeDesc，只有详情接口的
        bossInfo 里有；JD 正文在 jobInfo.postDescription，是干净的纯文本，
        不需要解析 DOM，也就不受前端 class 名改版影响。
        详情接口必须带列表响应里的 securityId 和 lid，否则会被打回空白页。
        """
        raw = job.raw
        security_id = str(raw.get("securityId") or "")
        if not security_id:
            raise RuntimeError("列表记录缺少 securityId，无法请求详情接口")
        params = {"securityId": security_id, "lid": str(raw.get("lid") or self.last_lid or "")}
        body = await self._same_origin_fetch(f"{DETAIL_PATH}?{urlencode(params)}")
        zp = body.get("zpData") or {}
        job_info = zp.get("jobInfo") or {}
        boss_info = zp.get("bossInfo") or {}
        brand_info = zp.get("brandComInfo") or {}

        description = str(job_info.get("postDescription") or "").strip()
        if description:
            job.job_description = re.sub(r"\n{3,}", "\n\n", description)

        # 详情页的活跃度比列表准：列表 bossOnline 常年 false，activeTimeDesc 根本不返回。
        active_desc = str(boss_info.get("activeTimeDesc") or "")
        if active_desc:
            job.recruiter_active_status = active_desc
            job.recruiter_active_days = active_days_from_desc(active_desc)
        job.recruiter_online = bool(boss_info.get("bossOnline"))
        job.recruiter_certificated = bool(boss_info.get("certificated"))
        if boss_info.get("name"):
            job.recruiter_name = str(boss_info["name"])
        if boss_info.get("title"):
            job.recruiter_title = str(boss_info["title"])
        # 招聘者唯一 ID：同一个 ID 挂大量岗位是僵尸/中介信号，公司名不可靠时靠它聚合。
        if job_info.get("encryptUserId"):
            job.recruiter_id = str(job_info["encryptUserId"])
        job.job_status_desc = str(job_info.get("jobStatusDesc") or "")
        job.position_name = str(job_info.get("positionName") or "")
        job.recruitment_count = str(job_info.get("recruitmentCountDesc") or "")
        if job_info.get("latitude") is not None:
            job.latitude = float(job_info["latitude"])
        if job_info.get("longitude") is not None:
            job.longitude = float(job_info["longitude"])

        # proxyJob/proxyType 非 0 表示代招（猎头、外包、人力公司）。
        job.is_proxy_job = bool(job_info.get("proxyJob")) or bool(job_info.get("proxyType"))
        job.job_invalid = bool(job_info.get("invalidStatus"))
        if job_info.get("address"):
            job.address = str(job_info["address"])
        for skill in job_info.get("showSkills") or []:
            text = str(skill or "").strip()
            if text and text not in job.skills:
                job.skills.append(text)
        if brand_info.get("scaleName"):
            job.company_size = str(brand_info["scaleName"])
        if brand_info.get("stageName"):
            job.financing_stage = str(brand_info["stageName"])
        if brand_info.get("industryName"):
            job.industry = str(brand_info["industryName"])
        # brandId 比公司名可靠：BOSS 上有大量「某大型互联网公司」这类匿名马甲，
        # 只有 ID 能把它们聚合成同一家。
        if brand_info.get("encryptBrandId"):
            job.brand_id = str(brand_info["encryptBrandId"])
        intro = str(brand_info.get("introduce") or "").strip()
        if intro:
            job.company_intro = intro
            # 注册资本 BOSS 完全不给；成立年份只能从简介文本里碰运气提取。
            job.company_founded_year = founded_year_from_intro(intro)
        for label in brand_info.get("labels") or []:
            text = str(label or "").strip()
            if text and text not in job.welfare:
                job.welfare.append(text)
        job.raw["detail"] = zp
        await asyncio.sleep(random.uniform(*self.config.detail_delay_seconds))
        return job

    async def _maybe_chat(self, job: JobRecord) -> str:
        policy: AutoChatPolicy = self.config.auto_chat
        if not policy.enabled:
            return "disabled"
        if self.chat_count >= policy.max_actions_per_run:
            return "limit_reached"
        if policy.dry_run:
            self.chat_count += 1
            return "dry_run"
        if policy.require_manual_confirm:
            answer = input(f"\n是否与 {job.company_name} / {job.title} 开聊？[y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                return "declined"
        assert self.context is not None
        page = await self.context.new_page()
        try:
            await page.goto(job.job_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1800)
            button = page.get_by_text(re.compile(r"^(立即沟通|继续沟通)$")).first
            if not await button.count():
                return "button_not_found"
            await button.click()
            await page.wait_for_timeout(1200)
            # 默认依赖 BOSS 已设置的招呼语。若页面出现可编辑输入框，才填入配置内容。
            editor = page.locator("textarea, [contenteditable='true']").last
            if await editor.count() and policy.greeting:
                try:
                    await editor.fill(policy.greeting)
                    send = page.get_by_text(re.compile(r"^(发送|送出)$")).last
                    if await send.count():
                        await send.click()
                except Exception:
                    pass
            self.chat_count += 1
            await asyncio.sleep(random.uniform(policy.min_delay_seconds, policy.max_delay_seconds))
            return "executed"
        finally:
            await page.close()

    async def collect(
        self,
        city: CityConfig,
        keyword: str,
        filters: SearchFilters,
        pages: int,
        detail_limit: int,
    ) -> AsyncIterator[JobRecord]:
        assert self.page is not None
        params = self._params(city, keyword, filters, 1)
        visible = {k: v for k, v in params.items() if k not in ("pageSize", "scene")}
        # 搜索页初始化期间会短暂跳到 about:blank，_goto_search 内部会重试到停在 zhipin 域。
        await self._goto_search(f"{SEARCH_BASE}?{urlencode(visible)}")

        detailed = 0
        for page_index in range(1, pages + 1):
            api_params = self._params(city, keyword, filters, page_index)
            # 平台用上一页响应里的 lid 串联同一次搜索会话，带上它更接近真实翻页。
            if self.last_lid:
                api_params["lid"] = self.last_lid
            body = await self._same_origin_fetch(f"{API_PATH}?{urlencode(api_params)}")
            zp_data = body.get("zpData") or {}
            if zp_data.get("lid"):
                self.last_lid = str(zp_data["lid"])
            raw_jobs = zp_data.get("jobList") or []
            if not raw_jobs:
                break
            for rank, raw in enumerate(raw_jobs, start=1):
                job = self._parse_api_job(raw, keyword, rank)
                if not self._should_keep(job):
                    continue
                if self.config.collect_details and detailed < detail_limit:
                    try:
                        job = await self._collect_detail(job)
                        detailed += 1
                    except Exception as exc:
                        job.raw["detail_error"] = str(exc)
                chat_status = await self._maybe_chat(job)
                if chat_status != "disabled":
                    job.raw["auto_chat_status"] = chat_status
                yield job
            if page_index < pages:
                low, high = self.config.request_delay_seconds
                await asyncio.sleep(random.uniform(low, high))
