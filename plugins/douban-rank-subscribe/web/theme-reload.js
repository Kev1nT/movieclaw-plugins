/* 插件页热更新 v1（配套 theme.css / theme-ui.js）
 *
 * 解决的问题：改完插件页的 HTML / CSS / JS，用户不用刷新整页、也不用重新打包安装，
 * 页面自己发现「文件变了」，把界面就地重新拉起来。
 *
 * 主程序的静态托管对插件网页是 `Cache-Control: no-cache` + ETag：
 * 浏览器每次都会回来问一句「变了没」（文件小，代价很低），改了文件 ETag 就变，
 * 所以**只要重新加载一次**就是最新的，不需要重启服务、也不需要重新装插件。
 *
 * 三种可用性，从弱到强：
 *   1. 默认：页面自己每 N 秒问一次（GET + Range 只取响应头，看 ETag；不要用 HEAD，会 405）。
 *   2. 更快：在页面里放 <div data-reload="ready"></div>，告诉这里可以接收广播；
 *      用户在浏览器控制台执行 `MCUI.reloadHere()`，
 *      存着这张页面的所有标签立刻一起更新，不用等下一轮轮询。
 *   3. 最省事：直接按 Ctrl+R / F5 —— 本来就是最新。
 *
 * 约定：页面要能「就地重来」。做法是给 <html> 打个 data-rv，换掉它再换回来，
 * 等同于点了一次刷新，但不需要用户动手、地址和滚动位置保留一部分。
 */
(function (root) {
  "use strict";

  const cfg = Object.assign(
    {
      interval: 15000,   // 轮询间隔（毫秒）；0 或 false 关闭
      onReload: null,    // 重新加载前回调（例如先收起弹层、存草稿）
    },
    (root.MC_RELOAD_CONFIG || {})
  );

  let last = null;            // 上次拿到的 ETag 集合
  let busy = false;

  /** 取当前页面的「版本指纹」：主文档 + 页面引用的本地 js/css 的 ETag 拼起来。
   *  用 GET + Range 只读第一个字节：主程序只实现了 GET（HEAD 会回 405），
   *  这样一次也就传几个字节，和 HEAD 的代价一样。 */
  async function fingerprint() {
    const urls = [location.pathname + location.search];
    document.querySelectorAll('script[src], link[rel="stylesheet"][href]').forEach((el) => {
      const src = el.getAttribute("src") || el.getAttribute("href");
      if (src && !/^(https?:)?\/\//.test(src) && !src.startsWith("data:")) urls.push(new URL(src, location.href).pathname);
    });
    const tags = [];
    for (const url of urls) {
      try {
        const res = await fetch(url, {
          cache: "no-store",
          credentials: "same-origin",
          headers: { Range: "bytes=0-0" },
        });
        // 304（内容没变）/ 206 / 200 都可能出现；服务端没给 ETag 时退回 Last-Modified
        tags.push(res.headers.get("etag") || res.headers.get("last-modified") || "s" + res.status);
        if (res.body && res.body.cancel) { try { await res.body.cancel(); } catch (e) { /* 已经读完 */ } }
      } catch (err) {
        tags.push("x");            // 断网、接口报错：不算版本变化
      }
    }
    return tags.join("|");
  }

  /** 就地重新加载：先给页面一次收尾机会，再把 <html> 换掉（等价刷新，但不打扰用户）。 */
  function reloadNow(reason) {
    if (busy) return;
    busy = true;
    try { if (typeof cfg.onReload === "function") cfg.onReload(reason); } catch (err) { /* 页面自己出错不影响刷新 */ }
    // 记下滚动位置，重来后尽量回到原处
    let y = 0;
    try { y = window.scrollY || 0; } catch (err) { /* 忽略 */ }
    const html = document.documentElement;
    const rv = String(Date.now());
    html.setAttribute("data-rv", rv);
    // 强制重新解析：去掉再装回同一个属性
    requestAnimationFrame(() => {
      html.removeAttribute("data-rv");
      html.setAttribute("data-rv", rv);
      try { window.scrollTo(0, y); } catch (err) { /* 忽略 */ }
      busy = false;
    });
  }

  async function tick(fromButton) {
    const now = await fingerprint();
    if (last === null) { last = now; return false; }
    if (now !== last) {
      last = now;
      reloadNow(fromButton ? "manual" : "changed");
      return true;
    }
    return false;
  }

  /* ---------------- 广播：同一台机器上开着这张页面的标签一起更新 ---------------- */
  let ch = null;
  try {
    ch = new BroadcastChannel("mc-plugin-page-reload");
    ch.onmessage = (e) => {
      if (e && e.data && e.data.action === "reload" && (!e.data.app || e.data.app === cfg.appId)) reloadNow("broadcast");
    };
  } catch (err) { ch = null; }   // 老浏览器没有 BroadcastChannel：只靠轮询

  /** 让所有开着这张插件的标签立刻更新（在任意一个标签的控制台里调这个）。 */
  function reloadHere() {
    if (ch) { try { ch.postMessage({ action: "reload", app: cfg.appId }); return; } catch (err) { /* 退回到只刷新自己 */ } }
    reloadNow("manual");
  }

  /** 只刷新自己所在的页面，不通知别人。 */
  function reloadMe() { reloadNow("manual"); }

  let timer = null;
  function start() {
    if (cfg.interval === false || !cfg.interval) return;
    stop();
    timer = setInterval(() => { tick(false).catch(() => null); }, cfg.interval);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") tick(false).catch(() => null);
    });
  }
  function stop() { if (timer) { clearInterval(timer); timer = null; } }

  /* 说明：不要用 HEAD 探版本。主程序的插件页静态托管只实现 GET，
     HEAD 会对每个文件回 405 并在日志里留下一串 WARNING（真踩过）。
     GET + Range: bytes=0-0 只取回响应头，代价与 HEAD 相同。 */

  /* ---------------- 自动接入 ---------------- */
  // 页面里加了 <div data-reload="ready"></div> 就表示「这个页面支持就地重来」：
  // 自动开始盯、并在控制台留一句提示，用户知道去哪儿点。
  function boot() {
    if (!document.querySelector('[data-reload="ready"]')) return;
    tick(true).catch(() => null);          // 先记下当前版本，不清空页面
    start();
    try {
      console.info(
        "%c[插件页热更新] 已开启：" + (cfg.interval / 1000) + " 秒检查一次。" +
        "改完 web/ 里的文件后，在控制台执行 MCUI.reloadHere() 让所有标签立刻更新，" +
        "或直接按 Ctrl+R 自己刷新。",
        "color:#46d369"
      );
    } catch (err) { /* 忽略 */ }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();

  root.MCUI = root.MCUI || {};
  root.MCUI.reloadHere = reloadHere;   // 通知所有标签（含自己）
  root.MCUI.reloadMe = reloadMe;       // 只刷新自己
  root.MCUI.reloadCheck = () => tick(true);   // 立刻检查一次
  root.MCUI.reloadWatch = start;
  root.MCUI.reloadUnwatch = stop;
  root.MCUI.reloadConfig = cfg;
})(typeof globalThis !== "undefined" ? globalThis : this);
