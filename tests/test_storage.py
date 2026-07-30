from pathlib import Path

from job_radar.models import JobRecord, Platform
from job_radar.storage import JobStore


def test_upsert_merges_queries(tmp_path: Path):
    db = tmp_path / "jobs.sqlite3"
    job = JobRecord(
        platform=Platform.BOSS,
        source_job_id="abc",
        job_url="https://example.com/abc",
        title="AI工程师",
        company_name="示例公司",
        city="杭州",
        matched_queries=["AI"],
    )
    with JobStore(db) as store:
        assert store.upsert(job) == "inserted"
        job.matched_queries = ["大模型"]
        assert store.upsert(job) == "updated"
        row = store.rows()[0]
        assert "AI" in row["matched_queries_json"]
        assert "大模型" in row["matched_queries_json"]
