"""豆瓣榜单订阅：定期扫描豆瓣电影/剧集榜单，新上榜且评分达标的作品自动创建订阅。

和 MoviePilot 的 doubanrank 插件做的事一样，但数据源换成 MovieClaw 内置的
Discover 片单（``discover.browse-collection``），不依赖外部 RSSHub：

- 用户在「设置」里勾选要盯的豆瓣榜单（电影、剧集、综艺都有）、设定评分门槛；
- 每轮把榜单里的作品交给宿主操作建订阅（``subscriptions.create``），
  已处理过的自动跳过；
- 榜单里是剧集时默认只追以后播出的新集（自动续订），不替用户猜季号；
  想自己勾季就在设置里改成「记入待确认」；
- 豆瓣条目对应多部 TMDB 作品时记进 ``pending``，留给用户在订阅页确认。

周期由定时任务控制（出现在「设置 → 定时任务」），可暂停、可立即运行。
"""

import asyncio
import datetime
import json
import re
import time
import xml.etree.ElementTree
from enum import StrEnum
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from movieclaw_api.plugins.keys import HOST_OPS, PLUGIN_DATA, PLUGIN_ROUTES
from movieclaw_scheduler import SCHEDULED_TASKS, TaskDefinition, TriggerType
from movieclaw_sdk import Context, net, plugin
from movieclaw_sdk.ops import OpsError

PLUGIN_ID = "douban-rank-subscribe"
TASK_KEY = f"{PLUGIN_ID}.refresh"

# 可勾选的豆瓣榜单：(配置字段名, 中文标题, collection_ref)。
# collection_ref 必须来自 discover.list-collections 的返回。
#
# 榜单的勾选在插件网页（左侧栏「豆瓣榜单」）里做，设置存在插件数据里，
# 不经过原生设置表单。名单固定，新增榜单只需往下面加一行。
RANK_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("movie_weekly_best", "豆瓣一周口碑电影榜", "douban:movie:movie_weekly_best"),
    ("movie_hot_gaia", "豆瓣热门电影", "douban:movie:movie_hot_gaia"),
    ("movie_showing", "影院热映", "douban:movie:movie_showing"),
    ("movie_soon", "即将上映", "douban:movie:movie_soon"),
    ("movie_high_score", "豆瓣高分电影", "douban:movie:movie_high_score"),
    ("movie_top250", "豆瓣电影 Top 250", "douban:movie:movie_top250"),
    ("movie_classic", "经典电影", "douban:movie:movie_classic"),
    ("movie_scifi", "高分经典科幻片榜", "douban:movie:movie_scifi"),
    ("movie_comedy", "高分经典喜剧片榜", "douban:movie:movie_comedy"),
    ("movie_action", "高分经典动作片榜", "douban:movie:movie_action"),
    ("movie_love", "高分经典爱情片榜", "douban:movie:movie_love"),
    ("tv_real_time_hotest", "豆瓣实时热门剧集", "douban:tv:tv_real_time_hotest"),
    ("tv_chinese_best_weekly", "华语口碑剧集榜", "douban:tv:tv_chinese_best_weekly"),
    ("tv_global_best_weekly", "全球口碑剧集榜", "douban:tv:tv_global_best_weekly"),
    ("tv_hot", "近期热门剧集", "douban:tv:tv_hot"),
    ("tv_domestic", "近期热门国产剧", "douban:tv:tv_domestic"),
    ("tv_american", "近期热门美剧", "douban:tv:tv_american"),
    ("tv_japanese", "近期热门日剧", "douban:tv:tv_japanese"),
    ("tv_korean", "近期热门韩剧", "douban:tv:tv_korean"),
    ("tv_animation", "近期热门动画", "douban:tv:tv_animation"),
    ("show_domestic", "近期热门国内综艺", "douban:tv:show_domestic"),
    ("show_foreign", "近期热门国外综艺", "douban:tv:show_foreign"),
)


class TvStrategy(StrEnum):
    FOLLOW_NEW = "follow_new"
    PENDING = "pending"


