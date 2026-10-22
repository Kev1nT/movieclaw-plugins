"""种子做种定时清理：给下载器里的种子设「做种多久」，到点自动删任务。

流程：定时任务（默认每 10 分钟，可在「设置 → 定时任务」里改）扫描下载器里的全部种子，
按 **qBittorrent 自己记的时间**（累计做种时长 / 下载完成时刻 / 添加时刻，取最长的那个）
算每个种子已经做种多久。到点、且没有安全线挡着的种子，直接从下载器删掉——
**不依赖 MovieClaw 入库**，只把它当下载器用也照样工作。

安全线（任何一条命中都不删，只提醒）：
- 站点有 H&R 考核、或做种时长没到站点要求的最低时长；
- 还在下载 / 做种没完成，或下载器正在校验；
- 订阅刚投递、还没拿到这个种子里的内容（删了会被打回缺资源重下）——可在设置里关；
- 这个种子还供着媒体库里别的条目 / 没识别的文件（合集、季包，删了连带毁别的片）；
- 刷流（分享率自动提升）引擎登记的种子——由它自己汰换；
- 正在播放、或 30 分钟内打开过这部片（可在设置里关）；
- 带「免清理 / noreap」标签的种子，永不处理。

删种走宿主的 ``dl.torrent.delete``：订阅工单、任务中心、订阅状态会跟着同步，
不会留下悬空记录。qBittorrent 自己从 v4.6 起已移除「做种限时」，所以由插件定时对账。
"""

import asyncio
from datetime import datetime, timezone

import pydantic
from fastapi import APIRouter, HTTPException
from pydantic import Field, SecretStr, field_validator

from movieclaw_api.plugins.keys import PLUGIN_DATA, PLUGIN_ROUTES
from movieclaw_scheduler import SCHEDULED_TASKS, TaskDefinition, TriggerType
from movieclaw_sdk import Context, plugin
from movieclaw_sdk.events import (
    DOWNLOAD_COMPLETED,
    LIBRARY_INGEST_COMPLETED,
    LIBRARY_ITEM_VIEWED,
    PLAYBACK_STARTED,
    PLUGIN_EVENTS,
)
from movieclaw_sdk.events._base import Frozen
from movieclaw_sdk.events.plugin import plugin_event
from movieclaw_sdk.jobs import JOB_HANDLERS, PLUGIN_JOBS, JobBlocked, RegisteredJobHandler
from movieclaw_sdk.ops import HOST_OPS, OpsError

PLUGIN_ID = "seed-scheduler"
SCAN_KEY = f"{PLUGIN_ID}.scan"
SWEEP_JOB = f"{PLUGIN_ID}:sweep"

SEEN_KEY = "seen"        # {infohash: 插件第一次看到它的时间戳}
REMOVED_KEY = "removed"  # {infohash: 删掉的时间戳}——删了之后不再重记首见时间
HISTORY_KEY = "history"  # 最近的处理记录（给插件接口和网页看）
OPEN_KEY = "opened"      # {media_item_id: 最近打开的秒级时间戳}
STATS_KEY = "stats"      # 累计计数
SEEN_LIMIT = 20000
HISTORY_SIZE = 300

# qBittorrent 里直接可见的标签：用户可以在下载器里加上它把某个种子排除在外
TAG_KEEP = "免清理"
KEEP_TAGS = {"免清理", "noreap", "seed-keep"}

# 订阅工单里「还在等这个种子」的状态：删了会把对应季集打回缺资源重下
_WAITING = {"grabbed", "downloaded", "replacement_pending", "trial"}
# 已经从库里删掉、不该再挡着的库文件状态
_IMPORTED = {"imported", "completed", "done", "succeeded", "fulfilled"}
_UNKNOWN_STATUS_KEEP = True   # 状态看不明白时保守：当成「还要用它」
_GONE_FILE_STATES = {"trashed", "purged", "deleted", "missing", "removed"}

UNFINISHED_GRACE_DAYS = 7   # 首次见到仍未下完的任务，最多赖这么久
WEB_IDLE_MINUTES = 30       # 最近打开过这部片，多久内不清理
MAX_PER_RUN = 200           # 一轮最多删多少个，防止一次改动在几秒内删光上千个任务


# ---------------------------------------------------------------------- 设置


