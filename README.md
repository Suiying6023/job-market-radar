# Job Market Radar

## 功能

- 连接本地已登录的 Chrome CDP 会话，读取岗位列表和详情（薪资、JD、公司信息、招聘者活跃度等）
- 按城市 + 关键词组合多次采集，增量入库去重（SQLite），支持多次运行合并结果
- 导出 CSV / JSON，生成本地 HTML 市场报告

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
playwright install chromium
```

启动一个独立的、已登录的浏览器：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_chrome_windows.ps1
```

复制一份 `config.example.yaml` 改成自己的城市/关键词，然后：

```powershell
job-radar doctor --config config.yaml
job-radar collect --config config.yaml
job-radar summary --config config.yaml
job-radar export --config config.yaml
job-radar report --config config.yaml
```

输出在 `data/`（SQLite）和 `output/`（CSV/JSON/HTML）。

## 说明

- 请求间隔较长，单次运行有请求量上限；遇到平台验证或异常提示会自动停止，不重试。
- 自动开聊功能默认关闭，需要显式开启并二次确认才会执行真实操作。

## 结构

```text
src/job_radar/
├── collectors/   平台适配器
├── analysis/     岗位分类与技能抽取
├── models.py     数据模型
├── storage.py    SQLite / CSV / JSON
├── runner.py     采集编排
├── report.py     HTML 报告
└── cli.py        命令行入口
```