#: 榜单开关在插件数据里的键（网页端唯一入口，后端设置表单不再出现）
SETTINGS_KEY = "settings"

#: 面板设置的默认值与取值范围（网页端与后端共用同一份口径）
DEFAULTS: dict[str, Any] = {
    "auto_subscribe": True,
    "min_rating": 7.0,
    "per_rank_limit": 30,
    "tv_strategy": "follow_new",
    "custom_feeds": "",
}


class PanelSettings(BaseModel):
    """网页端「设置」抽屉保存的内容；存在插件数据里，不经过原生设置表单。

    榜单开关用 ``rank_<field>`` 命名（见 RANK_CHOICES），没给的当关。
    """

    model_config = {"extra": "allow"}   # 榜单开关是动态拼出来的键，放行

    auto_subscribe: bool = True
    min_rating: float = Field(default=7.0, ge=0, le=10)
    per_rank_limit: int = Field(default=30, ge=1, le=500)
    tv_strategy: TvStrategy = Field(default=TvStrategy.FOLLOW_NEW)
    custom_feeds: str = Field(default="")


def normalize_panel(raw: Any) -> dict[str, Any]:
    """把存储里的值整理成一份完整、可信的设置（缺项补默认、类型不对回退）。"""
    data = dict(raw) if isinstance(raw, dict) else {}
    out: dict[str, Any] = dict(DEFAULTS)
    out["auto_subscribe"] = data.get("auto_subscribe") is not False   # 缺省视为开
    try:
        out["min_rating"] = min(10.0, max(0.0, float(data.get("min_rating", DEFAULTS["min_rating"]))))
    except (TypeError, ValueError):
        pass
    try:
        limit = int(data.get("per_rank_limit", DEFAULTS["per_rank_limit"]))
        out["per_rank_limit"] = min(500, max(1, limit))
    except (TypeError, ValueError):
        pass
    strategy = str(data.get("tv_strategy") or "").strip()
    out["tv_strategy"] = strategy if strategy in ("follow_new", "pending") else DEFAULTS["tv_strategy"]
    feeds = data.get("custom_feeds")
    out["custom_feeds"] = feeds if isinstance(feeds, str) else ""
    for field, _title, _ref in RANK_CHOICES:
        out[f"rank_{field}"] = data.get(f"rank_{field}") is True
    return out


def enabled_ranks_from(raw: Any) -> list[str]:
    """一份面板设置里勾选的榜单引用，按 RANK_CHOICES 顺序。"""
    data = raw if isinstance(raw, dict) else {}
    return [ref for field, _t, ref in RANK_CHOICES if data.get(f"rank_{field}") is True]


class SubscribeRequest(BaseModel):
    """网页卡片点「订阅」时提交的内容。"""

    title_ref: str = Field(min_length=1, max_length=160)
    media_type: str | None = Field(default=None, max_length=16)


class Settings(BaseModel):
    """插件启动时读到的运行设置：只用来接住原生设置里可能残留的旧值。

    正常的读写都走网页端的设置接口（存插件数据），所以这里不再声明那些字段，
    原生「设置」表单也就不会出现任何可编辑项。
    """

    model_config = {"extra": "ignore"}


def pick_new(
    titles: list[dict],
    seen: set[str],
    min_rating: float,
) -> list[dict]:
    """从榜单条目的原始数据里挑出要处理的作品；保持榜单顺序。

    过滤规则（纯函数，便于自测）：
    - 已经在库里、或已经处理过的引用跳过；
    - 引用格式不对（不是 douban:xxxx）跳过；
    - 有评分且低于门槛的跳过；没有评分（0 或缺失）时不因此淘汰。
    """
    picked: list[dict] = []
    for item in titles:
        ref = str(item.get("title_ref") or "")
        if not ref or not ref.startswith("douban:"):
            continue
        if ref in seen:
            continue
        rating = item.get("provider_rating")
        # 0 / 缺失 / 非数字都表示来源没有给出评分，不拿它当「低分」淘汰
        if min_rating > 0 and isinstance(rating, (int, float)) and rating > 0 and rating < min_rating:
            continue
        picked.append(item)
    return picked