class Settings(pydantic.BaseModel):
    """插件「设置」表单。"""

    seed_hours: float = Field(
        72,
        ge=1,
        le=8760,
        title="单种做种时长（小时）",
        description="从插件第一次看到这个任务起算。到点、且媒体库已确认有这份文件的，自动从下载器删除",
    )
    overdue_days: float = Field(
        3,
        ge=0,
        le=90,
        title="没入库的种子再等几天后提醒（天）",
        description="手动下载、一直没入库的种子不删除；超过「做种时长 + 这么多天」时提醒你处理",
    )
    protect_unwatched: bool = Field(
        False,
        title="库里的还没看完就先保留",
        description="打开后：种子挂着的订阅季集里还有没看完的集，就先不清理（默认关：纯当下载器用时不看观看进度）",
    )
    protect_recent_open: bool = Field(
        True,
        title="最近打开过的先保留",
        description="最近 30 分钟有人在网页上打开过这部片，就先保留",
    )
    respect_hr: bool = Field(
        True,
        title="带 H&R 考核的种子多保一段",
        description="有 H&R 的种子至少做种到下面这个时长才删（站点具体考核时长 MovieClaw 拿不到）",
    )
    hr_min_hours: float = Field(
        72,
        ge=1,
        le=8760,
        title="H&R 种子最短做种时长（小时）",
        description="站点有 H&R 考核时按它兜底：常见站点的考核是 72 小时、分享率 1.0 以上",
    )
    respect_boost: bool = Field(
        True,
        title="不碰刷流引擎的种子",
        description="分享率自动提升（刷流）引擎登记的种子由它自己汰换，本插件不删",
    )
    dry_run: bool = Field(
        False,
        title="只记录不删除（演练）",
        description="打开后只把「本来会删什么」写进记录，不动下载器——建议先跑几轮看看",
    )
    delete_files: bool = Field(
        True,
        title="删除时连数据文件一起删",
        description="关掉则只从下载器移除任务、保留磁盘文件（空间不会回来）",
    )

    qb_url: str = Field(
        "",
        title="qBittorrent WebUI 地址",
        description=(
            "插件要扫「下载器里的全部种子」，而任务中心只列还在跑的任务，所以需要直连一次 QB。"
            "填 http://10.10.10.100:8085 这样；留空则只用任务中心的口径（只看到还在跑的任务）"
        ),
    )
    qb_username: str = Field("", title="qBittorrent 用户名", description="未开鉴权可留空")
    qb_password: SecretStr | None = Field(None, title="qBittorrent 密码", description="加密保存，界面不回显")

    handle_seeding: bool = Field(
        True,
        title="处理·正在做种（上传中 / 已停做种）",
        description="做种满设定时长的种子里，这一类要不要处理。关掉则它们永不自动删除",
    )
    handle_missing_files: bool = Field(
        True,
        title="处理·文件已丢失（missingFiles）",
        description="QB 找不到文件、做不了种的种子。删它们不占空间，主要是清理任务列表",
    )
    handle_downloading: bool = Field(
        False,
        title="处理·仍在下载（下载中 / 排队 / 停滞）",
        description="到点还没下完的种子。默认不碰——还在下的种子删了等于放弃这次下载",
    )
    handle_errored: bool = Field(
        False,
        title="处理·出错（error）",
        description="QB 报错的种子。默认不碰，等你人工看一眼",
    )
    handle_checking: bool = Field(
        False,
        title="处理·校验中（checking / 分配空间）",
        description="正在校验文件的种子。默认不碰，校验完下一轮自然归到做种 / 出错",
    )
    handle_unknown: bool = Field(
        False,
        title="处理·其他未知状态",
        description="QB 新旧版本状态词对不上时用。默认不碰，宁可漏删也不误删",
    )

    library_required: bool = Field(
        False,
        title="只清理媒体库里已有这份文件的种子",
        description=(
            "默认关：做种满设定的时长，不论有没有入库都删（适合只把 MovieClaw 当下载器用）。"
            "打开后更保守：没入库的种子一律不删，只提醒"
        ),
    )
    keep_waiting_subscriptions: bool = Field(
        True,
        title="订阅还等着用的种子先保留",
        description=(
            "MovieClaw 订阅刚投递、还没拿到文件的种子（正在下的），到点也先留着，"
            "免得订阅被打回缺资源反复重下。用不到订阅可以关掉"
        ),
    )

    @field_validator("seed_hours")
    @classmethod
    def _at_least_one_hour(cls, value: float) -> float:
        if value < 1:
            raise ValueError("做种时长至少 1 小时")
        return value


# ---------------------------------------------------------------------- 纯函数（可单独自测）


def items_of(payload) -> list:
    """``dl.tasks`` 的返回可能被包成 {items: [...]} 或直接是列表。"""
    if isinstance(payload, dict):
        return list(payload.get("items") or [])
    if isinstance(payload, list):
        return list(payload)
    return []


