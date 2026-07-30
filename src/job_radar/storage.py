from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import JobRecord
from .normalization import merge_unique, normalized_text, stable_id


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS jobs (
    canonical_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    source_job_id TEXT NOT NULL,
    job_url TEXT NOT NULL,
    company_url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    company_name TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    district TEXT NOT NULL DEFAULT '',
    business_district TEXT NOT NULL DEFAULT '',
    location_text TEXT NOT NULL DEFAULT '',
    salary_text TEXT NOT NULL DEFAULT '',
    salary_min_k REAL,
    salary_max_k REAL,
    salary_months INTEGER,
    experience TEXT NOT NULL DEFAULT '',
    education TEXT NOT NULL DEFAULT '',
    company_size TEXT NOT NULL DEFAULT '',
    financing_stage TEXT NOT NULL DEFAULT '',
    industry TEXT NOT NULL DEFAULT '',
    recruiter_name TEXT NOT NULL DEFAULT '',
    recruiter_title TEXT NOT NULL DEFAULT '',
    recruiter_active_status TEXT NOT NULL DEFAULT '',
    recruiter_active_days INTEGER,
    recruiter_online INTEGER NOT NULL DEFAULT 0,
    is_proxy_job INTEGER NOT NULL DEFAULT 0,
    job_invalid INTEGER NOT NULL DEFAULT 0,
    address TEXT NOT NULL DEFAULT '',
    job_description TEXT NOT NULL DEFAULT '',
    skills_json TEXT NOT NULL DEFAULT '[]',
    labels_json TEXT NOT NULL DEFAULT '[]',
    welfare_json TEXT NOT NULL DEFAULT '[]',
    matched_queries_json TEXT NOT NULL DEFAULT '[]',
    source_rank INTEGER,
    -- 详情接口里已经抓到、原先没落库的字段。
    brand_id TEXT NOT NULL DEFAULT '',
    recruiter_id TEXT NOT NULL DEFAULT '',
    recruiter_certificated INTEGER NOT NULL DEFAULT 0,
    company_intro TEXT NOT NULL DEFAULT '',
    company_founded_year INTEGER,
    job_status_desc TEXT NOT NULL DEFAULT '',
    position_name TEXT NOT NULL DEFAULT '',
    recruitment_count TEXT NOT NULL DEFAULT '',
    latitude REAL,
    longitude REAL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    collected_at TEXT NOT NULL,
    -- 增量采集的时间轴：first_seen_at 永不覆盖，用来算岗位存活天数。
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    seen_count INTEGER NOT NULL DEFAULT 1,
    detail_fetched_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, source_job_id)
);

-- 每次采集运行的记录，用来对比多轮之间的增量（新增了多少、请求用了多少）。
-- 岗位第一次出现的时间已经在 jobs.first_seen_at 里，不需要再建一张按运行记录
-- 出现次数的表——那张表没有代码会写入，属于当前不需要的实体。
CREATE TABLE IF NOT EXISTS collection_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    config_label TEXT NOT NULL DEFAULT '',
    stats_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS cross_platform_groups (
    group_id TEXT NOT NULL,
    canonical_id TEXT NOT NULL REFERENCES jobs(canonical_id) ON DELETE CASCADE,
    confidence TEXT NOT NULL DEFAULT 'candidate',
    PRIMARY KEY(group_id, canonical_id)
);

