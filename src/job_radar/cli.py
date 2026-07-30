from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.request import urlopen

import typer
from rich.console import Console
from rich.table import Table

from .config import load_config
from .report import generate_html_report
from .runner import run_collection
from .storage import JobStore


app = typer.Typer(no_args_is_help=True, help="国内 AI 岗位聚合与可选自动开聊工具")
console = Console()


@app.command()
def doctor(config: str = "config.yaml") -> None:
    """检查配置、数据库目录和 CDP 地址。"""
    cfg = load_config(config)
    console.print(f"配置有效：{len(cfg.cities)} 个城市，{len(cfg.keywords)} 个关键词")
    console.print(f"浏览器模式：{cfg.browser.mode.value}")
    console.print(f"自动开聊：enabled={cfg.auto_chat.enabled}, dry_run={cfg.auto_chat.dry_run}")
    if cfg.browser.mode.value == "cdp":
        try:
            endpoint = cfg.browser.cdp_url.rstrip("/") + "/json/version"
            with urlopen(endpoint, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            console.print(f"CDP 可用：{payload.get('Browser', 'Chromium')}" )
        except Exception as exc:
            console.print(f"[yellow]CDP 暂不可用：{exc}。请先运行独立浏览器启动脚本。[/]")
    if cfg.auto_chat.enabled and not cfg.auto_chat.dry_run:
        console.print("[bold yellow]警告：配置请求真实自动开聊；collect 仍要求额外传入 --allow-live-chat。[/]")


@app.command()
def collect(
    config: str = "config.yaml",
    allow_live_chat: bool = typer.Option(False, "--allow-live-chat", help="允许执行真实开聊；不传时真实开聊配置会被拒绝"),
) -> None:
    """运行采集任务。"""
    cfg = load_config(config)
    if cfg.auto_chat.enabled and not cfg.auto_chat.dry_run and not allow_live_chat:
        raise typer.BadParameter(
            "配置已启用真实自动开聊，但未传入 --allow-live-chat。请先使用 dry_run。"
        )
    stats = asyncio.run(run_collection(cfg))
    console.print(stats.model_dump_json(indent=2))


@app.command()
def export(
    config: str = "config.yaml",
    csv_path: str = "output/jobs.csv",
    json_path: str = "output/jobs.json",
) -> None:
    """从 SQLite 导出 CSV 和 JSON。"""
    cfg = load_config(config)
    with JobStore(cfg.database_path) as store:
        console.print(f"CSV: {store.export_csv(csv_path)}")
        console.print(f"JSON: {store.export_json(json_path)}")


@app.command()
def report(config: str = "config.yaml", output: str = "output/report.html") -> None:
    """生成本地 HTML 市场报告。"""
    cfg = load_config(config)
    path = generate_html_report(cfg.database_path, output)
    console.print(f"报告已生成: {path}")


@app.command(name="summary")
def summary(config: str = "config.yaml") -> None:
    """显示城市汇总。"""
    cfg = load_config(config)
    with JobStore(cfg.database_path) as store:
        rows = store.city_summary()
    table = Table("城市", "岗位数", "公司数", "平均薪资中点(K)", "初级岗位", "本科可投")
    for row in rows:
        table.add_row(
            str(row["city"]), str(row["jobs"]), str(row["companies"]),
            str(row["avg_mid_salary_k"] or ""), str(row["junior_jobs"]), str(row["bachelor_accessible"]),
        )
    console.print(table)


if __name__ == "__main__":
    app()