def num(value, default=0.0) -> float:
    """稳妥地把接口里的数字转成 float；None、字符串、布尔都不炸。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_time(value) -> datetime | None:
    """把接口返回的时间解析成 UTC datetime；解析不了返回 None。"""
    if not value:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def hash_key(value) -> str:
    """info_hash 是唯一键，统一小写（工具与下载器的大小写不一定一致）。"""
    return str(value or "").strip().lower()


def has_keep_tag(tags) -> bool:
    """任务是否被标记为「永不清理」。"""
    wanted = {tag.lower() for tag in KEEP_TAGS}
    return any(str(tag).strip().lower() in wanted for tag in tags or ())


# QB 状态词 → 本插件的归类。qBittorrent 4.x 与 5.x 用词不同（pausedUP / stoppedUP、
# completed / uploading），两边都收；认不出的归 other，由「处理·其他未知状态」决定。
_STATE_GROUPS = {
    "seeding": {
        "uploading", "stalledup", "forcedup", "queuedup", "pausedup", "stoppedup",
        "completed", "seeding", "forcedseeding", "queuedseeding",
    },
    "missing": {"missingfiles"},
    "downloading": {
        "downloading", "metadl", "forceddl", "stalleddl", "queueddl", "allocating",
        "pauseddl", "stoppeddl", "downloadingmetadata",
    },
    "errored": {"error"},
    "checking": {"checkingdl", "checkingup", "checkingresumedata", "moving"},
}
GROUP_TITLES = {
    "seeding": "正在做种",
    "missing": "文件已丢失",
    "downloading": "仍在下载",
    "errored": "出错",
    "checking": "校验中",
    "other": "其他状态",
}


def state_group(brief: dict) -> str:
    """把这个种子归到哪一类（见 ``_STATE_GROUPS``）。

    先看 QB 的原始状态词；状态词认不出时退回「进度是否 100%」——
    下完了却认不出状态，只可能是做种之类，不能当「还在下载」放过。
    """
    raw = str(brief.get("state") or "").strip().lower()
    for group, words in _STATE_GROUPS.items():
        if raw in words:
            return group
    if not raw:
        return "seeding" if brief.get("completed") else "downloading"
    return "other"


def group_allowed(group: str, config: Settings) -> bool:
    """这一类要不要处理（对应设置里的几个勾选）。"""
    return {
        "seeding": config.handle_seeding,
        "missing": config.handle_missing_files,
        "downloading": config.handle_downloading,
        "errored": config.handle_errored,
        "checking": config.handle_checking,
        "other": config.handle_unknown,
    }.get(group, False)


def seeded_hours(brief: dict, first_seen: float, now: float) -> float:
    """这个种子已经做种多久（小时）。**以 QB 自己的记录为准**，不从插件装上的时刻起算。

    三个信号取最长的那个（都来自 qBittorrent 的任务信息）：

    - ``seeding_seconds``：QB 记的累计做种秒数（暂停的时间不算，最准）；
    - ``completed_on``：下载完成的时刻，之后一直在做种；
    - ``added_on``：加进下载器的时刻。

    为什么取最长：下载完成后立即开始上传的种子，三个值本来就一致；而 QB 重启、
    重新校验会把累计秒数归零，偶尔也会重置添加时间——取最长才不会把熬了半个月的
    老种子判成刚下的、白等一轮。插件自己的首见时间只在这些信号全都缺失时兜底。
    """
    candidates: list[float] = []
    seconds = num(brief.get("seeding_seconds"), -1.0)
    if seconds > 0:
        candidates.append(seconds / 3600.0)
    for field in ("completed_on", "added_on"):
        stamp = num(brief.get(field), 0.0)
        if stamp > 0:
            candidates.append(max(0.0, (now - stamp) / 3600.0))
    if candidates:
        return round(max(candidates), 3)
    if first_seen <= 0:
        return 0.0
    return max(0.0, (now - first_seen) / 3600.0)


def seeded_start(brief: dict, first_seen: float, now: float) -> float:
    """做种起点（秒级时间戳），给「什么时候该删」用；和 :func:`seeded_hours` 同一个口径。"""
    hours = seeded_hours(brief, first_seen, now)
    if hours <= 0:
        return 0.0
    return now - hours * 3600.0


def removal_blocker(
    brief: dict,
    facts: dict,
    config: Settings,
    *,
    first_seen: float,
    now: float,
) -> str | None:
    """这个任务现在**不能删**的原因；能删返回 None。只看种子自己的事实。

    ``facts``：``group``（状态归类）/ ``watching`` / ``boost`` / ``hr``。
    「该不该删」「库里有没有」「订阅还等不等」由调用方判断。
    """
    if has_keep_tag(brief.get("tags")):
        return "带「免清理」标签"
    group = str(facts.get("group") or "other")
    if not group_allowed(group, config):
        return f"「{GROUP_TITLES.get(group, group)}」这一类没勾选处理"
    if facts.get("boost") and config.respect_boost:
        return "刷流引擎在管这个种子"
    if facts.get("watching"):
        return "正在播放，或最近打开过"
    if config.respect_hr and facts.get("hr"):
        elapsed = seeded_hours(brief, first_seen, now)
        return (
            f"站点有 H&R 考核（已做种 {elapsed:.0f} 小时，要求不低于 "
            f"{config.hr_min_hours:g} 小时）"
        )
    return None


def library_verdict(relations: dict, config: Settings) -> tuple[str | None, bool]:
    """媒体库这一侧还要不要拦。返回 (不能删的原因, 库里是否已有这份内容)。

    **默认一点不拦**：只当下载器用时，种子到点就删，不用等入库、也不看库里的状态。
    仍会拦的只剩两件「删了会出真事」的情况：

    - 订阅还等着用这个种子里的季集 → 不删（删了会被打回「缺资源」重新下载一遍）；
    - 这个种子还供着别的条目 / 没识别的文件（合集、季包）→ 不删（会连带毁别的片）。

    两个可选开关（默认都关）：``library_required``（没入库不删）、
    ``protect_unwatched``（库里的集没看完不删）。
    """
    waiting = relations.get("waiting_units") or []
    guarded = relations.get("guarded_units") or []
    other = relations.get("other_file_ids") or []
    owned = relations.get("file_ids") or []
    in_library = bool(owned or other)
    if waiting and config.keep_waiting_subscriptions:
        return f"订阅还在等这些季集 {units_text(waiting)}", in_library
    if other:
        return "还供着媒体库里别的条目或没识别的文件", True
    if config.library_required and not in_library:
        return "媒体库还没有这份文件", False
    if config.protect_unwatched and guarded:
        return f"库里的 {units_text(guarded)} 还没看完", True
    return None, in_library


def units_text(units) -> str:
    parts = []
    for unit in units or []:
        try:
            season, episode = int(unit[0]), int(unit[1])
        except (TypeError, ValueError, IndexError):
            continue
        parts.append("电影" if season == 0 and episode == 0 else f"S{season:02d}E{episode:02d}")
    return "、".join(parts[:6]) + ("…" if len(parts) > 6 else "")


def as_relations(raw: dict) -> dict:
    """``library.items.relations`` 的结果 → 删种判断要的事实。"""
    file_ids: list = []
    others: list = []
    waiting: list = []
    guarded: list = []
    for torrent in raw.get("torrents") or []:
        owns = list(torrent.get("file_ids") or []) + list(torrent.get("other_file_ids") or [])
        if not owns:
            continue
        file_ids.extend(torrent.get("file_ids") or [])
        others.extend(torrent.get("other_file_ids") or [])
        status = str(torrent.get("status") or "").lower()
        units = list(torrent.get("units") or [])
        if status in _WAITING:
            # 订阅投递的工单还在等它，或手动下载意图还在等它入库：删了都要重来
            waiting.extend(units)
        elif status in _IMPORTED:
            # 已完成 / 已入库：只剩「库里的集有没有看完」这一条要看
            guarded.extend(units)
        elif _UNKNOWN_STATUS_KEEP:
            # 状态看不明白、又确实认领了库文件：保守当成「完成」，看观看情况
            # （只有空状态，也就是文件来源记录，才直接放行）
            if status:
                guarded.extend(units)
    return {
        "file_ids": uniq(file_ids),
        "other_file_ids": uniq(others),
        "waiting_units": uniq_units(waiting),
        "guarded_units": uniq_units(guarded),
    }


def uniq(values):
    return list(dict.fromkeys(values))


def uniq_units(units):
    out: dict = {}
    for unit in units:
        try:
            key = (int(unit[0]), int(unit[1]))
        except (TypeError, ValueError, IndexError):
            continue
        out[key] = [key[0], key[1]]
    return list(out.values())


def should_remind(brief: dict, first_seen: float, now: float, config: Settings) -> bool:
    """没入库的种子是否已经超过兜底时间，该提醒用户了。"""
    if first_seen <= 0:
        return False
    if now - first_seen < (config.seed_hours + config.overdue_days) * 3600:
        return False
    return bool(brief.get("completed"))


def recently_opened(
    opened: dict, media_item_id, now: float, minutes: float = WEB_IDLE_MINUTES
) -> bool:
    """这部片是不是最近被打开过（网页点进详情 / 开始播放）。"""
    if media_item_id is None:
        return False
    stamp = num(opened.get(str(media_item_id)), 0.0)
    return stamp > 0 and now - stamp < minutes * 60


# ---------------------------------------------------------------------- 插件自己的事件


class SeedRemoved(Frozen):
    name: str
    info_hash: str
    title: str = ""
    media_item_id: int | None = None
    seeded_hours: float = 0
    size_bytes: int = 0
    files: str = ""
    deleted_files: bool = True


class SeedOverdue(Frozen):
    name: str
    info_hash: str
    title: str = ""
    media_item_id: int | None = None
    seeded_hours: float = 0
    hours: float = 0


SEED_REMOVED = plugin_event(
    f"{PLUGIN_ID}.seed-removed", SeedRemoved, title="做种定时：种子已到期清理"
)
SEED_OVERDUE = plugin_event(
    f"{PLUGIN_ID}.seed-overdue",
    SeedOverdue,
    title="做种定时：种子到期但被安全线挡着没删",
)


# ---------------------------------------------------------------------- 插件入口


@plugin(
    PLUGIN_ID,
    title="种子做种定时清理",
    config=Settings,
    inject=(HOST_OPS, PLUGIN_DATA, PLUGIN_ROUTES, PLUGIN_JOBS, PLUGIN_EVENTS),
    permissions=(
        "dl.list",
        "dl.tasks",
        "dl.torrent.delete",
        "library.list",
        "library.items.get",
        "library.items.relations",
        "dl.file-sources.list",
        "library.scan.start",
    ),
)
async def seed_scheduler(ctx: Context[Settings]) -> None:
    config = ctx.config
    ops = await ctx.use(HOST_OPS).client(ctx)
    store = ctx.use(PLUGIN_DATA).scoped(ctx)
    jobs = ctx.use(PLUGIN_JOBS).scoped(ctx)
    publisher = await ctx.use(PLUGIN_EVENTS).register(ctx, SEED_REMOVED, SEED_OVERDUE)
    log = ctx.logger

    async def data(key: str, default):
        return await store.get(key, default=default)

    async def remember(key: str, value) -> None:
        await store.set(key, value)

    async def add_history(entry: dict) -> None:
        history = await data(HISTORY_KEY, []) or []
        history.insert(0, entry)
        await remember(HISTORY_KEY, history[:HISTORY_SIZE])

    async def media_of(info_hash: str, file_ids: list[int]) -> dict:
        """这个种子在媒体库里的身份：认领它的库文件、所属条目。

        任务中心只列出**仍在业务管线里**的任务（订阅还等着、手动下载还没入库），
        已经入库、做完使命的种子不列——所以库里有没有这份文件、是谁的，只能按
        infohash 问文件来源，不按片名猜。插件跑在独立进程里，一切走宿主接口。
        """
        ids = [int(i) for i in file_ids if str(i).isdigit()]
        if not ids:
            return {"file_ids": [], "item": None}
        try:
            sources = await ops.call(
                "dl.file-sources.list", {"file_ids": ",".join(str(i) for i in ids[:200])}
            )
        except OpsError as exc:
            log.debug("查文件来源失败（%s）：%s", info_hash, exc.message)
            return {"file_ids": [], "item": None}
        own = [row for row in sources or [] if hash_key(row.get("info_hash")) == info_hash]
        if not own and len(sources or []) == 1:
            own = sources
        claimed = uniq([int(i) for row in own for i in row.get("file_ids") or []])
        if not claimed:
            return {"file_ids": [], "item": None}
        return {"file_ids": claimed, "item": await owner_item(claimed[0])}

    async def owner_item(file_id: int) -> dict | None:
        """某个库文件属于哪个库、哪个条目（``library.items.get`` 顺带给出标题与海报）。"""
        try:
            guesses = await ops.call("library.items.list", {"limit": 200, "offset": 0}) or []
        except OpsError:
            guesses = []
        for library in guesses:
            try:
                detail = await ops.call(
                    "library.items.get",
                    {"library_id": library.get("library_id"), "media_item_id": library.get("media_item_id")},
                )
            except OpsError:
                return None
            files = (detail or {}).get("files") or []
            if any(int(f.get("id") or 0) == file_id for f in files):
                return {
                    "library_id": library.get("library_id"),
                    "media_item_id": library.get("media_item_id"),
                    "title": (detail or {}).get("title") or library.get("title"),
                    "poster_url": (detail or {}).get("poster_url") or library.get("poster_url"),
                }
        return None

    async def relations_of(info_hash: str, media: dict) -> dict:
        """媒体库这一侧的事实：认领的文件、别的引用、等待中的季集。

        优先问「这个条目背后有哪些下载任务」（带订阅工单状态，能判断订阅还在等哪几集）；
        条目找不到时退回「这些文件来自哪些种子」的文件台账。
        """
        item = media.get("item") or {}
        library_id, media_item_id = item.get("library_id"), item.get("media_item_id")
        if library_id is not None and media_item_id is not None:
            try:
                raw = await ops.call(
                    "library.items.relations",
                    {"library_id": library_id, "media_item_id": media_item_id},
                )
                settled = as_relations(raw or {})
                if settled["file_ids"] or settled["other_file_ids"] or settled["waiting_units"]:
                    return settled
            except OpsError as exc:
                log.debug("查条目关联失败（%s）：%s", info_hash, exc.message)
        return {
            "file_ids": media.get("file_ids") or [],
            "other_file_ids": [],
            "waiting_units": [],
            "guarded_units": [],
        }

    async def run_scan(dry_run: bool | None = None, only: list[str] | None = None) -> dict:
        """跑一轮：记账 → 判断 → 删除 / 提醒。幂等，随时可以重跑。

        ``only``：这一轮只处理这些状态类（``seeding`` / ``missing`` /
        ``downloading`` / ``errored`` / ``checking`` / ``other``），给人「先只删
        丢失文件的试试」这种用法；不传就按设置里的勾选来。
        """
        dry = config.dry_run if dry_run is None else dry_run
        picked = {str(item).strip().lower() for item in (only or []) if str(item).strip()}
        now = datetime.now(timezone.utc).timestamp()
        seen: dict = await data(SEEN_KEY, {}) or {}
        removed: dict = await data(REMOVED_KEY, {}) or {}
        reminded: dict = await data("reminded", {}) or {}
        opened: dict = await data(OPEN_KEY, {}) or {}
        report = {
            "at": datetime.now(timezone.utc).isoformat(),
            "dry_run": dry,
            "seeded_hours_limit": config.seed_hours,
            "scanned": 0,
            "removed": 0,
            "reminded": 0,
            "downloader_errors": [],
            "removed_items": [],
            "overdue_items": [],
        }
        index = await task_index()
        try:
            downloaders = await ops.call("dl.list", {}) or []
        except OpsError as exc:
            report["downloader_errors"].append(f"读取下载器列表失败：{exc.message}")
            return report
        for downloader in downloaders:
            name = downloader.get("name") or downloader.get("id")
            if not downloader.get("enabled", True):
                continue
            if not downloader.get("usable", True):
                report["downloader_errors"].append(f"下载器「{name}」当前不可用，本轮跳过")
                continue
            did = downloader.get("id")
            briefs = await idle_torrents(downloader, index)
            for brief in briefs:
                known = index.get(hash_key(brief.get("info_hash"))) or {}
                if known:
                    brief.setdefault("media_item_id", known.get("media_item_id"))
                    brief["hit_and_run"] = brief.get("hit_and_run") or known.get("hit_and_run")
                    brief["source"] = brief.get("source") or known.get("source")
            report["scanned"] += len(briefs)
            handled = 0
            for brief in briefs:
                info_hash = hash_key(brief.get("info_hash"))
                first_seen = num(seen.get(info_hash), 0.0)
                if first_seen <= 0:
                    if info_hash in removed:
                        continue  # 删掉又被加回来的，从下次见到重新起算
                    first_seen = now
                    seen[info_hash] = int(now)
                if seeded_hours(brief, first_seen, now) < config.seed_hours:
                    continue

                media = await media_of(info_hash, list(brief.get("file_ids") or []))
                relations = await relations_of(info_hash, media)
                item = media.get("item") or {}
                media_item_id = item.get("media_item_id") or brief.get("media_item_id")
                seeded = round(seeded_hours(brief, first_seen, now), 1)
                title = str(brief.get("title") or brief.get("name") or "")
                group = state_group(brief)
                facts = {
                    "group": group,
                    "watching": recently_opened(opened, media_item_id, now)
                    and config.protect_recent_open,
                    "boost": brief.get("source") == "boost",
                    "hr": bool(brief.get("hit_and_run")),
                }
                if picked and group not in picked:
                    continue
                if not group_allowed(group, config):
                    continue
                blocker = removal_blocker(brief, facts, config, first_seen=first_seen, now=now)
                lib_blocker, _in_library = library_verdict(relations, config)
                if blocker is None:
                    blocker = lib_blocker

                if blocker is None:
                    log.info("到期待清理：%s（%s）", title or info_hash, seeded)
                    if handled >= MAX_PER_RUN:
                        continue
                    handled += 1
                    kept = {
                        "name": title or info_hash,
                        "media_item_id": media_item_id,
                        "title": item.get("title") or brief.get("title") or "",
                        "poster_url": item.get("poster_url"),
                    }
                    if dry:
                        report["removed"] += 1
                        report["removed_items"].append(
                            {**kept, "info_hash": info_hash, "seeded_hours": seeded}
                        )
                        continue
                    try:
                        await ops.call(
                            "dl.torrent.delete",
                            {
                                "downloader_id": did,
                                "info_hash": info_hash,
                                "delete_files": config.delete_files,
                            },
                        )
                    except OpsError as exc:
                        report["downloader_errors"].append(f"删除「{title}」失败：{exc.message}")
                        log.warning("删除种子失败（%s）：%s", info_hash, exc.message)
                        continue
                    removed[info_hash] = int(now)
                    seen.pop(info_hash, None)
                    report["removed"] += 1
                    report["removed_items"].append(
                        {**kept, "info_hash": info_hash, "seeded_hours": seeded}
                    )
                    await add_history(
                        {
                            "at": datetime.now(timezone.utc).isoformat(),
                            "action": "removed",
                            "name": title,
                            "info_hash": info_hash,
                            "downloader": name,
                            "media_item_id": media_item_id,
                            "title": item.get("title") or "",
                            "seeded_hours": seeded,
                            "size_bytes": int(num(brief.get("size_bytes"))),
                            "delete_files": config.delete_files,
                        }
                    )
                    log.info(
                        "已清理做种 %s 小时的种子：%s（%s）",
                        seeded,
                        title or info_hash,
                        "连数据删除" if config.delete_files else "保留数据文件",
                    )
                    try:
                        await publisher.publish(
                            SEED_REMOVED,
                            SeedRemoved(
                                name=title,
                                info_hash=info_hash,
                                title=title,
                                media_item_id=media_item_id,
                                seeded_hours=seeded,
                                size_bytes=int(num(brief.get("size_bytes"))),
                                files="（含数据文件）" if config.delete_files else "（保留数据文件）",
                                deleted_files=config.delete_files,
                            ),
                        )
                    except Exception:  # noqa: BLE001 -- 通知失败不影响删除结论
                        log.warning("发「种子已清理」事件失败", exc_info=True)
                    continue

                # 不删：被安全线挡着。只在「开了入库检查（library_required）」时才提醒——
                # 纯下载器用法下到点就是删，不该再拿「还没入库」来打扰人
                if not (config.library_required and should_remind(brief, first_seen, now, config)):
                    continue
                if info_hash in reminded:
                    continue
                reminded[info_hash] = int(now)
                report["reminded"] += 1
                report["overdue_items"].append(
                    {"name": title, "info_hash": info_hash, "reason": blocker}
                )
                await add_history(
                    {
                        "at": datetime.now(timezone.utc).isoformat(),
                        "action": "overdue",
                        "name": title,
                        "info_hash": info_hash,
                        "title": title,
                        "seeded_hours": seeded,
                        "reason": blocker,
                    }
                )
                log.info("「%s」已做种 %s 小时仍保留：%s", title or info_hash, seeded, blocker)
                try:
                    await publisher.publish(
                        SEED_OVERDUE,
                        SeedOverdue(
                            name=title,
                            info_hash=info_hash,
                            title=title,
                            media_item_id=media_item_id,
                            seeded_hours=seeded,
                            hours=round(config.seed_hours + config.overdue_days * 24, 1),
                        ),
                    )
                except Exception:  # noqa: BLE001
                    log.warning("发「到期未入库」事件失败", exc_info=True)

        # 收敛状态：60 天没动静的记账清掉，别让插件数据无限长
        cutoff = now - 60 * 86400
        seen = {k: v for k, v in seen.items() if num(v) >= cutoff}
        removed = {k: v for k, v in removed.items() if num(v) >= cutoff}
        await remember(SEEN_KEY, dict(list(seen.items())[-SEEN_LIMIT:]))
        await remember(REMOVED_KEY, dict(list(removed.items())[-SEEN_LIMIT:]))
        stats = await data(STATS_KEY, {}) or {}
        if not dry:
            stats["removed"] = int(stats.get("removed", 0)) + report["removed"]
            stats["reminded"] = int(stats.get("reminded", 0)) + report["reminded"]
        stats["last_run_at"] = report["at"]
        stats["last_scanned"] = report["scanned"]
        await remember(STATS_KEY, stats)
        log.info(
            "%s：扫了 %d 个任务，清理 %d，提醒 %d",
            "演练" if dry else "做种定时",
            report["scanned"],
            report["removed"],
            report["reminded"],
        )
        return report

    # ---- 定时任务（「设置 → 定时任务」里可改周期 / 暂停 / 立即运行）----------
    async def scheduled_scan() -> None:
        try:
            await run_scan()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- 一轮失败不能让定时任务停摆
            log.warning("做种定时扫描失败，下一轮再试", exc_info=True)

    ctx.contribute(
        SCHEDULED_TASKS,
        "scan",
        TaskDefinition(
            key=SCAN_KEY,
            title="做种定时清理扫描",
            handler=scheduled_scan,
            default_trigger_type=TriggerType.INTERVAL,
            default_interval_seconds=600,
            description="扫一遍下载器里的种子，把做种满设定时长、媒体库也已确认的种子清理掉",
        ),
    )

    # ---- 后台任务：下载完成 / 入库完成 / 用户点「立即扫描」-------------------
    async def sweep(job_ctx, payload: dict) -> dict:
        await job_ctx.update_progress(
            mode="indeterminate", phase="scan", message="扫描下载器…"
        )
        result = await run_scan(dry_run=bool(payload.get("dry_run", False)))
        if result.get("downloader_errors") and not result.get("scanned"):
            raise JobBlocked("下载器都读不到任务：" + "；".join(result["downloader_errors"][:3]))
        return result

    ctx.contribute(
        JOB_HANDLERS,
        SWEEP_JOB,
        RegisteredJobHandler(sweep, frozenset({1}), title="做种定时清理扫描"),
    )

    def name_of(downloader: dict) -> str:
        return str(downloader.get("name") or downloader.get("id"))

    async def task_index() -> dict:
        """任务中心的快照：这个种子是不是刷流、有没有 H&R、对应哪个条目。"""
        try:
            listing = await ops.call("dl.tasks", {}) or {}
        except OpsError as exc:
            log.debug("读任务中心失败：%s", exc.message)
            return {}
        index: dict[str, dict] = {}
        for item in items_of(listing):
            subs = [sub for sub in item.get("subscriptions") or [] if isinstance(sub, dict)]
            index[hash_key(item.get("info_hash"))] = {
                "source": item.get("source") or "",
                "hit_and_run": any(bool(sub.get("hit_and_run")) for sub in subs),
                "media_item_id": item.get("media_item_id"),
            }
        return index

    def qb_connection() -> dict:
        """插件设置里填的 QB 连接信息（下载器列表里没有密码，独立进程也拿不到）。"""
        url = (config.qb_url or "").strip().rstrip("/")
        if not url:
            return {}
        password = config.qb_password.get_secret_value() if config.qb_password else ""
        return {"url": url, "username": config.qb_username or "", "password": password}

    def local_torrents(connection: dict, index: dict) -> list[dict]:
        """按下载器的连接信息，直接问 qBittorrent 要**全部**种子。

        任务中心只列还在业务管线里的任务，做完使命的种子它不列；而「做种满多久」
        恰恰是这些老种子的事。这里用插件设置里填的连接信息自己连一次——密码不进
        权限系统、也不碰系统里的下载器配置，只在这份插件配置里。
        """
        import qbittorrentapi  # noqa: PLC0415 -- 随主程序带的库

        client = qbittorrentapi.Client(
            host=connection["url"],
            username=connection.get("username") or None,
            password=connection.get("password") or None,
            REQUESTS_ARGS={"timeout": 20},
        )
        out = []
        for torrent in client.torrents_info():
            key = hash_key(torrent.hash)
            known = index.get(key) or {}
            progress = num(getattr(torrent, "progress", 0))
            out.append(
                {
                    "info_hash": key,
                    "name": torrent.name,
                    "title": torrent.name,
                    "progress": progress,
                    "completed": progress >= 1.0 and str(getattr(torrent, "state", "")) not in {"checkingDL", "checkingUP"},
                    "size_bytes": int(num(getattr(torrent, "size", 0))),
                    "state": str(getattr(torrent, "state", "")),
                    # 做种时长以 QB 自己的记录为准（见 seeded_hours）
                    "seeding_seconds": int(num(getattr(torrent, "seeding_time", 0))),
                    "completed_on": int(num(getattr(torrent, "completion_on", 0))),
                    "added_on": int(num(getattr(torrent, "added_on", 0))),
                    "tags": [
                        tag.strip()
                        for tag in str(getattr(torrent, "tags", "") or "").split(",")
                        if tag.strip()
                    ],
                    "media_item_id": known.get("media_item_id"),
                    "hit_and_run": known.get("hit_and_run"),
                    "source": known.get("source") or "",
                }
            )
        return out

    async def idle_torrents(downloader: dict, index: dict) -> list[dict]:
        """这台下载器里的种子：优先直连下载器取全量，连不上时退回任务中心口径。"""
        connection = qb_connection()
        if connection:
            try:
                return await asyncio.to_thread(local_torrents, connection, index)
            except Exception as exc:  # noqa: BLE001 -- 连不上就退回任务中心，绝不因此误判
                log.warning(
                    "直连下载器「%s」失败（%s: %s），改用任务中心的口径",
                    name_of(downloader),
                    type(exc).__name__,
                    str(exc)[:200],
                )
        try:
            listing = await ops.call("dl.tasks", {})
        except OpsError:
            return []
        return [
            item
            for item in items_of(listing)
            if item.get("downloader_id") == downloader.get("id") and item.get("info_hash")
        ]

    async def kick(reason: str) -> None:
        # 节流：事件（下载完成 / 入库完成）可能连着来，5 分钟内只补扫一次，
        # 免得重复盯着同一个种子反复判断
        last = num(await data("last_kick", 0.0))
        now = datetime.now(timezone.utc).timestamp()
        if now - last < 300:
            return
        await remember("last_kick", int(now))
        try:
            await jobs.create(SWEEP_JOB, input={"dry_run": config.dry_run}, dedupe_key="sweep")
        except Exception:  # noqa: BLE001 -- 排不上也无所谓，定时任务稍后还会扫
            log.debug("%s 后的立刻扫描没排上", reason, exc_info=True)

    async def on_download_event(event) -> None:
        # 下载完成 / 入库完成：记下这一刻，尽量让「首见时间」贴近真正开始做种的时刻，
        # 再顺手扫一轮（刚入库的种子往往就是该清的那批）
        if getattr(event, "info_hash", None):
            seen = await data(SEEN_KEY, {}) or {}
            key = hash_key(event.info_hash)
            if key not in seen:
                seen[key] = int(datetime.now(timezone.utc).timestamp())
                await remember(SEEN_KEY, seen)
        await kick("下载完成")

    ctx.on(DOWNLOAD_COMPLETED, on_download_event, id="completed")
    ctx.on(LIBRARY_INGEST_COMPLETED, on_download_event, id="ingested")

    async def on_opened(event) -> None:
        media = getattr(event, "media", None)
        item_id = getattr(media, "id", None)
        if item_id is None:
            return
        opened = await data(OPEN_KEY, {}) or {}
        opened[str(item_id)] = int(datetime.now(timezone.utc).timestamp())
        horizon = datetime.now(timezone.utc).timestamp() - 3 * 86400
        await remember(OPEN_KEY, {k: v for k, v in opened.items() if num(v) >= horizon})

    ctx.on(LIBRARY_ITEM_VIEWED, on_opened, id="viewed")
    ctx.on(PLAYBACK_STARTED, on_opened, id="playback")

    # ---- 接口（同时是 mclaw 命令，AI 助手可以直接调）------------------------
    router = APIRouter()

    @router.get(
        "/status",
        operation_id=f"plugins.{PLUGIN_ID}.status",
        summary="做种定时：设置、累计统计与最近的处理记录",
    )
    async def status() -> dict:
        return {
            "settings": config.model_dump(),
            "stats": await data(STATS_KEY, {}) or {},
            "tracking": len(await data(SEEN_KEY, {}) or {}),
            "history": (await data(HISTORY_KEY, []) or [])[:50],
        }

    @router.post(
        "/scan",
        operation_id=f"plugins.{PLUGIN_ID}.scan",
        summary="做种定时：立刻扫描一次（dry_run 不传时按设置里的演练开关）",
    )
    async def scan(
        dry_run: bool | None = None,
        only: list[str] | None = None,
    ) -> dict:
        return await run_scan(dry_run=dry_run, only=only)

    @router.get(
        "/preview",
        operation_id=f"plugins.{PLUGIN_ID}.preview",
        summary="做种定时：只读预演，现在哪些种子会到期、各自为什么删 / 为什么留",
    )
    async def preview() -> dict:
        result = await run_scan(dry_run=True)
        return {
            "would_remove": result["removed"],
            "would_remind": result["reminded"],
            "items": result["removed_items"],
            "overdue": result["overdue_items"],
            "downloader_errors": result["downloader_errors"],
        }

    @router.get(
        "/queue",
        operation_id=f"plugins.{PLUGIN_ID}.queue",
        summary="做种定时：每个种子还要做种多久、什么时候清理（只读，按时间排好）",
    )
    async def queue() -> dict:
      try:
        now = datetime.now(timezone.utc).timestamp()
        seen = await data(SEEN_KEY, {}) or {}
        opened = await data(OPEN_KEY, {}) or {}
        index = await task_index()
        rows: list[dict] = []
        try:
            downloaders = await ops.call("dl.list", {}) or []
        except OpsError as exc:
            raise HTTPException(status_code=502, detail=f"读取下载器失败：{exc.message}") from exc
        for downloader in downloaders:
            if not downloader.get("usable", True):
                continue
            for brief in await idle_torrents(downloader, index):
                info_hash = hash_key(brief.get("info_hash"))
                first_seen = num(seen.get(info_hash), 0.0)
                elapsed = round(seeded_hours(brief, first_seen, now), 1)
                media_item_id = brief.get("media_item_id")
                group = state_group(brief)
                facts = {
                    "group": group,
                    "watching": recently_opened(opened, media_item_id, now)
                    and config.protect_recent_open,
                    "boost": brief.get("source") == "boost",
                    "hr": bool(brief.get("hit_and_run")),
                }
                blocker = removal_blocker(brief, facts, config, first_seen=first_seen, now=now)
                started = seeded_start(brief, first_seen, now) or first_seen
                planned = (started + config.seed_hours * 3600) if started else None
                if blocker:
                    state = "保留"
                elif elapsed >= config.seed_hours:
                    state = "已到期"
                else:
                    state = "待到期"
                rows.append(
                    {
                        "name": brief.get("name"),
                        "info_hash": info_hash,
                        "state": state,
                        "group": group,
                        "group_title": GROUP_TITLES.get(group, group),
                        "seeded_hours": elapsed,
                        "remaining_hours": round(max(0.0, config.seed_hours - elapsed), 1),
                        "removes_at": (
                            datetime.fromtimestamp(planned, timezone.utc).isoformat()
                            if planned
                            else None
                        ),
                        "note": blocker or "",
                    }
                )
        rows.sort(key=lambda row: (row["removes_at"] or "9", -num(row.get("seeded_hours"))))
        counts: dict[str, int] = {}
        for row in rows:
            key = row["group"]
            counts[key] = counts.get(key, 0) + 1
        log.info("队列读数：%d 个任务，最早到期 %s", len(rows), rows[0]["removes_at"] if rows else "-")
        return {
            "seeded_hours_limit": config.seed_hours,
            "dry_run": config.dry_run,
            # 每一类有多少个、这一类现在勾没勾处理
            "groups": [
                {
                    "group": group,
                    "title": GROUP_TITLES[group],
                    "count": counts.get(group, 0),
                    "handled": group_allowed(group, config),
                }
                for group in ["seeding", "missing", "downloading", "errored", "checking", "other"]
                if counts.get(group, 0)
            ],
            "count": len(rows),
            "items": rows,
        }
      except Exception as exc:  # noqa: BLE001 -- 排查期把真话给出去
        log.warning("队列读数失败：%r", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"队列读数失败：{exc!r}") from exc

    routes = ctx.use(PLUGIN_ROUTES)
    routes.mount(ctx, router)

    log.info(
        "%s 已加载：做种满 %s 小时清理（%s）",
        PLUGIN_ID,
        config.seed_hours,
        "演练模式" if config.dry_run else ("连数据删除" if config.delete_files else "保留数据文件"),
    )
