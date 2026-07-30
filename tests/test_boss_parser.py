import json
from pathlib import Path

from job_radar.collectors.boss import BossCollector
from job_radar.models import CollectionConfig


def test_parse_api_job():
    cfg = CollectionConfig.model_validate({
        "cities": [{"name": "杭州", "boss_code": "101210100"}],
        "keywords": ["AI Agent"],
    })
    collector = BossCollector(cfg)
    raw = json.loads(Path("tests/fixtures/boss_joblist.json").read_text(encoding="utf-8"))["zpData"]["jobList"][0]
    job = collector._parse_api_job(raw, "AI Agent", 1)
    assert job.title == "AI Agent 开发工程师"
    assert job.company_name == "示例科技"
    assert job.salary_min_k == 20
    assert job.salary_months == 14
    assert job.recruiter_active_status == "在线"