CREATE TABLE IF NOT EXISTS chat_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_id TEXT NOT NULL REFERENCES jobs(canonical_id),
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# 索引单独放一份，必须在列迁移之后才能建——建表脚本对旧库是 IF NOT EXISTS
# 空操作，这时新字段还不存在，索引若和建表脚本放在一起执行会直接报错。
SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_jobs_city ON jobs(city);
CREATE INDEX IF NOT EXISTS idx_jobs_title ON jobs(title);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_name);
CREATE INDEX IF NOT EXISTS idx_jobs_brand ON jobs(brand_id);
CREATE INDEX IF NOT EXISTS idx_jobs_recruiter ON jobs(recruiter_id);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen_at);
"""


class JobStore:
    # 列名 -> (SQLite 类型声明, 用于 ALTER TABLE 的默认值表达式)。
    # CREATE TABLE IF NOT EXISTS 不会给已存在的旧库补列，所以老库（比如上次
    # 只有 37 列那版）打开时要靠这张表逐个 ALTER TABLE ADD COLUMN 补齐。
    MIGRATION_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("recruiter_certificated", "INTEGER NOT NULL DEFAULT 0", "0"),
        ("brand_id", "TEXT NOT NULL DEFAULT ''", "''"),
        ("recruiter_id", "TEXT NOT NULL DEFAULT ''", "''"),
        ("company_intro", "TEXT NOT NULL DEFAULT ''", "''"),
        ("company_founded_year", "INTEGER", "NULL"),
        ("job_status_desc", "TEXT NOT NULL DEFAULT ''", "''"),
        ("position_name", "TEXT NOT NULL DEFAULT ''", "''"),
        ("recruitment_count", "TEXT NOT NULL DEFAULT ''", "''"),
        ("latitude", "REAL", "NULL"),
        ("longitude", "REAL", "NULL"),
        # first_seen_at/last_seen_at 在建表语句里是 NOT NULL 无默认值，旧库
        # 补列时用已有的 collected_at 回填，保证语义一致（首次见到的时间）。
        ("first_seen_at", "TEXT NOT NULL DEFAULT ''", "collected_at"),
        ("last_seen_at", "TEXT NOT NULL DEFAULT ''", "collected_at"),
        ("seen_count", "INTEGER NOT NULL DEFAULT 1", "1"),
        ("detail_fetched_at", "TEXT NOT NULL DEFAULT ''", "''"),
    )

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate_legacy_columns()
        self.conn.executescript(SCHEMA_INDEXES)

    def _migrate_legacy_columns(self) -> None:
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(jobs)")}
        for name, decl, backfill in self.MIGRATION_COLUMNS:
            if name in existing:
                continue
            self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
            if backfill == "collected_at":
                # 回填用 collected_at，把旧数据的“首次/最近见到”都视作上次采集时间。
                self.conn.execute(f"UPDATE jobs SET {name} = collected_at")
            elif backfill != "NULL":
                self.conn.execute(f"UPDATE jobs SET {name} = {backfill}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    @staticmethod
    def cross_platform_group(job: JobRecord) -> str:
        return stable_id(job.company_name, job.title, job.city, length=20)

    # 增量采集：first_seen_at 只在首次插入时写入，之后永不覆盖；
    # last_seen_at 每次命中都刷新；seen_count 累加。三者合起来才能回答
    # “这个岗位第一次出现是什么时候、最近还在不在、一共被看到过几次”。
    INSERT_COLUMNS = (
        "canonical_id", "platform", "source_job_id", "job_url", "company_url",
        "title", "company_name", "city", "district", "business_district",
        "location_text", "salary_text", "salary_min_k", "salary_max_k",
        "salary_months", "experience", "education", "company_size",
        "financing_stage", "industry", "recruiter_name", "recruiter_title",
        "recruiter_active_status", "recruiter_active_days", "recruiter_online",
        "recruiter_certificated", "recruiter_id", "brand_id", "company_intro",
        "company_founded_year", "job_status_desc", "position_name",
        "recruitment_count", "latitude", "longitude",
        "is_proxy_job", "job_invalid", "address", "job_description",
        "skills_json", "labels_json", "welfare_json", "matched_queries_json",
        "source_rank", "raw_json", "collected_at",
        "first_seen_at", "last_seen_at",
    )

    # 重新采集时这些字段直接用新值覆盖（平台上的最新状态）。
    OVERWRITE_COLUMNS = (
        "job_url", "company_url", "title", "company_name", "city", "district",
        "business_district", "location_text", "salary_text", "salary_min_k",
        "salary_max_k", "salary_months", "experience", "education",
        "company_size", "financing_stage", "industry", "recruiter_name",
        "recruiter_title", "recruiter_active_status", "recruiter_active_days",
        "recruiter_online", "recruiter_certificated", "job_status_desc",
        "position_name", "recruitment_count", "is_proxy_job", "job_invalid",
        "source_rank", "raw_json", "collected_at",
    )

    # 这些字段只在新值非空时才覆盖，避免第二轮只抓列表就把已抓到的详情抹掉。
    KEEP_IF_EMPTY_COLUMNS = (
        "recruiter_id", "brand_id", "company_intro", "company_founded_year",
        "address", "latitude", "longitude",
    )

    def upsert(self, job: JobRecord) -> str:
        canonical_id = job.canonical_key()
        existing = self.conn.execute(
            "SELECT matched_queries_json, skills_json, welfare_json FROM jobs WHERE canonical_id = ?",
            (canonical_id,),
        ).fetchone()
        if existing:
            # 多关键词命中同一岗位时合并关键词，不是覆盖。
            job.matched_queries = merge_unique(
                json.loads(existing["matched_queries_json"] or "[]"), job.matched_queries
            )
            job.skills = merge_unique(json.loads(existing["skills_json"] or "[]"), job.skills)
            job.welfare = merge_unique(json.loads(existing["welfare_json"] or "[]"), job.welfare)
            status = "updated"
        else:
            status = "inserted"

        payload = job.model_dump(mode="json")
        now = payload["collected_at"]
        params = {
            "canonical_id": canonical_id,
            "platform": job.platform.value,
            "source_job_id": job.source_job_id,
            "job_url": job.job_url,
            "company_url": job.company_url,
            "title": job.title,
            "company_name": job.company_name,
            "city": job.city,
            "district": job.district,
            "business_district": job.business_district,
            "location_text": job.location_text,
            "salary_text": job.salary_text,
            "salary_min_k": job.salary_min_k,
            "salary_max_k": job.salary_max_k,
            "salary_months": job.salary_months,
            "experience": job.experience,
            "education": job.education,
            "company_size": job.company_size,
            "financing_stage": job.financing_stage,
            "industry": job.industry,
            "recruiter_name": job.recruiter_name,
            "recruiter_title": job.recruiter_title,
            "recruiter_active_status": job.recruiter_active_status,
            "recruiter_active_days": job.recruiter_active_days,
            "recruiter_online": int(job.recruiter_online),
            "recruiter_certificated": int(job.recruiter_certificated),
            "recruiter_id": job.recruiter_id,
            "brand_id": job.brand_id,
            "company_intro": job.company_intro,
            "company_founded_year": job.company_founded_year,
            "job_status_desc": job.job_status_desc,
            "position_name": job.position_name,
            "recruitment_count": job.recruitment_count,
            "latitude": job.latitude,
            "longitude": job.longitude,
            "is_proxy_job": int(job.is_proxy_job),
            "job_invalid": int(job.job_invalid),
            "address": job.address,
            "job_description": job.job_description,
            "skills_json": json.dumps(job.skills, ensure_ascii=False),
            "labels_json": json.dumps(job.labels, ensure_ascii=False),
            "welfare_json": json.dumps(job.welfare, ensure_ascii=False),
            "matched_queries_json": json.dumps(job.matched_queries, ensure_ascii=False),
            "source_rank": job.source_rank,
            "raw_json": json.dumps(payload.get("raw", {}), ensure_ascii=False),
            "collected_at": now,
            "first_seen_at": now,
            "last_seen_at": now,
        }

        columns = ", ".join(self.INSERT_COLUMNS)
        placeholders = ", ".join(f":{name}" for name in self.INSERT_COLUMNS)
        updates = [f"{name}=excluded.{name}" for name in self.OVERWRITE_COLUMNS]
        # 新值为空时保留旧值，防止只抓列表的轮次覆盖掉详情数据。
        updates += [
            f"{name}=CASE WHEN excluded.{name} IS NULL OR excluded.{name} = '' "
            f"THEN jobs.{name} ELSE excluded.{name} END"
            for name in self.KEEP_IF_EMPTY_COLUMNS
        ]
        # JD 保留更长的那份：第二轮若只抓列表，不该把已有 JD 清空。
        updates.append(
            "job_description=CASE WHEN length(excluded.job_description) > "
            "length(jobs.job_description) THEN excluded.job_description "
            "ELSE jobs.job_description END"
        )
        updates += [
            "skills_json=excluded.skills_json",
            "labels_json=excluded.labels_json",
            "welfare_json=excluded.welfare_json",
            "matched_queries_json=excluded.matched_queries_json",
            # first_seen_at 刻意不在更新列表里，保证首次发现时间永不丢失。
            "last_seen_at=excluded.last_seen_at",
            "seen_count=jobs.seen_count + 1",
            "updated_at=CURRENT_TIMESTAMP",
        ]

        self.conn.execute(
            f"INSERT INTO jobs ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(canonical_id) DO UPDATE SET {', '.join(updates)}",
            params,
        )
        group_id = self.cross_platform_group(job)
        self.conn.execute(
            "INSERT OR IGNORE INTO cross_platform_groups(group_id, canonical_id) VALUES (?, ?)",
            (group_id, canonical_id),
        )
        self.conn.commit()
        return status

    def record_run(self, config_label: str, stats_json: str) -> int:
        """记录一次采集运行，便于对比多轮之间的增量。"""
        cursor = self.conn.execute(
            "INSERT INTO collection_runs(config_label, stats_json) VALUES (?, ?)",
            (config_label, stats_json),
        )
        self.conn.commit()
        return int(cursor.lastrowid or 0)

    def record_chat_action(self, canonical_id: str, action: str, status: str, message: str = "") -> None:
        self.conn.execute(
            "INSERT INTO chat_actions(canonical_id, action, status, message) VALUES (?, ?, ?, ?)",
            (canonical_id, action, status, message),
        )
        self.conn.commit()

    def rows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM jobs ORDER BY city, company_name, title"))

    def export_csv(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        rows = self.rows()
        if not rows:
            out.write_text("", encoding="utf-8-sig")
            return out
        columns = rows[0].keys()
        with out.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(dict(row) for row in rows)
        return out

    def export_json(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        data = [dict(row) for row in self.rows()]
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return out

    def city_summary(self) -> list[dict]:
        query = """
        SELECT city,
               COUNT(*) AS jobs,
               COUNT(DISTINCT company_name) AS companies,
               ROUND(AVG((salary_min_k + salary_max_k) / 2.0), 2) AS avg_mid_salary_k,
               SUM(CASE WHEN experience IN ('应届生', '经验不限', '1年以内') THEN 1 ELSE 0 END) AS junior_jobs,
               SUM(CASE WHEN education IN ('', '不限', '大专', '本科') THEN 1 ELSE 0 END) AS bachelor_accessible
        FROM jobs
        GROUP BY city
        ORDER BY jobs DESC
        """
        return [dict(row) for row in self.conn.execute(query)]
