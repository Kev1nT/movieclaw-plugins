/* 插件页通用交互 v1（配套 theme.css）
 *
 * 引入：<script src="theme-ui.js"></script>，拿到 window.MCUI。
 * 只收「每个插件页都要写一遍」的那几件事：取接口、浮起提示、设置抽屉、
 * 选项弹窗、确认弹窗、Esc 关闭、卡片点开。
 *
 * 刻意不做的：业务判断、数据加工。那些写进页面自己的 logic.js（纯函数，能用 node 跑正反例）。
 */
(function (root) {
  "use strict";

  const $ = (sel, el) => (el || document).querySelector(sel);
  const $$ = (sel, el) => Array.from((el || document).querySelectorAll(sel));

  /** HTML 转义：任何拼进 innerHTML 的接口字段都要过它。 */
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  /** 取接口：同源、自动带登录；外面包一层 {success, code, message, data}，这里只返回 data。
   *  base 缺省是 /api/v1（主程序接口）；调插件自己的后端传 "/api/v1/plugins/<id>"。 */
  async function api(path, options) {
    const res = await fetch(path, Object.assign({ credentials: "same-origin", headers: {} }, options || {}));
    const body = await res.json().catch(() => null);
    if (!res.ok || (body && body.success === false)) {
      const detail = body && (body.message || body.detail);
      const err = new Error(detail || ("请求失败（" + res.status + "）"));
      err.status = res.status;
      throw err;
    }
    if (body && typeof body === "object" && "success" in body && "data" in body) return body.data;
    return body;
  }

  /** POST / PUT 带 JSON；body 缺省是 {}。 */
  function post(path, body, method) {
    return api(path, {
      method: method || "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  }

  /* ================= 浮起提示 ================= */
  let toastEl = null;
  function toast(text, ms) {
    if (!toastEl) {
      toastEl = document.getElementById("toast");
      if (!toastEl) {
        toastEl = document.createElement("div");
        toastEl.id = "toast";
        toastEl.style.opacity = "0";
        document.body.appendChild(toastEl);
      }
    }
    toastEl.textContent = text;
    toastEl.style.opacity = "1";
    clearTimeout(toastEl._t);
    toastEl._t = setTimeout(() => { toastEl.style.opacity = "0"; }, ms || 2200);
  }

  /* ================= 设置抽屉 ================= */
  // 约定：结构就是 theme.css 第 9 节那套（#drawer + #scrim + 取消/保存按钮）
  /** 打开设置抽屉（420px、右上角、透明遮罩）。
   *  看一条数据的内容用 openDetail()，不要塞进这里。
   *  render 是内容绘制函数：**先画再显示**，不会先闪一格空白。 */
  function openDrawer(render) {
    if (typeof render === "function") { try { render(); } catch (err) { console.error("[plugin-page]", err); } }
    const d = $("#drawer"), s = $("#scrim");
    if (d) { d.classList.add("on"); d.setAttribute("aria-hidden", "false"); }
    if (s) s.classList.add("on");
  }
  function closeDrawer() {
    closePicker(false);          // 抽屉一关，挂在上面的选项弹窗也一起收
    const d = $("#drawer"), s = $("#scrim");
    if (d) { d.classList.remove("on"); d.setAttribute("aria-hidden", "true"); }
    if (s) s.classList.remove("on");
  }

  /* ================= 详情弹窗（居中，看一条数据用） ================= */
  /* 与设置抽屉的分工（SKILL.md 第 1.1 节）：改配置走 .drawer，看内容走这里。
     结构（与骨架页一致）：
       <div class="detail-scrim" id="detail-scrim"></div>
       <div class="detail-modal" id="detail-modal">
         <header><h2 id="detail-title"></h2><button class="icon-btn" id="detail-close">×</button></header>
         <div class="body" id="detail-body"></div>
         <footer id="detail-footer"></footer>
       </div>
     用法：MCUI.openDetail({ title, html, footer, onOpen })   footer＝尾部按钮的 HTML，可省。 */
  function openDetail(opt) {
    const o = opt || {};
    const m = $("#detail-modal"), sc = $("#detail-scrim");
    if (!m) return null;
    const titleEl = $("#detail-title"), bodyEl = $("#detail-body"), footEl = $("#detail-footer");
    if (titleEl) titleEl.textContent = o.title || "";
    if (bodyEl) bodyEl.innerHTML = o.html || "";
    if (footEl) { footEl.innerHTML = o.footer || ""; footEl.style.display = o.footer ? "" : "none"; }
    if (sc) sc.classList.add("on");
    m.classList.add("on");
    m.setAttribute("aria-hidden", "false");
    // Esc / 点遮罩 / 点关闭按钮 都收起
    (o.onOpen || noop)(m, bodyEl);
    return m;
  }
  function closeDetail() {
    const m = $("#detail-modal"), sc = $("#detail-scrim");
    if (m) { m.classList.remove("on"); m.setAttribute("aria-hidden", "true"); }
    if (sc) sc.classList.remove("on");
  }
  function isDetailOpen() {
    const m = $("#detail-modal");
    return !!(m && m.classList.contains("on"));
  }
  function noop() {}

  /* ================= 选项弹窗 ================= */
  // 点一行「剧集没写季号时  按第一季订 ›」→ 弹窗长在设置抽屉的位置上，选完点「完成」才生效
  let pickerApply = null;
  function openPicker(title, html, doneLabel) {
    $("#picker-title").textContent = title;
    $("#picker-body").innerHTML = html;
    $("#picker-done").textContent = doneLabel || "完成";
    $("#picker").classList.add("on");
    $("#picker-scrim").classList.add("on");
  }
  function closePicker(apply) {
    const p = $("#picker");
    if (!p) return;
    p.classList.remove("on");
    $("#picker-scrim").classList.remove("on");
    $("#picker-body").innerHTML = "";
    const fn = pickerApply;
    pickerApply = null;
    if (apply && fn) fn();
  }
  /** 单选弹窗：options = [{value, title, desc}]，onPick(value) 在点「完成」后调用。 */
  function singlePicker(title, options, current, onPick) {
    const html = options.map((o) =>
      '<button type="button" class="opt" data-value="' + esc(o.value) + '" aria-selected="' + (current === o.value) + '">' +
      '<span class="mark"></span><span class="txt"><b>' + esc(o.title) + "</b>" +
      (o.desc ? "<small>" + esc(o.desc) + "</small>" : "") + "</span></button>"
    ).join("");
    let temp = current;
    openPicker(title, html, "完成");
    $$("#picker-body .opt").forEach((btn) => {
      btn.addEventListener("click", () => {
        temp = btn.dataset.value;
        $$("#picker-body .opt").forEach((other) => other.setAttribute("aria-selected", String(other === btn)));
      });
    });
    pickerApply = () => onPick(temp);
  }

  /* ================= 确认弹窗 ================= */
  function confirmBox(title, text, okLabel, onYes, note) {
    const box = $("#confirm");
    if (!box) { if (window.confirm(text)) onYes(); return; }
    $("#confirm-title").textContent = title;
    $("#confirm-text").textContent = text;
    const n = $("#confirm-note");
    if (n) { n.textContent = note || ""; n.style.display = note ? "" : "none"; }
    const okBtn = $("#confirm-ok");
    okBtn.textContent = okLabel || "确定";
    box.classList.add("on");
    const close = () => { box.classList.remove("on"); okBtn.onclick = null; };
    $("#confirm-cancel").onclick = close;
    okBtn.onclick = async () => { close(); await onYes(); };
  }

  /* ================= 分段标签条：单行横滑 ================= */
  /* 结构：<div class="tabs"> <button class="tab">…</button> … </div>
     自动做三件事：
       1. 内容放得下 → 加 .no-scroll，去掉右侧渐隐（不留一条没必要的白雾）；
       2. 滑到最右 → 加 .at-end，右渐隐消失；滑过最左 → 加 .scrolled，左渐隐出现；
       3. 选中项（.tab.on）不在可见范围时，自动滚进来居中。 */
  function bindTabs(tabsEl) {
    const el = typeof tabsEl === "string" ? $(tabsEl) : tabsEl;
    if (!el || el.__mcTabs) return el;
    el.__mcTabs = true;

    const sync = () => {
      const max = el.scrollWidth - el.clientWidth;
      el.classList.toggle("no-scroll", max <= 1);
      el.classList.toggle("at-end", max <= 1 || el.scrollLeft >= max - 1);
      el.classList.toggle("scrolled", el.scrollLeft > 1);
    };
    el.addEventListener("scroll", sync, { passive: true });
    // 内容变了（数量从 – 变成数字、标签增删）要重算
    if (typeof ResizeObserver === "function") {
      const ro = new ResizeObserver(sync);
      ro.observe(el);
      $$(".tab", el).forEach((t) => ro.observe(t));
    }
    window.addEventListener("resize", sync);
    requestAnimationFrame(sync);
    el.__mcSync = sync;
    return el;
  }

  /** 把当前选中的标签滚到可见范围（切换标签后调用）。 */
  function revealActiveTab(tabsEl, smooth) {
    const el = typeof tabsEl === "string" ? $(tabsEl) : tabsEl;
    if (!el) return;
    const active = $(".tab.on", el) || el.querySelector(".tab.on");
    if (!active) return;
    const left = active.offsetLeft - el.offsetLeft;
    const right = left + active.offsetWidth;
    const viewLeft = el.scrollLeft;
    const viewRight = viewLeft + el.clientWidth;
    if (left < viewLeft || right > viewRight) {
      // 居中：两边各留一点余量，别贴着边缘
      const target = left - (el.clientWidth - active.offsetWidth) / 2;
      try { el.scrollTo({ left: Math.max(0, target), behavior: smooth ? "smooth" : "auto" }); }
      catch (err) { el.scrollLeft = Math.max(0, target); }
    }
    if (el.__mcSync) requestAnimationFrame(el.__mcSync);
  }

  /* ================= 主页点海报：正中浮出按钮（照豆瓣同步） ================= */
  /* 点一下海报 → 海报盖一层暗色 → 正中浮出一颗胶囊按钮；再点一下或点别处收起。
     不弹窗口、不弹菜单：海报就是点击目标，按钮贴着它，视线不用挪。
     卡片要多给一颗「二次确认」时（点右上角 × 才真正执行），用 askOnCard。
     页面接动作：给按钮加 data-act="…"，在 bindCards(scope, onAct) 的回调里处理。 */
  function bindCards(scope, onAct) {
    const rootEl = scope || document;
    rootEl.addEventListener("click", (e) => {
      // 浮层里的按钮：先处理，别冒泡成「展开/收起」
      const btn = e.target.closest(".act-btn");
      if (btn) {
        // 带 href 的浮层按钮（「前往订阅」「前往媒体库」）是真链接：
        // 必须交给浏览器跳转，不能 preventDefault，否则点了毫无反应。
        if (btn.tagName === "A" && btn.getAttribute("href")) {
          e.stopPropagation();
          return;
        }
        e.preventDefault(); e.stopPropagation();
        if (btn.disabled) return;
        if (typeof onAct === "function") onAct(btn.dataset.act, btn, btn.closest(".card"));
        return;
      }
      // 真链接（跳主程序页面）交给浏览器
      if (e.target.closest("a[href]")) return;
      const card = e.target.closest(".card");
      if (!card) return;
      if (card.dataset.noAct === "1") return;      // 纯浏览的卡片（如「在看」墙）点了不动
      e.preventDefault();
      const willOpen = !card.classList.contains("is-open") && !card.classList.contains("ask-dismiss");
      closeCards();
      if (willOpen) card.classList.add("is-open");
    });
    // 点空白处收起
    document.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".card")) return;
      closeCards();
    }, true);
  }

  /** 收起所有打开的海报浮层（含二次确认态）。 */
  function closeCards() {
    $$(".card.is-open, .card.ask-dismiss").forEach((c) => {
      c.classList.remove("is-open");
      c.classList.remove("ask-dismiss");
    });
  }

  /* ================= 就地展开一块内容（详情 / 档案） ================= */
  /* 用于「这块内容本来就属于这张卡」的场合：在卡片所在的那一行下面铺开一整块，
     网格自动换行，卡片位置不动；再点一次收起。
     与浮层按钮的分工（见 SKILL.md 第 1.1 节）：
       一句话动作  → 海报浮层按钮（.act-btn）
       一整屏内容  → 这里（.inline-panel），**不要塞进右上角设置抽屉**
     用法：
       MCUI.togglePanel(host, key, "<div>…内容…</div>", cardEl)   // cardEl＝被点的那张卡
       host = 一个 grid 容器（铺开后横跨一整行）；key 相同表示同一块，再调一次就收起。
     页面要自己保证：内容里的按钮/链接都带 target="_top" 或自己绑事件。 */
  let openPanel = null;          // { host, key, el }
  function togglePanel(host, key, html, after, onClose) {
    if (openPanel && openPanel.host === host && openPanel.key === key) { closePanel(); return null; }
    closePanel();
    if (!host) return null;
    const el = document.createElement("div");
    el.className = "inline-panel";
    el.innerHTML = html;
    // after 给了就插在那张卡后面（紧贴被点的那张），没给就放在容器末尾
    const anchorEl = after && after.parentNode === host ? after : null;
    if (anchorEl) host.insertBefore(el, anchorEl.nextSibling);
    else host.appendChild(el);
    openPanel = { host, key, el, onClose };
    return el;
  }
  /** 收起就地展开的内容；返回是否真的收了一块。 */
  function closePanel() {
    if (!openPanel) return false;
    const { el, onClose } = openPanel;
    openPanel = null;
    el.classList.add("closing");
    el.remove();
    if (typeof onClose === "function") { try { onClose(); } catch (err) { /* 页面自己出错不影响收起 */ } }
    return true;
  }
  /** 当前展开着的是不是这一块。 */
  function isPanelOpen(key) { return !!openPanel && openPanel.key === key; }

  /** 切到「二次确认」态：点 × 之后先露出 .act-btn.dismiss，点下去才真执行。 */
  function askOnCard(card) {
    if (!card) return;
    closeCards();
    card.classList.add("ask-dismiss");
  }

  /* ================= Esc 统一处理：先关最上面那层 ================= */
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (isDetailOpen()) closeDetail();
    else if ($("#picker") && $("#picker").classList.contains("on")) closePicker(false);
    else if ($("#confirm") && $("#confirm").classList.contains("on")) $("#confirm").classList.remove("on");
    else if ($("#drawer") && $("#drawer").classList.contains("on")) closeDrawer();
    else closeCards();
  });

  /* ================= 右对齐基准的重算 ================= */
  // 真正的测量在页面 <head> 里的内联脚本（见 skeleton.html / SKILL.md 第 2 节）：
  // 必须赶在正文渲染之前算好，否则会"先画好再往左跳一下"。这里只负责转屏/改窗口后重量。
  window.addEventListener("resize", function () {
    if (typeof window.__mcalignChrome === "function") requestAnimationFrame(window.__mcalignChrome);
  }, { passive: true });

  /* ================= 启动兜底 ================= */
  // 页面里没接住的报错写进 console，方便排查（主程序也会汇总插件页报错）
  window.addEventListener("error", (e) => console.error("[plugin-page]", e.message));

  const MCUI = {
    $, $$, esc, api, post, toast,
    openDrawer, closeDrawer,
    openDetail, closeDetail, isDetailOpen,
    openPicker, closePicker, singlePicker,
    bindCards, closeCards, askOnCard,
    bindTabs, revealActiveTab,
    togglePanel, closePanel, isPanelOpen,
    confirm: confirmBox,
  };
  root.MCUI = MCUI;
  if (typeof module !== "undefined" && module.exports) module.exports = MCUI;
})(typeof globalThis !== "undefined" ? globalThis : this);
