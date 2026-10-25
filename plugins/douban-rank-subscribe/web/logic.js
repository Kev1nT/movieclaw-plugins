/* 豆瓣榜单订阅页面的纯逻辑：不碰 DOM，便于用 node 跑正反例。
 *
 * 页面脚本通过 <script src="logic.js"> 引入，接口挂在 window.RankLogic 上；
 * 测试时 require() 拿到的也是同一组函数。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.RankLogic = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  // 与后端 Settings 里 rank_* 字段严格一一对应（后端有自测守着）
  const RANKS = [
    { field: "movie_weekly_best", title: "一周口碑电影榜", kind: "movie", ref: "douban:movie:movie_weekly_best" },
    { field: "movie_hot_gaia", title: "豆瓣热门电影", kind: "movie", ref: "douban:movie:movie_hot_gaia" },
    { field: "movie_showing", title: "影院热映", kind: "movie", ref: "douban:movie:movie_showing" },
    { field: "movie_soon", title: "即将上映", kind: "movie", ref: "douban:movie:movie_soon" },
    { field: "movie_high_score", title: "豆瓣高分电影", kind: "movie", ref: "douban:movie:movie_high_score" },
    { field: "movie_top250", title: "电影 Top 250", kind: "movie", ref: "douban:movie:movie_top250" },
    { field: "movie_classic", title: "经典电影", kind: "movie", ref: "douban:movie:movie_classic" },
    { field: "movie_scifi", title: "高分经典科幻片榜", kind: "movie", ref: "douban:movie:movie_scifi" },
    { field: "movie_comedy", title: "高分经典喜剧片榜", kind: "movie", ref: "douban:movie:movie_comedy" },
    { field: "movie_action", title: "高分经典动作片榜", kind: "movie", ref: "douban:movie:movie_action" },
    { field: "movie_love", title: "高分经典爱情片榜", kind: "movie", ref: "douban:movie:movie_love" },
    { field: "tv_real_time_hotest", title: "实时热门剧集", kind: "tv", ref: "douban:tv:tv_real_time_hotest" },
    { field: "tv_chinese_best_weekly", title: "华语口碑剧集榜", kind: "tv", ref: "douban:tv:tv_chinese_best_weekly" },
    { field: "tv_global_best_weekly", title: "全球口碑剧集榜", kind: "tv", ref: "douban:tv:tv_global_best_weekly" },
    { field: "tv_hot", title: "近期热门剧集", kind: "tv", ref: "douban:tv:tv_hot" },
    { field: "tv_domestic", title: "近期热门国产剧", kind: "tv", ref: "douban:tv:tv_domestic" },
    { field: "tv_american", title: "近期热门美剧", kind: "tv", ref: "douban:tv:tv_american" },
    { field: "tv_japanese", title: "近期热门日剧", kind: "tv", ref: "douban:tv:tv_japanese" },
    { field: "tv_korean", title: "近期热门韩剧", kind: "tv", ref: "douban:tv:tv_korean" },
    { field: "tv_animation", title: "近期热门动画", kind: "tv", ref: "douban:tv:tv_animation" },
    { field: "show_domestic", title: "近期热门国内综艺", kind: "tv", ref: "douban:tv:show_domestic" },
    { field: "show_foreign", title: "近期热门国外综艺", kind: "tv", ref: "douban:tv:show_foreign" },
  ];

  /** 配置里勾了哪些榜单（按 RANKS 顺序返回）。 */
  function enabledRanks(values, kind) {
    const v = values || {};
    return RANKS.filter((r) => (kind ? r.kind === kind : true)).filter((r) => v["rank_" + r.field] === true);
  }

  /** 评分门槛：低于门槛的剧照不显示；没给评分（0 / 空）的不算低分。
   *  注意与后端 pick_new 同一口径：0 表示来源没评分，不淘汰。 */
  function passesRating(rating, minRating) {
    const min = Number(minRating) || 0;
    if (min <= 0) return true;
    const r = Number(rating);
    if (!isFinite(r) || r <= 0) return true;
    return r >= min;
  }

  /** 过滤一个片单的条目：保留评分达标的。 */
  function filterTitles(titles, minRating) {
    return (titles || []).filter((t) => t && passesRating(t.provider_rating, minRating));
  }

  /* 远程图片要走主程序的图片代理（images.proxy）：
   * 豆瓣图床直连带 Referer 校验，浏览器里会 418；代理会补 Referer、缓存、派生尺寸。
   * 与主程序前端同一套口径：http(s) 一律转成 images/proxy?url=<编码>。 */
  const PROXY_BASE = "/api/v1/images/proxy?url=";

  function imageUrl(url, variant) {
    if (!url) return "";
    if (!/^https?:\/\//i.test(url)) return url;      // 已经是本地地址（/images/...）就不再套一层
    let out = PROXY_BASE + encodeURIComponent(url);
    if (variant) out += "&variant=" + encodeURIComponent(variant);
    return out;
  }

  /** 海报地址：优先豆瓣给的海报；没有时用背景图顶替。走代理 + 竖版档位。 */
  function posterOf(title) {
    if (!title) return "";
    return imageUrl(title.poster_url || title.backdrop_url || "", "poster-card");
  }

  /** 头像/剧照等通用图片：统一走代理。 */
  function plainImage(url) {
    return imageUrl(url);
  }

  /** 评分展示：0 / 缺失不显示分数，避免满屏 0.0。 */
  function ratingText(rating) {
    const r = Number(rating);
    return isFinite(r) && r > 0 ? r.toFixed(1) : "";
  }

  /** 详情页第一行 meta（评分另算）：年份 / 规模 / 徽标，空的不留空档。
   *  与发现页一致：评分在最前，然后是年份、片长，不塞地区与类型。 */
  function metaLine(details) {
    const t = (details && details.title) || {};
    const parts = [];
    if (t.release_year) parts.push(String(t.release_year));
    if (t.extent_label) parts.push(t.extent_label);
    return parts.join(" · ");
  }

  /** 第二行：地区 · 语言 ｜ 类型。与发现页同一口径。 */
  function countryGenreLine(details) {
    const t = (details && details.title) || {};
    const m = (details && details.metadata) || {};
    const place = [m.country, m.language].filter(Boolean).join(" · ");
    const genres = (t.genres || []).join(" · ");
    return { place, genres };
  }

  /** 上映/首播：电影写上映日期，剧集写首播日期。 */
  function releaseLine(details) {
    const m = (details && details.metadata) || {};
    const t = (details && details.title) || {};
    if (!m.released) return "";
    const label = t.media_type === "tv" ? "首播日期" : "上映日期";
    return label + " · " + m.released;
  }

  /** 简介是否长到需要「展开全文」（发现页按 4 行截断）。 */
  function needsExpand(overview) {
    return String(overview || "").length > 140;
  }

  /** 详情页演职员：导演在前、演员在后。
   *  与发现页一致：电影标「导演」，剧集标「主创」，演员标「饰 XXX」。 */
  function castList(details, limit) {
    const m = (details && details.metadata) || {};
    const t = (details && details.title) || {};
    const cap = limit || 12;
    const directorCredit = t.media_type === "tv" ? "主创" : "导演";
    const out = [];
    const directors = m.director_credits && m.director_credits.length
      ? m.director_credits
      : (m.directors || []).map((name) => ({ name }));
    directors.forEach((p) => {
      if (p && p.name) out.push({ name: p.name, credit: directorCredit, avatar: p.avatar_url || "" });
    });
    (m.cast || []).forEach((p) => {
      if (p && p.name) out.push({ name: p.name, credit: p.role ? "饰 " + p.role : "", avatar: p.avatar_url || "" });
    });
    return out.slice(0, cap);
  }

  /** 详情页的大背景：**统一用竖版海报**，放大模糊后铺底。
   *  不再区分剧照/海报——豆瓣剧照尺寸小、清晰度不足，统一走海报更稳。
   */
  function heroVisual(details) {
    if (!details) return { url: "", kind: "none" };
    const t = details.title || {};
    // 一律用竖版海报当背景（放大模糊由页面样式负责），不再挑剧照
    if (t.poster_url) return { url: imageUrl(t.poster_url), kind: "poster" };
    return { url: "", kind: "none" };
  }

  /** 只要地址（兼容旧调用点）。 */
  function heroImage(details) {
    return heroVisual(details).url;
  }

  /** 有没有可用的背景画面。 */
  function hasBackdrop(details) {
    return Boolean(heroVisual(details).url);
  }

  /** 从 title_ref（douban:123）取出豆瓣 ID；不是豆瓣条目返回空串。 */
  function doubanIdOf(titleRef) {
    const m = /^douban:(\d+)$/.exec(String(titleRef || ""));
    return m ? m[1] : "";
  }

  /** 在一份订阅列表里找这条作品对应的订阅。
   *  订阅条目的 media.douban_id 是判断依据（榜单里都是豆瓣条目）。 */
  function findSubscription(subs, titleRef) {
    const dbId = doubanIdOf(titleRef);
    if (!dbId) return null;
    for (const sub of subs || []) {
      const media = (sub && sub.media) || {};
      if (media.douban_id && String(media.douban_id) === dbId) return sub;
    }
    return null;
  }

  /** 订阅状态的中文说明（与系统详情页一致）。 */
  const SUB_STATUS = {
    active: "追踪中",
    completed: "已收齐",
    paused: "已暂停",
  };

  function subscriptionLabel(sub) {
    if (!sub) return "";
    const text = SUB_STATUS[sub.status] || "追踪中";
    return "已订阅 · " + text;
  }

  /** 海报角标用的短文案：只有「已订阅」。
   *  角标空间小，完整状态（追踪中/已收齐）留在详情页显示。 */
  function subscriptionShort(sub) {
    return sub ? "已订阅" : "";
  }

  /** 订阅详情页地址。 */
  function subscriptionHref(sub) {
    return sub && sub.id ? "/subscriptions/" + sub.id : "/subscriptions";
  }

  /** 已入库时跳转到媒体库条目页的地址。
   *  详情接口的 library_links 里才有 library_id（library_status 只有 media_item_id），
   *  优先取第一条；没有时返回空串。 */
  function libraryHref(details) {
    const links = (details && details.library_links) || [];
    if (!links.length) return "";
    const first = links[0] || {};
    if (!first.media_item_id) return "";
    // 电影/剧集条目页：/library/{library_id}/item/{media_item_id}
    if (first.library_id) {
      return "/library/" + first.library_id + "/item/" + first.media_item_id;
    }
    return "/library";
  }

  /** 已入库条目的入口文案：有库名就用库名，让用户知道会跳到哪。 */
  function libraryLabel(details) {
    const links = (details && details.library_links) || [];
    const name = links.length && (links[0].library_name || "");
    return name ? "在「" + name + "」查看" : "在媒体库查看";
  }

  /** 详情页顶栏的搜索落点：与主程序「搜索资源」同一套参数（q + also）。 */
  function searchUrl(details) {
    const t = (details && details.title) || {};
    const q = new URLSearchParams();
    const title = (t.title || "").trim();
    if (title) q.set("q", title);
    else q.set("browse", "1");
    // 原名一起搜（与发现页一样），去重、去掉与中文名相同的
    const also = new Set();
    [t.original_title, (details && details.metadata && details.metadata.english_title) || ""]
      .map((s) => String(s || "").trim())
      .filter((s) => s && s !== title)
      .forEach((s) => also.add(s));
    also.forEach((s) => q.append("also", s));
    return "/search?" + q.toString();
  }

  /** 主程序里「去订阅」的落点：发现页按类型打开。 */
  function discoverUrl(kind) {
    return kind === "tv" ? "/discover/tv" : "/discover/movie";
  }

  return {
    RANKS,
    enabledRanks,
    passesRating,
    filterTitles,
    posterOf,
    ratingText,
    metaLine,
    countryGenreLine,
    releaseLine,
    needsExpand,
    castList,
    heroImage,
    hasBackdrop,
    libraryHref,
    libraryLabel,
    doubanIdOf,
    findSubscription,
    subscriptionLabel,
    subscriptionShort,
    subscriptionHref,
    searchUrl,
    imageUrl,
    heroVisual,
    plainImage,
    discoverUrl,
  };
});
