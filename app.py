from __future__ import annotations

import json
import os
import asyncio
import sqlite3
import threading
import time
import urllib.request
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parent.resolve()
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "app.db"
BROWSER_PROFILE = DATA_DIR / "xhs-browser-profile"
PLAYWRIGHT_BROWSERS = DATA_DIR / "ms-playwright"
HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8000"))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(PLAYWRIGHT_BROWSERS))


def ensure_db() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            create table if not exists settings (
                key text primary key,
                value text not null
            )
            """
        )
        con.execute(
            """
            create table if not exists items (
                id integer primary key autoincrement,
                platform text not null,
                title text not null,
                url text not null,
                author text,
                snippet text,
                tags text,
                raw_json text,
                collected_at integer not null,
                unique(platform, url)
            )
            """
        )
        con.execute(
            """
            create table if not exists analysis_runs (
                id integer primary key autoincrement,
                created_at integer not null,
                item_count integer not null,
                result_json text not null
            )
            """
        )


def db_get_settings() -> dict[str, str]:
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("select key, value from settings").fetchall()
    return {key: value for key, value in rows}


def db_save_settings(values: dict[str, str]) -> None:
    allowed = {"api_key", "base_url", "model"}
    with sqlite3.connect(DB_PATH) as con:
        for key, value in values.items():
            if key in allowed:
                con.execute(
                    "insert or replace into settings(key, value) values(?, ?)",
                    (key, value.strip()),
                )


def db_upsert_items(items: list[dict[str, Any]]) -> int:
    now = int(time.time())
    inserted = 0
    with sqlite3.connect(DB_PATH) as con:
        for item in items:
            title = clean_text(item.get("title")) or "未命名收藏"
            url = clean_text(item.get("url")) or f"local://unknown/{now}/{inserted}"
            raw = json.dumps(item, ensure_ascii=False)
            cur = con.execute(
                """
                insert or ignore into items(
                    platform, title, url, author, snippet, tags, raw_json, collected_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "xhs",
                    title[:240],
                    url[:600],
                    clean_text(item.get("author"))[:120],
                    clean_text(item.get("snippet"))[:1000],
                    json.dumps(item.get("tags") or [], ensure_ascii=False),
                    raw,
                    now,
                ),
            )
            inserted += cur.rowcount
    return inserted