def describe(item: dict) -> str:
    """日志里用的《片名 (年份)》。"""
    title = str(item.get("title") or "未知")
    year = item.get("release_year")
    return f"《{title}》" + (f"（{year}）" if year else "")


# ---------------------------------------------------------------------------
# 自定义榜单：用户自己填地址，正文格式自动识别
# ---------------------------------------------------------------------------

#: 从链接里取豆瓣 ID：movie.douban.com/subject/1234567/ 这种
_DOUBAN_LINK = re.compile(r"douban\.com/subject/(\d+)")
#: 发布信息里的年份
_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")


def parse_custom_feeds(text: str) -> list[str]:
    """多行文本 → 地址列表（去空行、去注释、去重、保序）。"""
    urls: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line not in urls:
            urls.append(line)
    return urls


def _entry_from_title(title: str, link: str = "", extra: str = "") -> dict | None:
    """把一条「片名（可带链接/描述）」整理成统一的条目字典。"""
    title = (title or "").strip()
    if not title:
        return None
    item: dict = {"title": title}
    match = _DOUBAN_LINK.search(link or "") or _DOUBAN_LINK.search(extra or "")
    if match:
        item["title_ref"] = f"douban:{match.group(1)}"
    year = _YEAR.search(extra or "")
    if year:
        item["release_year"] = int(year.group(1))
    return item


def parse_feed_text(text: str) -> list[dict]:
    """外部榜单正文 → 条目列表；RSS/Atom、JSON、纯文本一行一个都认。

    返回的每项是 ``{"title": …, 可选 title_ref / release_year}``；
    带 ``title_ref``（豆瓣 ID）的直接订阅，其余靠片名去搜。
    """
    body = (text or "").strip()
    if not body:
        return []
    if body.startswith("<"):
        return _parse_xml(body)
    if body[0] in "[{":
        parsed = _parse_json(body)
        # 看着是 JSON 就必须按 JSON 解：认不出形状时当作空，不要落到纯文本分支
        # 把整段 JSON 当成一个片名。
        return parsed if parsed is not None else []
    # 纯文本：一行一个片名
    items = [_entry_from_title(line) for line in body.splitlines()]
    return [item for item in items if item]


def _parse_xml(text: str) -> list[dict]:
    """RSS 2.0 / Atom：取 title + link(/guid)，能从链接里认豆瓣 ID。"""
    try:
        root = xml.etree.ElementTree.fromstring(text)
    except xml.etree.ElementTree.ParseError:
        return []
    items: list[dict] = []
    # RSS 的 item，Atom 的 entry（带命名空间）
    nodes = [n for n in root.iter() if _localname(n.tag) in ("item", "entry")]
    for node in nodes:
        fields: dict[str, str] = {}
        for child in node:
            fields.setdefault(_localname(child.tag), (child.text or "").strip())
        link = fields.get("link") or ""
        if not link:
            # Atom 的 link 常写成 <link href="…"/>，且没有正文
            for child in node:
                if _localname(child.tag) == "link" and child.get("href"):
                    link = child.get("href") or ""
                    break
        extra = " ".join(v for k, v in fields.items() if k in ("description", "summary", "content"))
        item = _entry_from_title(fields.get("title") or "", link, extra)
        if item:
            items.append(item)
    return items


