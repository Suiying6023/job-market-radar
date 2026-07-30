from job_radar.normalization import parse_salary, source_id_from_url


def test_parse_salary_k_months():
    assert parse_salary("20-35K·14薪") == (20.0, 35.0, 14)


def test_parse_salary_wan():
    assert parse_salary("2-3万") == (20.0, 30.0, None)


def test_source_id_from_url():
    assert source_id_from_url("https://www.zhipin.com/job_detail/abc123.html") == "abc123"