def db_items(limit: int = 240) -> list[dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            select id, platform, title, url, author, snippet, tags, collected_at
            from items
            order by id desc
            limit ?
            """,
            (limit,),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["tags"] = json.loads(item.get("tags") or "[]")
        except json.JSONDecodeError:
            item["tags"] = []
        items.append(item)
    return items


def db_save_analysis(result: dict[str, Any], item_count: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "insert into analysis_runs(created_at, item_count, result_json) values(?, ?, ?)",
            (int(time.time()), item_count, json.dumps(result, ensure_ascii=False)),
        )


def db_latest_analysis() -> dict[str, Any] | None:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "select result_json from analysis_runs where item_count > 0 order by id desc limit 1"
        ).fetchone()
    if not row:
        return None
    return json.loads(row[0])


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\u200b", " ").split())


@dataclass
class BrowserState:
    playwright: Any = None
    context: Any = None
    page: Any = None


browser_state = BrowserState()


class AsyncWorker:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro: Any) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=120)


async_worker = AsyncWorker()


async def connect_xhs_browser() -> dict[str, Any]:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        return {
            "ok": False,
            "error": "缺少 Playwright。请先运行：pip install -r requirements.txt，然后运行：python -m playwright install chromium",
            "detail": str(exc),
        }

    if browser_state.context and browser_state.page:
        await browser_state.page.bring_to_front()
        return {"ok": True, "message": "小红书浏览器已经打开。"}

    BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    browser_state.playwright = await async_playwright().start()
    browser_state.context = await browser_state.playwright.chromium.launch_persistent_context(
        str(BROWSER_PROFILE),
        headless=False,
        viewport={"width": 1320, "height": 900},
        locale="zh-CN",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    browser_state.page = browser_state.context.pages[0] if browser_state.context.pages else await browser_state.context.new_page()
    await browser_state.page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded")
    await browser_state.page.bring_to_front()
    return {"ok": True, "message": "浏览器已打开。请登录小红书，并进入收藏页。"}


async def scrape_xhs_current_page() -> dict[str, Any]:
    if not browser_state.page:
        return {"ok": False, "error": "还没有连接小红书。请先点击“连接小红书”。"}

    page = browser_state.page
    await page.bring_to_front()
    collected: list[dict[str, Any]] = []

    for _ in range(8):
        batch = await page.evaluate(
            """
            () => {
              const anchors = Array.from(document.querySelectorAll('a[href]'));
              const cards = [];
              for (const a of anchors) {
                const href = a.href || '';
                const box = a.closest('section, article, div') || a;
                const text = (box.innerText || a.innerText || '').replace(/\\s+/g, ' ').trim();
                if (!href.includes('xiaohongshu.com')) continue;
                if (!text || text.length < 2) continue;
                const title = text.split(' ').slice(0, 18).join(' ');
                cards.push({
                  title,
                  url: href.split('?')[0],
                  author: '',
                  snippet: text.slice(0, 500),
                  tags: []
                });
              }
              const seen = new Set();
              return cards.filter((x) => {
                const key = x.url + '|' + x.title;
                if (seen.has(key)) return false;
                seen.add(key);
                return true;
              }).slice(0, 80);
            }
            """
        )
        collected.extend(batch)
        await page.mouse.wheel(0, 1600)
        await page.wait_for_timeout(900)

    deduped: dict[str, dict[str, Any]] = {}
    for item in collected:
        key = item.get("url") or item.get("title")
        if key and key not in deduped:
            deduped[key] = item
    items = list(deduped.values())[:160]
    inserted = db_upsert_items(items)
    return {
        "ok": True,
        "found": len(items),
        "inserted": inserted,
        "message": f"读取到 {len(items)} 条候选内容，新增 {inserted} 条。",
    }


THEME_KEYWORDS = {
    "变美穿搭": ["穿搭", "妆", "护肤", "发型", "显瘦", "变美", "香水", "口红", "裙", "ootd"],
    "健身身材": ["健身", "减脂", "塑形", "马甲线", "体脂", "瑜伽", "普拉提", "训练", "身材"],
    "美食探店": ["美食", "探店", "咖啡", "甜品", "餐厅", "火锅", "烘焙", "食谱", "做饭"],
    "家居装修": ["装修", "家居", "收纳", "卧室", "客厅", "改造", "租房", "软装"],
    "旅行攻略": ["旅行", "旅游", "攻略", "周末", "酒店", "民宿", "路线", "Citywalk"],
    "搞钱副业": ["副业", "搞钱", "赚钱", "变现", "创业", "简历", "面试", "职场", "收入"],
    "学习成长": ["学习", "读书", "英语", "课程", "效率", "笔记", "自律", "计划", "复盘"],
    "情绪疗愈": ["焦虑", "疗愈", "松弛", "治愈", "情绪", "内耗", "独处", "冥想"],
    "恋爱关系": ["恋爱", "暧昧", "约会", "亲密", "关系", "伴侣", "脱单", "聊天"],
    "感官吸引力": ["美女", "帅哥", "身材", "氛围感", "擦边", "心动", "纯欲", "性感", "写真"],
    "数码工具": ["AI", "软件", "工具", "数码", "效率神器", "插件", "教程"],
}

MOTIVE_KEYWORDS = {
    "想变好": ["自律", "计划", "复盘", "成长", "提升", "坚持"],
    "想变美": ["穿搭", "妆", "护肤", "发型", "显瘦", "变美"],
    "想变有钱": ["搞钱", "副业", "赚钱", "收入", "变现", "创业"],
    "想变松弛": ["松弛", "疗愈", "治愈", "焦虑", "内耗", "独处"],
    "想被喜欢": ["恋爱", "约会", "暧昧", "心动", "聊天", "吸引"],
    "想拥有理想生活": ["家居", "装修", "旅行", "咖啡", "周末", "生活方式"],
    "想获得刺激": ["心动", "性感", "氛围感", "多巴胺", "爽", "上头"],
}


def count_keywords(text: str, keywords: list[str]) -> int:
    text_lower = text.lower()
    return sum(1 for kw in keywords if kw.lower() in text_lower)


def analyze_by_rules(items: list[dict[str, Any]]) -> dict[str, Any]:
    corpus = " ".join(
        clean_text(x.get("title")) + " " + clean_text(x.get("snippet")) for x in items
    )
    n = max(len(items), 1)

    theme_scores = {
        theme: count_keywords(corpus, kws) for theme, kws in THEME_KEYWORDS.items()
    }
    motive_scores = {
        motive: count_keywords(corpus, kws) for motive, kws in MOTIVE_KEYWORDS.items()
    }

    top_themes = [x[0] for x in sorted(theme_scores.items(), key=lambda p: p[1], reverse=True)[:3] if x[1] > 0]
    top_motives = [x[0] for x in sorted(motive_scores.items(), key=lambda p: p[1], reverse=True)[:3] if x[1] > 0]
    if not top_themes:
        top_themes = ["生活方式", "兴趣探索", "情绪补给"]
    if not top_motives:
        top_motives = ["想拥有理想生活", "想变好", "想获得确定感"]

    action_terms = ["教程", "步骤", "清单", "攻略", "计划", "方法", "复盘", "模板"]
    consumption_terms = ["种草", "好物", "平价", "必买", "探店", "同款", "推荐", "购物"]
    sensory_terms = THEME_KEYWORDS["感官吸引力"] + ["好看", "漂亮", "帅", "美", "氛围"]
    growth_terms = ["学习", "健身", "自律", "副业", "搞钱", "效率", "读书", "成长"]
    social_terms = ["恋爱", "关系", "聊天", "朋友", "社交", "约会", "脱单", "伴侣"]

    action = min(100, 35 + count_keywords(corpus, action_terms) * 7)
    focus = min(100, 35 + (max(theme_scores.values() or [0]) * 11) - max(0, len([v for v in theme_scores.values() if v > 0]) - 4) * 4)
    consumption = min(100, 30 + count_keywords(corpus, consumption_terms) * 8)
    sensory = min(100, 25 + count_keywords(corpus, sensory_terms) * 7)
    growth = min(100, 30 + count_keywords(corpus, growth_terms) * 7)
    social = min(100, 25 + count_keywords(corpus, social_terms) * 8)

    dimensions = {
        "行动性": max(10, action),
        "聚焦度": max(10, focus),
        "消费性": max(10, consumption),
        "感官性": max(10, sensory),
        "自我提升": max(10, growth),
        "社交关系": max(10, social),
    }

    code = "".join(
        [
            "做" if dimensions["行动性"] >= 58 else "梦",
            "钻" if dimensions["聚焦度"] >= 58 else "散",
            "买" if dimensions["消费性"] >= dimensions["自我提升"] else "学",
            "稳" if dimensions["行动性"] + dimensions["自我提升"] >= dimensions["感官性"] + dimensions["消费性"] else "爽",
        ]
    ) + "型"

    label = pick_persona_label(code, top_themes, dimensions)
    support_tags = pick_support_tags(code, top_themes, top_motives, dimensions)

    safe_summary = (
        f"你是{label}，收藏夹正在认真搭建一个“{top_themes[0]} + {top_motives[0]}”版本的理想人生。"
        f"系统看完以后认为：你不是没方向，你是方向太会发光了。"
    )
    private_summary = (
        f"你的收藏偏向{', '.join(top_themes)}，隐藏动机更像是{', '.join(top_motives)}。"
        f"最大卡点可能是：收藏动作已经提前给了你一点完成感。"
    )

    plan = [
        "从收藏里挑 1 条最容易执行的内容，只做 10 分钟。",
        f"把“{top_themes[0]}”相关收藏删到只剩 5 条最想做的。",
        "选一个不需要买东西的动作，今天直接完成。",
        "给未来 3 天各安排一个小任务，不超过 15 分钟。",
        "打开一条旧收藏，判断它是还想做，还是只想留个念想。",
        "把一个收藏内容转成现实清单，比如穿搭、路线、菜单或训练动作。",
        "写一句复盘：我到底是在想变好，还是在用收藏安慰自己。",
    ]

    return {
        "item_count": len(items),
        "persona_label": label,
        "persona_code": code,
        "support_tags": support_tags,
        "top_themes": top_themes,
        "top_motives": top_motives,
        "dimensions": dimensions,
        "safe_summary": safe_summary,
        "private_summary": private_summary,
        "action_plan": plan,
        "generated_by": "local-rules",
    }


def pick_persona_label(code: str, themes: list[str], dimensions: dict[str, int]) -> str:
    if dimensions["感官性"] >= 60:
        return "审美雷达过于灵敏型人格"
    if "搞钱副业" in themes:
        return "副业收藏型 CEO"
    if "学习成长" in themes and code.startswith("梦"):
        return "间歇性自律幻想家"
    if "家居装修" in themes:
        return "理想生活样板间管理员"
    if "旅行攻略" in themes:
        return "周末逃离计划收藏家"
    if code.startswith("做钻"):
        return "收藏夹里的真行动派"
    if code.startswith("梦散"):
        return "人生重启计划收藏家"
    return "赛博许愿池管理员"


def pick_support_tags(code: str, themes: list[str], motives: list[str], dimensions: dict[str, int]) -> list[str]:
    tags = []
    if code.startswith("梦"):
        tags.append("收藏很多，执行随缘")
    else:
        tags.append("有把收藏变成清单的潜力")
    if "散" in code:
        tags.append("理想人生分支过多")
    if dimensions["感官性"] >= 55:
        tags.append("收藏夹里有一些不方便展开的美学坚持")
    if dimensions["消费性"] >= 55:
        tags.append("嘴上断舍离，收藏夹很诚实")
    if dimensions["自我提升"] >= 55:
        tags.append("每天都想重启人生")
    tags.append(f"核心执念：{themes[0]}")
    tags.append(f"隐藏愿望：{motives[0]}")
    return tags[:6]


def call_ai_if_configured(result: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    settings = db_get_settings()
    api_key = settings.get("api_key", "").strip()
    base_url = settings.get("base_url", "https://api.openai.com/v1").strip().rstrip("/")
    model = settings.get("model", "gpt-4o-mini").strip()
    if not api_key:
        return result

    compact_items = [
        {"title": x.get("title"), "snippet": x.get("snippet")}
        for x in items[:80]
    ]
    prompt = {
        "task": "基于规则分析结果，润色成搞笑、体面、适合截图分享的小红书收藏夹人格报告。不要羞辱用户，敏感/擦边内容用审美、氛围感、多巴胺等体面说法。",
        "rules_result": result,
        "sample_items": compact_items,
        "output_json_schema": {
            "persona_label": "string",
            "support_tags": ["string"],
            "safe_summary": "string",
            "private_summary": "string",
            "action_plan": ["string", "string", "string", "string", "string", "string", "string"],
        },
    }
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一个中文产品里的幽默人格报告生成器，只输出 JSON。"},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            "temperature": 0.8,
            "response_format": {"type": "json_object"},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        ai = json.loads(content)
        merged = {**result, **{k: v for k, v in ai.items() if v}}
        merged["generated_by"] = "ai"
        return merged
    except Exception as exc:
        result["ai_error"] = f"AI 调用失败，已使用本地规则兜底：{exc}"
        return result


def generate_analysis() -> dict[str, Any]:
    items = db_items(limit=300)
    if not items:
        raise ValueError("还没有收藏数据。请先连接小红书并读取收藏。")
    result = analyze_by_rules(items)
    result = call_ai_if_configured(result, items)
    db_save_analysis(result, len(items))
    return result


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>收藏夹人格分析</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #1f2933;
      --muted: #65758b;
      --line: #d9e2ec;
      --bg: #f7f9fb;
      --panel: #ffffff;
      --accent: #e14d72;
      --accent-2: #147d64;
      --accent-3: #3d5afe;
      --soft: #fff1f4;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--ink); }
    button, input { font: inherit; }
    .shell { max-width: 1160px; margin: 0 auto; padding: 24px; }
    .top { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 18px; }
    h1 { margin: 0; font-size: 30px; line-height: 1.15; letter-spacing: 0; }
    .sub { color: var(--muted); margin-top: 8px; }
    .status { display: inline-flex; align-items: center; min-height: 36px; padding: 0 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); color: var(--muted); white-space: nowrap; }
    .actions { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 20px 0; }
    .btn { border: 0; border-radius: 8px; padding: 14px 16px; background: var(--ink); color: white; cursor: pointer; min-height: 52px; }
    .btn.secondary { background: var(--accent); }
    .btn.tertiary { background: var(--accent-2); }
    .btn.ghost { background: var(--panel); color: var(--ink); border: 1px solid var(--line); }
    .btn:disabled { opacity: .55; cursor: not-allowed; }
    .grid { display: grid; grid-template-columns: 380px 1fr; gap: 16px; align-items: start; }
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; }
    .panel h2 { margin: 0 0 12px; font-size: 18px; }
    .settings { display: none; margin-bottom: 16px; }
    .settings.open { display: block; }
    label { display: block; color: var(--muted); font-size: 13px; margin: 10px 0 6px; }
    input { width: 100%; border: 1px solid var(--line); border-radius: 8px; padding: 10px 11px; background: white; color: var(--ink); }
    .items { display: grid; gap: 8px; max-height: 520px; overflow: auto; padding-right: 4px; }
    .item { border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: #fbfdff; }
    .item-title { font-weight: 700; line-height: 1.35; }
    .item-snippet { color: var(--muted); font-size: 13px; margin-top: 5px; line-height: 1.45; }
    .report { display: none; }
    .report.show { display: block; }
    .hero-report { background: linear-gradient(135deg, #fff6f0, #eef7ff); border: 1px solid var(--line); border-radius: 8px; padding: 22px; margin-bottom: 14px; }
    .persona { font-size: 34px; line-height: 1.15; margin: 0 0 8px; letter-spacing: 0; }
    .code { display: inline-flex; padding: 6px 10px; border-radius: 8px; background: var(--ink); color: white; font-weight: 700; }
    .chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
    .chip { padding: 7px 10px; border-radius: 8px; background: var(--soft); color: #96324f; border: 1px solid #ffd6df; }
    .cols { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .score { display: grid; grid-template-columns: 82px 1fr 44px; align-items: center; gap: 8px; margin: 10px 0; }
    .bar { height: 10px; background: #eef2f7; border-radius: 999px; overflow: hidden; }
    .fill { height: 100%; background: var(--accent-3); border-radius: 999px; }
    .summary { font-size: 16px; line-height: 1.7; color: #354052; }
    details { margin-top: 12px; }
    summary { cursor: pointer; color: var(--muted); }
    ol { padding-left: 22px; line-height: 1.7; }
    .notice { color: var(--muted); line-height: 1.55; }
    .error { color: #b42318; }
    @media (max-width: 860px) {
      .top { align-items: flex-start; flex-direction: column; }
      .actions, .grid, .cols { grid-template-columns: 1fr; }
      .shell { padding: 16px; }
      .persona { font-size: 27px; }
      .status { white-space: normal; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="top">
      <div>
        <h1>收藏夹人格分析</h1>
        <div class="sub">本地读取你选择暴露的小红书页面，生成一份可截图分享的画像报告。</div>
      </div>
      <div class="status" id="status">准备中</div>
    </section>

    <section class="actions">
      <button class="btn" id="connectBtn">连接小红书</button>
      <button class="btn secondary" id="scrapeBtn">读取收藏</button>
      <button class="btn tertiary" id="analyzeBtn">生成画像</button>
    </section>

    <section class="panel settings" id="settingsPanel">
      <h2>AI 设置</h2>
      <div class="notice">不填写也能用本地规则生成报告；填写后会用你自己的 OpenAI-compatible 接口润色。</div>
      <label>API Key</label>
      <input id="apiKey" type="password" placeholder="sk-..." />
      <label>Base URL</label>
      <input id="baseUrl" placeholder="https://api.openai.com/v1" />
      <label>Model</label>
      <input id="model" placeholder="gpt-4o-mini" />
      <div style="margin-top:12px; display:flex; gap:10px; flex-wrap:wrap;">
        <button class="btn ghost" id="saveSettingsBtn">保存设置</button>
        <button class="btn ghost" id="closeSettingsBtn">收起</button>
      </div>
    </section>

    <div style="margin-bottom:14px;">
      <button class="btn ghost" id="settingsBtn">AI 设置</button>
    </div>

    <section class="grid">
      <aside class="panel">
        <h2>收藏预览</h2>
        <div class="notice" id="itemHint">连接后进入小红书收藏页，再读取当前页面内容。</div>
        <div class="items" id="items"></div>
      </aside>

      <section class="panel">
        <h2>画像报告</h2>
        <div id="emptyReport" class="notice">生成后这里会出现你的收藏夹人格、四轴代码、标签和 7 天计划。</div>
        <div class="report" id="report">
          <div class="hero-report">
            <h3 class="persona" id="personaLabel"></h3>
            <span class="code" id="personaCode"></span>
            <div class="chips" id="supportTags"></div>
          </div>
          <div class="cols">
            <div class="panel">
              <h2>兴趣领域</h2>
              <div class="chips" id="themes"></div>
            </div>
            <div class="panel">
              <h2>隐藏动机</h2>
              <div class="chips" id="motives"></div>
            </div>
          </div>
          <div class="panel" style="margin-top:12px;">
            <h2>六维分数</h2>
            <div id="dimensions"></div>
          </div>
          <div class="panel" style="margin-top:12px;">
            <h2>分享安全版总结</h2>
            <div class="summary" id="safeSummary"></div>
            <details>
              <summary>展开私密扎心版</summary>
              <div class="summary" id="privateSummary"></div>
            </details>
          </div>
          <div class="panel" style="margin-top:12px;">
            <h2>7 天轻量计划</h2>
            <ol id="plan"></ol>
          </div>
        </div>
      </section>
    </section>
  </main>

  <script>
    const $ = (id) => document.getElementById(id);
    const state = { busy: false };

    function setStatus(text, isError=false) {
      $('status').textContent = text;
      $('status').classList.toggle('error', isError);
    }

    async function api(path, options = {}) {
      const res = await fetch(path, {
        headers: { 'Content-Type': 'application/json' },
        ...options
      });
      const data = await res.json();
      if (!res.ok || data.ok === false) throw new Error(data.error || '请求失败');
      return data;
    }

    function renderItems(items) {
      $('items').innerHTML = '';
      $('itemHint').textContent = items.length ? `已保存 ${items.length} 条内容。` : '还没有收藏数据。';
      for (const item of items.slice(0, 80)) {
        const div = document.createElement('div');
        div.className = 'item';
        div.innerHTML = `<div class="item-title"></div><div class="item-snippet"></div>`;
        div.querySelector('.item-title').textContent = item.title || '未命名收藏';
        div.querySelector('.item-snippet').textContent = item.snippet || item.url || '';
        $('items').appendChild(div);
      }
    }

    function chipList(id, values) {
      $(id).innerHTML = '';
      for (const value of values || []) {
        const span = document.createElement('span');
        span.className = 'chip';
        span.textContent = value;
        $(id).appendChild(span);
      }
    }

    function renderReport(r) {
      $('emptyReport').style.display = 'none';
      $('report').classList.add('show');
      $('personaLabel').textContent = r.persona_label;
      $('personaCode').textContent = r.persona_code;
      chipList('supportTags', r.support_tags);
      chipList('themes', r.top_themes);
      chipList('motives', r.top_motives);
      $('safeSummary').textContent = r.safe_summary;
      $('privateSummary').textContent = r.private_summary;
      $('dimensions').innerHTML = '';
      for (const [name, score] of Object.entries(r.dimensions || {})) {
        const row = document.createElement('div');
        row.className = 'score';
        row.innerHTML = `<div></div><div class="bar"><div class="fill"></div></div><div></div>`;
        row.children[0].textContent = name;
        row.querySelector('.fill').style.width = `${Math.max(0, Math.min(100, score))}%`;
        row.children[2].textContent = score;
        $('dimensions').appendChild(row);
      }
      $('plan').innerHTML = '';
      for (const task of r.action_plan || []) {
        const li = document.createElement('li');
        li.textContent = task;
        $('plan').appendChild(li);
      }
      setStatus(`已生成报告：${r.item_count || 0} 条内容，${r.generated_by === 'ai' ? 'AI 润色' : '本地规则'}。`);
      if (r.ai_error) setStatus(r.ai_error, true);
    }

    async function refresh() {
      const data = await api('/api/status');
      renderItems(data.items || []);
      if (data.analysis) renderReport(data.analysis);
      setStatus(data.message || '准备就绪');
      $('baseUrl').value = data.settings?.base_url || 'https://api.openai.com/v1';
      $('model').value = data.settings?.model || 'gpt-4o-mini';
    }

    $('settingsBtn').onclick = () => $('settingsPanel').classList.toggle('open');
    $('closeSettingsBtn').onclick = () => $('settingsPanel').classList.remove('open');

    $('saveSettingsBtn').onclick = async () => {
      try {
        setStatus('正在保存设置...');
        await api('/api/settings', {
          method: 'POST',
          body: JSON.stringify({
            api_key: $('apiKey').value,
            base_url: $('baseUrl').value,
            model: $('model').value
          })
        });
        $('apiKey').value = '';
        setStatus('设置已保存。');
      } catch (err) {
        setStatus(err.message, true);
      }
    };

    $('connectBtn').onclick = async () => {
      try {
        setStatus('正在打开小红书浏览器...');
        const data = await api('/api/connect-xhs', { method: 'POST', body: '{}' });
        setStatus(data.message || '浏览器已打开。');
      } catch (err) {
        setStatus(err.message, true);
      }
    };

    $('scrapeBtn').onclick = async () => {
      try {
        setStatus('正在读取当前小红书页面...');
        const data = await api('/api/scrape-xhs', { method: 'POST', body: '{}' });
        setStatus(data.message);
        await refresh();
      } catch (err) {
        setStatus(err.message, true);
      }
    };

    $('analyzeBtn').onclick = async () => {
      try {
        setStatus('正在生成画像...');
        const data = await api('/api/analyze', { method: 'POST', body: '{}' });
        renderReport(data.result);
      } catch (err) {
        setStatus(err.message, true);
      }
    };

    refresh().catch(err => setStatus(err.message, true));
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            self.send_text(INDEX_HTML, "text/html; charset=utf-8")
            return
        if self.path == "/api/status":
            self.send_json(
                {
                    "ok": True,
                    "message": "准备就绪",
                    "items": db_items(),
                    "analysis": db_latest_analysis(),
                    "settings": {
                        "base_url": db_get_settings().get("base_url", "https://api.openai.com/v1"),
                        "model": db_get_settings().get("model", "gpt-4o-mini"),
                    },
                }
            )
            return
        self.send_json({"ok": False, "error": "Not found"}, status=404)

    def do_POST(self) -> None:
        if self.path == "/api/settings":
            payload = self.read_json()
            db_save_settings(payload)
            self.send_json({"ok": True})
            return
        if self.path == "/api/connect-xhs":
            self.send_json(run_async(connect_xhs_browser()))
            return
        if self.path == "/api/scrape-xhs":
            self.send_json(run_async(scrape_xhs_current_page()))
            return
        if self.path == "/api/analyze":
            try:
                result = generate_analysis()
                self.send_json({"ok": True, "result": result})
            except ValueError as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        self.send_json({"ok": False, "error": "Not found"}, status=404)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw or "{}")

    def send_text(self, text: str, content_type: str, status: int = 200) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, value: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[server] {self.address_string()} {fmt % args}")


def run_async(coro: Any) -> Any:
    return async_worker.run(coro)


def open_browser_later(url: str) -> None:
    def _open() -> None:
        time.sleep(0.8)
        webbrowser.open(url)

    threading.Thread(target=_open, daemon=True).start()


def main() -> None:
    ensure_db()
    url = f"http://{HOST}:{PORT}"
    open_browser_later(url)
    print(f"收藏夹人格分析已启动：{url}")
    print("按 Ctrl+C 停止。")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