def _parse_json(text: str) -> list[dict] | None:
    """JSON：字符串数组 / 对象数组 / ``{"titles": [...]}``；认不出的返回 None。"""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict):
        for key in ("titles", "items", "list", "data", "results"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            return None
    if not isinstance(data, list):
        return None
    items: list[dict] = []
    for entry in data:
        if isinstance(entry, str):
            item = _entry_from_title(entry)
        elif isinstance(entry, dict):
            title = next(
                (str(entry[k]) for k in ("title", "name", "douban_id", "tmdb_id") if entry.get(k)),
                "",
            )
            link = str(entry.get("link") or entry.get("url") or "")
            extra = " ".join(
                str(entry[k]) for k in ("description", "overview", "year", "id") if entry.get(k)
            )
            item = _entry_from_title(title, link, extra)
            # 对象里直接写豆瓣 ID 的（如 {"douban_id": "12345"}）
            for key in ("douban_id", "douban", "db_id"):
                if entry.get(key) and str(entry[key]).isdigit():
                    item = item or {"title": str(entry[key])}
                    item["title_ref"] = f"douban:{entry[key]}"
                    break
        else:
            continue
        if item:
            items.append(item)
    return items


def _localname(tag: str) -> str:
    """去掉 XML 命名空间，`{ns}title` → `title`。"""
    return tag.rsplit("}", 1)[-1].lower()


@plugin(
    PLUGIN_ID,
    title="豆瓣榜单订阅",
    inject=(HOST_OPS, PLUGIN_DATA, PLUGIN_ROUTES),
    permissions=(
        "discover.browse-collection",
        "search.titles",
        "subscriptions.create",
    ),
    config=Settings,
)
async def apply(ctx: Context[Settings]) -> None:
    ops = await ctx.use(HOST_OPS).client(ctx)
    store = ctx.use(PLUGIN_DATA).scoped(ctx)
    routes = ctx.use(PLUGIN_ROUTES)
    log = ctx.logger

    async def load_panel() -> dict:
        """读网页端保存的设置；没保存过就用默认值（全都不勾）。"""
        return normalize_panel(await store.get(SETTINGS_KEY, default={}))

    async def save_panel(values: dict) -> dict:
        cleaned = normalize_panel(values)
        await store.set(SETTINGS_KEY, cleaned)
        return cleaned

    initial = await load_panel()
    if not enabled_ranks_from(initial) and not parse_custom_feeds(initial["custom_feeds"]):
        log.info("还没勾选任何榜单、也没填自定义地址，插件已加载但不会订阅内容；去左侧栏「豆瓣榜单」里配置")

    async def load_seen() -> dict:
        raw = await store.get("seen", default={})
        return raw if isinstance(raw, dict) else {}

    async def save_seen(seen: dict) -> None:
        # 只留最近的一批，避免无限增长
        items = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:3000]
        await store.set("seen", dict(items))

    async def hold_pending(ref: str, title: str, reason: str, candidates: list[str]) -> None:
        """要用户定的：记下来，不自动处理。"""
        pending = await store.get("pending", default={})
        if not isinstance(pending, dict):
            pending = {}
        pending[ref] = {"title": title, "reason": reason, "candidates": candidates}
        await store.set("pending", pending)

    async def resolve_by_name(title: str) -> str | None:
        """没有豆瓣 ID 的条目：拿片名去搜，取最匹配的一个 title_ref。

        剧本或搜不到都返回 None，由调用方记入待确认。
        """
        try:
            found = await ops.call(
                "search.titles", {"query": title, "provider": "all", "save_history": False}
            )
        except OpsError as exc:
            log.warning("搜《%s》失败：%s", title, exc.message)
            return None
        candidates = found.get("titles") or [] if isinstance(found, dict) else []
        if not candidates:
            return None
        return str(candidates[0].get("title_ref") or "") or None

    async def subscribe(item: dict, panel: dict) -> str:
        """处理一个作品；返回结果描述（写日志/历史用）。"""
        ref = str(item.get("title_ref") or "")
        name = describe(item)
        media_type = item.get("media_type")

        # 自定义榜单里可能只给了片名：先搜出对应的影视条目
        if not ref:
            ref = await resolve_by_name(str(item.get("title") or ""))
            if not ref:
                await hold_pending(
                    f"name:{item.get('title')}", name, "没有豆瓣 ID 且搜不到对应作品，请手动订阅", []
                )
                return "搜不到对应作品，已记入待确认"

        payload: dict = {"title_ref": ref, "selection_mode": "rules"}
        if media_type == "tv":
            # 榜单里是剧集时，豆瓣条目对不上 TMDB 的哪一季只有订阅时才知道：
            # 默认只追以后播出的新集，不替用户猜季号；想自己勾季就选 pending。
            if panel.get("tv_strategy") == TvStrategy.PENDING.value:
                await hold_pending(ref, name, "是剧集，请手动勾选要订阅的季", [])
                return "是剧集，已记入待确认"
            payload["follow_future"] = True

        try:
            created = await ops.call("subscriptions.create", payload)
        except OpsError as exc:
            if exc.code == "SUBSCRIPTION_TARGET_AMBIGUOUS":
                refs = [
                    str(c.get("title_ref") or "")
                    for c in (exc.details or [])
                    if isinstance(c, dict)
                ]
                await hold_pending(ref, name, "对应多部作品，请选择要订阅的一部", refs)
                return "对应多部作品，已记入待确认"
            raise

        subscription = (created or {}).get("subscription") or {}
        sub_id = subscription.get("id")
        return f"已订阅（#{sub_id}）" if sub_id is not None else "已订阅"

    async def fetch(url: str) -> str:
        """拉取自定义榜单：按用户的代理设置走（清单声明了联网）。"""
        async with httpx.AsyncClient(
            transport=net.http_transport(PLUGIN_ID), timeout=30, follow_redirects=True
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    def feed_key(url: str, item: dict) -> str:
        """自定义条目的去重键：有豆瓣 ID 用 ID，否则用「地址 + 片名」。"""
        ref = item.get("title_ref")
        return str(ref) if ref else f"{url}#{item.get('title')}"

    async def record(item: dict, source: str, result: str, key: str, seen: dict, history: list) -> None:
        seen[key] = _now()
        log.info("%s %s（来自「%s」）", describe(item), result, source)
        history.insert(0, {
            "title": str(item.get("title") or ""),
            "year": item.get("release_year"),
            "rating": item.get("provider_rating"),
            "media_type": item.get("media_type"),
            "title_ref": item.get("title_ref") or key,
            "rank": source,
            "result": result,
            "time": _now_text(),
        })

    async def refresh_once() -> dict:
        """跑一轮：返回本轮小结。每轮重新读设置，网页上改完立即在下一轮生效。"""
        panel = await load_panel()
        ranks = enabled_ranks_from(panel)
        feeds = parse_custom_feeds(panel["custom_feeds"])
        min_rating = panel["min_rating"]
        limit = panel["per_rank_limit"]
        seen = await load_seen()
        history = await store.get("history", default=[])
        if not isinstance(history, list):
            history = []
        added = 0
        skipped = 0
        processed = 0

        # ---- 内置豆瓣榜单 ----
        for ref in ranks:
            try:
                listing = await ops.call(
                    "discover.browse-collection",
                    {"collection_ref": ref, "limit": limit},
                )
            except OpsError as exc:
                log.warning("读取榜单 %s 失败：%s", ref, exc.message)
                continue
            collection = listing.get("collection") or {}
            rank_name = str(collection.get("name") or ref)
            titles = [t for t in (listing.get("titles") or []) if isinstance(t, dict)]
            picked = pick_new(titles, set(seen), min_rating)
            skipped += len(titles) - len(picked)
            if picked:
                log.info("榜单「%s」有 %d 个新作品待处理", rank_name, len(picked))
            for item in picked:
                processed += 1
                try:
                    result = await subscribe(item, panel)
                except OpsError as exc:
                    log.warning("订阅 %s 失败：%s", describe(item), exc.message)
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 -- 单个作品出错不拖垮整轮
                    log.warning("处理 %s 时出错", describe(item), exc_info=True)
                    continue
                added += 1
                await record(item, rank_name, result, str(item["title_ref"]), seen, history)

        # ---- 自定义榜单地址 ----
        for url in feeds:
            try:
                text = await fetch(url)
            except httpx.HTTPError as exc:
                log.warning("拉取自定义榜单 %s 失败：%s", url, exc)
                continue
            except Exception:  # noqa: BLE001 -- 地址需要登录 / 正文不是文本等
                log.warning("拉取自定义榜单 %s 出错", url, exc_info=True)
                continue
            entries = parse_feed_text(text)
            if not entries:
                log.warning("自定义榜单 %s 没解析出任何条目", url)
                continue
            fresh = [e for e in entries if feed_key(url, e) not in seen]
            skipped += len(entries) - len(fresh)
            if fresh:
                log.info("自定义榜单 %s 有 %d 个新作品待处理", url, len(fresh))
            for item in fresh:
                processed += 1
                key = feed_key(url, item)
                try:
                    result = await subscribe(item, panel)
                except OpsError as exc:
                    log.warning("订阅 %s 失败：%s", describe(item), exc.message)
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 -- 单个作品出错不拖垮整轮
                    log.warning("处理 %s 时出错", describe(item), exc_info=True)
                    continue
                added += 1
                await record(item, url, result, key, seen, history)

        await save_seen(seen)
        await store.set("history", history[:500])
        await store.set("last_run_at", _now())
        summary = {
            "ranks": len(ranks),
            "custom_feeds": len(feeds),
            "subscribed": added,
            "skipped": skipped,
        }
        log.info(
            "本轮完成：%d 个内置榜单 + %d 个自定义地址，新建订阅 %d 个，跳过 %d 条",
            len(ranks),
            len(feeds),
            added,
            skipped,
        )
        return summary

    async def scheduled() -> None:
        panel = await load_panel()
        # 总开关关掉时定时任务什么都不做（页面仍可浏览榜单、手动订阅）
        if not panel.get("auto_subscribe", True):
            return
        if not enabled_ranks_from(panel) and not parse_custom_feeds(panel["custom_feeds"]):
            return
        await refresh_once()

    # 定时任务：用户在「设置 → 定时任务」里调周期 / 暂停 / 立即运行
    ctx.contribute(
        SCHEDULED_TASKS,
        TASK_KEY,
        TaskDefinition(
            key=TASK_KEY,
            title="豆瓣榜单订阅：扫描榜单",
            handler=scheduled,
            default_trigger_type=TriggerType.CRON,
            default_cron="0 8 * * *",
            default_enabled=True,
            description="扫描勾选的豆瓣榜单，为新上榜且评分达标的作品创建订阅。",
        ),
    )

    router = APIRouter()

    @router.get(
        "/panel",
        operation_id=f"plugins.{PLUGIN_ID}.panel",
        summary="豆瓣榜单订阅：读取网页端设置（榜单勾选、评分门槛等）",
    )
    async def panel_settings() -> dict:
        return {"settings": await load_panel(), "defaults": DEFAULTS}

    @router.put(
        "/panel",
        operation_id=f"plugins.{PLUGIN_ID}.save-panel",
        summary="豆瓣榜单订阅：保存网页端设置",
    )
    async def save_panel_settings(values: PanelSettings) -> dict:
        raw = values.model_dump() if hasattr(values, "model_dump") else dict(values)
        extra = getattr(values, "model_extra", None) or {}
        raw.update(extra)
        saved = await save_panel(raw)
        log.info(
            "网页端设置已更新：%d 个榜单，评分门槛 %.1f，每个榜单 %d 条，自定义地址 %d 个",
            len(enabled_ranks_from(saved)),
            saved["min_rating"],
            saved["per_rank_limit"],
            len(parse_custom_feeds(saved["custom_feeds"])),
        )
        return {"settings": saved}

    @router.post(
        "/subscribe",
        operation_id=f"plugins.{PLUGIN_ID}.subscribe",
        summary="豆瓣榜单订阅：为页面上的这部作品建订阅",
    )
    async def subscribe_one(body: SubscribeRequest) -> dict:
        """网页卡片上的「订阅」按钮走这里。

        前端已经知道这部是电影还是剧集，直接传类型，省一次详情请求。
        剧集的处理与自动扫描一致（按设置里的剧集策略）。
        """
        ref = (body.title_ref or "").strip()
        if not ref.startswith("douban:"):
            raise HTTPException(status_code=400, detail="只能订阅豆瓣条目")
        item = {"title_ref": ref, "media_type": (body.media_type or "").strip() or None}
        try:
            result = await subscribe(item, await load_panel())
        except OpsError as exc:
            raise HTTPException(status_code=502, detail=f"订阅失败：{exc.message}") from exc
        log.info("网页端订阅 %s：%s", ref, result)
        return {"ok": True, "result": result, "message": result}

    @router.get(
        "/status",
        operation_id=f"plugins.{PLUGIN_ID}.status",
        summary="豆瓣榜单订阅：运行状态",
    )
    async def status() -> dict:
        panel = await load_panel()
        pending = await store.get("pending", default={})
        history = await store.get("history", default=[])
        return {
            "ranks": enabled_ranks_from(panel),
            "custom_feeds": parse_custom_feeds(panel["custom_feeds"]),
            "min_rating": panel["min_rating"],
            "last_run_at": await store.get("last_run_at"),
            "subscribed_total": len(history) if isinstance(history, list) else 0,
            "pending_count": len(pending) if isinstance(pending, dict) else 0,
        }

    @router.get(
        "/history",
        operation_id=f"plugins.{PLUGIN_ID}.history",
        summary="豆瓣榜单订阅：最近的新增订阅记录",
    )
    async def history(limit: int = 50) -> dict:
        records = await store.get("history", default=[])
        if not isinstance(records, list):
            records = []
        return {"total": len(records), "items": records[: max(1, limit)]}

    @router.get(
        "/pending",
        operation_id=f"plugins.{PLUGIN_ID}.pending",
        summary="豆瓣榜单订阅：待确认的作品（要你手动决定）",
    )
    async def pending_list() -> dict:
        pending = await store.get("pending", default={})
        if not isinstance(pending, dict):
            pending = {}
        return {
            "items": [{"title_ref": k, **v} for k, v in pending.items()],
        }

    @router.post(
        "/run",
        operation_id=f"plugins.{PLUGIN_ID}.run",
        summary="豆瓣榜单订阅：立即扫描一轮",
    )
    async def run_now() -> dict:
        panel = await load_panel()
        # 自动订阅关着时，「立即扫描」也不下单：否则误点一次就批量订阅，开关形同虚设。
        # 页面上的单张卡片仍可手动点「订阅」，那条走 /subscribe，不受此限。
        if not panel.get("auto_subscribe", True):
            return {
                "ranks": 0,
                "custom_feeds": 0,
                "subscribed": 0,
                "skipped": 0,
                "message": "自动订阅已关闭；需要订阅请直接点海报上的「订阅」",
            }
        if not enabled_ranks_from(panel) and not parse_custom_feeds(panel["custom_feeds"]):
            return {
                "ranks": 0,
                "custom_feeds": 0,
                "subscribed": 0,
                "skipped": 0,
                "message": "还没勾选榜单、也没填自定义地址",
            }
        return await refresh_once()

    @router.post(
        "/clear-history",
        operation_id=f"plugins.{PLUGIN_ID}.clear-history",
        summary="豆瓣榜单订阅：清空历史与「已处理」记录（清空后会重新订阅当前榜单）",
    )
    async def clear_history() -> dict:
        await store.set("history", [])
        await store.set("seen", {})
        return {"ok": True, "message": "已清空历史与已处理记录"}

    @router.post(
        "/dismiss-pending",
        operation_id=f"plugins.{PLUGIN_ID}.dismiss-pending",
        summary="豆瓣榜单订阅：把某个待确认作品标记为已处理（不再提醒）",
    )
    async def dismiss_pending(title_ref: str) -> dict:
        pending = await store.get("pending", default={})
        if isinstance(pending, dict) and title_ref in pending:
            pending.pop(title_ref, None)
            await store.set("pending", pending)
            return {"ok": True}
        return {"ok": False, "message": "待确认列表里没有这个引用"}

    routes.mount(ctx, router)
    log.info(
        "%s 已加载：%d 个榜单，评分门槛 %.1f，每个榜单取前 %d 条",
        PLUGIN_ID,
        len(enabled_ranks_from(initial)),
        initial["min_rating"],
        initial["per_rank_limit"],
    )


def _now() -> int:
    return int(time.time())


def _now_text() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
