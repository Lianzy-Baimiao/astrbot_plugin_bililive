(function () {
  "use strict";


  function byId(id) { return document.getElementById(id); }

  var toastTimer = null;
  function toast(msg, isErr) {
    var el = byId("toast");
    el.textContent = String(msg || "");
    el.className = "toast show" + (isErr ? " err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.className = "toast"; }, 3200);
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // 面板跑在 AstrBot 仪表盘的 iframe 里，浏览器会屏蔽 window.prompt/confirm，
  // 所以用页内弹窗替代（否则确认框点了没反应）。
  function _modal(opts) {
    return new Promise(function (resolve) {
      var mask = byId("modalMask");
      var input = byId("modalInput");
      byId("modalTitle").textContent = opts.title || "";
      byId("modalMsg").textContent = opts.message || "";
      input.hidden = !opts.prompt;
      if (opts.prompt) { input.value = opts.value || ""; }
      mask.hidden = false;
      var previousFocus = document.activeElement;
      byId("modalOk").focus();
      function cancelOnEscape(e) {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); done(opts.prompt ? null : false); }
      }
      mask.addEventListener("keydown", cancelOnEscape);
      if (opts.prompt) { input.focus(); input.select(); }
      function done(val) {
        mask.hidden = true;
        mask.removeEventListener("keydown", cancelOnEscape);
        if (previousFocus && previousFocus.focus) previousFocus.focus();
        byId("modalOk").onclick = null;
        byId("modalCancel").onclick = null;
        input.onkeydown = null;
        resolve(val);
      }
      byId("modalOk").onclick = function () { done(opts.prompt ? input.value : true); };
      byId("modalCancel").onclick = function () { done(opts.prompt ? null : false); };
      input.onkeydown = function (e) {
        if (e.key === "Enter") { e.preventDefault(); done(input.value); }
        else if (e.key === "Escape") { e.preventDefault(); done(null); }
      };
    });
  }
  function promptModal(title, message, value) {
    return _modal({ prompt: true, title: title, message: message, value: value || "" });
  }
  function confirmModal(message, title) {
    return _modal({ prompt: false, title: title || "确认", message: message });
  }

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(0) + " KB";
    return n + " B";
  }

  // 后端统一返回 {status, message, data}；status 不是 ok 就抛出 message。
  function unwrap(resp) {
    if (resp && typeof resp === "object" && "status" in resp) {
      if (String(resp.status).toLowerCase() !== "ok") {
        throw new Error(resp.message || "请求失败");
      }
      return resp.data || {};
    }
    return resp || {};
  }

  var _bridgeReady = null;
  function waitForBridge(timeoutMs) {
    if (_bridgeReady) return _bridgeReady;
    _bridgeReady = new Promise(function (resolve, reject) {
      var pollTimer;
      var expired = false;
      var timeout = setTimeout(function () {
        expired = true;
        clearTimeout(pollTimer);
        reject(new Error("面板桥接不可用，请在 AstrBot WebUI 里打开本页面"));
      }, timeoutMs || 8000);
      function finish(error, bridge) {
        if (expired) return;
        clearTimeout(timeout);
        if (error) reject(error);
        else resolve(bridge);
      }
      (function poll() {
        var bridge = window.AstrBotPluginPage;
        if (!bridge) { pollTimer = setTimeout(poll, 32); return; }
        Promise.resolve().then(function () {
          if (typeof bridge.ready === "function") return bridge.ready();
        }).then(function () { finish(null, bridge); }, function (err) { finish(err); });
      })();
    }).catch(function (err) {
      _bridgeReady = null;
      throw err;
    });
    return _bridgeReady;
  }

  function apiGet(path, params) {
    return window.PanelUI.request(waitForBridge().then(function (bridge) {
      return bridge.apiGet(path, params || {}).then(unwrap);
    }));
  }

  function apiPost(path, body) {
    return window.PanelUI.request(waitForBridge().then(function (bridge) {
      return bridge.apiPost(path, body || {}).then(unwrap);
    }));
  }

  function fail(err) { toast((err && err.message) || "操作失败", true); }

  function initTheme() { /* Theme is owned by shell.js. */ }


  var state = {
    meta: {},
    status: {},
    kind: "live",            // 当前 tab：live / dynamic
    subs: { live: { rows: [] }, dynamic: { rows: [] } },
    groups: [],              // 候选群：{value,label,group_id,group_name,source}
    notify: [],
    editing: null,           // {uid, targets:{umo: at_all_bool}}，null = 新增
    modalPicked: {}          // 弹窗里勾选的群：umo -> true
  };

  function kindLabel(kind) {
    return kind === "dynamic" ? "动态订阅" : "开播订阅";
  }

  // ---------- 渲染：状态 ----------

  function renderStatus() {
    var st = state.status || {};
    var pill = byId("runPill");
    if (!st.session_ok) {
      pill.textContent = "HTTP 会话异常";
      pill.className = "pill off";
    } else if (st.monitor_running) {
      pill.textContent = "监控运行中";
      pill.className = "pill";
    } else {
      pill.textContent = "监控未启动";
      pill.className = "pill warn";
    }
    var live = st.check_interval + " 秒" + (st.backoff_live ? " → 退避 " + st.current_interval + " 秒" : "");
    var dyn = st.dynamic_check_interval + " 秒" + (st.backoff_dynamic ? " → 退避 " + st.dyn_current_interval + " 秒" : "");
    byId("stats").innerHTML = [
      ['<div class="stat"><div class="n">', st.monitors || 0, ' / ', st.max_monitors || 0, '</div><div class="l">监控 UP 主</div></div>'],
      ['<div class="stat"><div class="n">', st.monitor_links || 0, '</div><div class="l">开播群订阅</div></div>'],
      ['<div class="stat"><div class="n">', st.dyn_monitors || 0, '</div><div class="l">动态 UP 主</div></div>'],
      ['<div class="stat"><div class="n">', st.dyn_baseline || 0, '</div><div class="l">动态基线</div></div>'],
      ['<div class="stat"><div class="n">', esc(live), '</div><div class="l">开播检查间隔</div></div>'],
      ['<div class="stat"><div class="n">', esc(dyn), '</div><div class="l">动态检查间隔</div></div>'],
      ['<div class="stat"><div class="n">', st.cookie_set ? "已配置" : "匿名", '</div><div class="l">B站 Cookie</div></div>']
    ].map(function (p) { return p.join(""); }).join("");
  }

  // ---------- 渲染：订阅矩阵 ----------

  function currentRows() {
    var box = state.subs[state.kind] || {};
    return box.rows || [];
  }

  function renderSubs() {
    var tb = byId("subBody");
    byId("tabLive").className = "tab" + (state.kind === "live" ? " on" : "");
    byId("tabDynamic").className = "tab" + (state.kind === "dynamic" ? " on" : "");
    var box = state.subs[state.kind] || {};
    var invalid = box.invalid_lines || [];
    byId("subHint").innerHTML = "当前：" + esc(kindLabel(state.kind)) +
      "，共 <b>" + currentRows().length + "</b> 个 UP 主、<b>" + (box.group_links || 0) + "</b> 条群订阅。" +
      (invalid.length ? ' <span class="tag warn">' + invalid.length + " 行配置无法解析</span>" : "") +
      " 保存后与群里命令改的是同一份配置。";

    var isDyn = (state.kind === "dynamic");
    byId("commentWatchSettings").hidden = !isDyn;
    var thWatch = byId("subThWatch");
    if (thWatch) thWatch.hidden = !isDyn;  // 盯置顶评论列只在「动态订阅」矩阵出现
    var cols = isDyn ? 5 : 4;

    if (!currentRows().length) {
      tb.innerHTML = '<tr><td colspan="' + cols + '" class="empty">还没有订阅，点右上角「新增 UP 主」</td></tr>';
      return;
    }
    tb.innerHTML = currentRows().map(function (row, idx) {
      var tags = (row.targets || []).map(function (t) {
        var plat = t.platform || String(t.umo).split(":")[0];
        var bad = t.reachable === false;
        return '<span class="tag' + (t.at_all ? " at" : "") + (bad ? " bad" : "") +
          '" title="' + esc(t.umo) + (bad ? "（该平台前缀不在已加载实例里，这条发不出去）" : "") + '">' +
          esc(t.label || t.umo) + (t.at_all ? " @全体" : "") +
          ' <span class="plat">' + esc(plat) + "</span>" + (bad ? " ⚠" : "") + "</span>";
      }).join("") || '<span class="hint">没有目标群（不会推送）</span>';
      var watchTd = isDyn
        ? '<td style="text-align:center"><input type="checkbox" data-act="watch-comment" data-idx="' +
            idx + '" title="发视频后盯它评论区，等 UP 自己的置顶评论出现再补推一条"' +
            (row.watch_comment ? " checked" : "") + "></td>"
        : "";
      return "<tr>" +
        '<td>' + (row.uname ? '<b>' + esc(row.uname) + '</b><br><span class="hint">' + esc(row.uid) + "</span>" : '<b>' + esc(row.uid) + "</b>") + "</td>" +
        '<td><div class="tag-list">' + tags + "</div></td>" +
        '<td>' + (row.group_count || 0) + "</td>" +
        watchTd +
        '<td class="acts">' +
          '<button class="link" data-act="edit-sub" data-idx="' + idx + '">编辑</button>' +
          '<button class="link danger" data-act="del-sub" data-idx="' + idx + '">删除</button>' +
        "</td></tr>";
    }).join("");
  }

  // ---------- 渲染：通知开关 ----------

  function notifyCell(row, kind, label) {
    return "<td><label>" + esc(label) + " " +
      '<input type="checkbox" data-act="notify" data-umo="' + esc(row.umo) +
      '" data-kind="' + kind + '"' + (row[kind] ? " checked" : "") + "></label></td>";
  }

  function renderNotify() {
    var tb = byId("notifyBody");
    if (!state.notify.length) {
      tb.innerHTML = '<tr><td colspan="4" class="empty">还没有被订阅引用到的群</td></tr>';
      return;
    }
    tb.innerHTML = state.notify.map(function (row) {
      var notes = "";
      if (row.variant_count > 1) {
        notes += ' <span class="tag warn" title="' + esc((row.umos || []).join("\n")) +
          '">' + row.variant_count + " 种写法</span>";
      }
      if (row.mixed) {
        notes += ' <span class="tag warn">各写法设置不一致</span>';
      }
      return "<tr><td>" + esc(row.label) + notes + "</td>" +
        notifyCell(row, "notify", "开播") +
        notifyCell(row, "notify_end", "关播") +
        notifyCell(row, "notify_dyn", "动态") +
        "</tr>";
    }).join("");
  }

  // ---------- 渲染：诊断 ----------

  function renderDiag() {
    var st = state.status || {};
    var items = [];
    var invalid = st.invalid_lines || {};
    ["live", "dynamic"].forEach(function (kind) {
      (invalid[kind] || []).forEach(function (line) {
        items.push('<div class="list-item"><span class="nm">' + esc(kindLabel(kind)) +
          "：配置行无法解析 → " + esc(line) + "</span></div>");
      });
    });
    var quiet = st.quiet || {};
    if (quiet.raw) {
      items.push('<div class="list-item"><span class="nm">静音时段：' + esc(quiet.raw) +
        (quiet.valid ? (quiet.active ? "（💤 正在静音）" : "（当前非静音时段）")
                     : "（⚠️ 格式无效，应为 HH:MM-HH:MM）") + "</span></div>");
    }
    var sub = state.meta || {};  // loadSubscriptions 存进来的整包（含 platforms / duplicates）
    var platforms = sub.platforms || [];
    var dupCount = sub.duplicate_groups || 0;
    if (dupCount) {
      var detail = (sub.duplicates || []).map(function (d) {
        return esc(kindLabel(d.kind)) + " 群 " + esc(d.group_id) + "：" +
          esc((d.umos || []).join(" / "));
      }).join("<br>");
      items.push('<div class="list-item"><span class="nm">' +
        '<span class="tag warn">重复群 ' + dupCount + " 组</span> " +
        "同一个群号被写成了多种平台写法（右边只有一个能真正发出去）：<br>" + detail +
        "</span></div>");
    }
    if (sub.default_platform && platforms.length && platforms.indexOf(sub.default_platform) < 0) {
      items.push('<div class="list-item"><span class="nm">⚠️ 配置里的「默认平台」是 ' +
        esc(sub.default_platform) + "，但它不是已加载的平台实例（应是 " +
        esc(platforms.join(" / ")) + "）。裸群号会被补成这个前缀 → 发不出去；" +
        "建议在插件配置页清空「默认平台」让它自动探测。</span></div>");
    }
    items.push('<div class="list-item"><span class="nm">默认平台：' +
      esc(st.default_platform || "—") +
      (st.platforms && st.platforms.length
        ? "（已加载：" + esc(st.platforms.join(", ")) + "）"
        : "（还没探测到平台）") + "</span></div>");
    items.push('<div class="list-item"><span class="nm">全局通知：开播 ' +
      (st.global_notify && st.global_notify.live ? "✅" : "❌") + " / 关播 " +
      (st.global_notify && st.global_notify.end ? "✅" : "❌") +
      "（全局开关在插件配置页改）</span></div>");
    items.push('<div class="list-item"><span class="nm">群通知开关记录：' +
      (st.group_settings_count || 0) + " 个群 ｜ 数据目录：" +
      esc(st.data_dir || "—") + "</span></div>");
    byId("diag").innerHTML = items.join("");
  }

  // ---------- 数据加载 ----------

  function loadStatus() {
    return apiGet("page/status").then(function (d) {
      state.status = d || {};
      renderStatus();
      renderDiag();
    });
  }

  function loadSubscriptions() {
    return apiGet("page/subscriptions").then(function (d) {
      state.subs.live = d.live || { rows: [] };
      state.subs.dynamic = d.dynamic || { rows: [] };
      state.groups = d.groups || [];
      state.meta = d;
      renderSubs();
      renderDiag();
      var dup = d.duplicate_groups || 0;
      var dead = 0;
      ["live", "dynamic"].forEach(function (k) {
        (((d[k] || {}).rows) || []).forEach(function (row) {
          (row.targets || []).forEach(function (t) { if (t.reachable === false) dead++; });
        });
      });
      byId("diagHint").textContent = [
        dup ? "重复群 " + dup + " 组" : "",
        dead ? "发不出去的写法 " + dead + " 条" : ""
      ].filter(Boolean).join(" ｜ ");
      byId("btnDedupe").disabled = !dup && !dead;
    });
  }

  function loadNotify() {
    return apiGet("page/notify").then(function (d) {
      state.notify = d.rows || [];
      renderNotify();
    });
  }

  function loadCommentWatchConfig() {
    return apiGet("page/config").then(function (d) {
      byId("commentWatchHours").value = d.dyn_comment_watch_hours == null ? 2 : d.dyn_comment_watch_hours;
      byId("commentWatchHours").disabled = false;
      byId("btnSaveCommentWatch").disabled = false;
    });
  }

  function saveCommentWatchConfig() {
    var input = byId("commentWatchHours");
    var hours = Number(input.value);
    if (!input.value.trim() || !Number.isInteger(hours) || hours < 1 || hours > 72) {
      toast("请输入 1–72 小时的整数", true);
      return;
    }
    var btn = byId("btnSaveCommentWatch");
    btn.disabled = true;
    input.disabled = true;
    apiPost("page/config/save", { dyn_comment_watch_hours: hours }).then(function (d) {
      input.value = d.dyn_comment_watch_hours;
      toast("盯梢时长已保存，下轮检查生效");
    }).catch(fail).finally(function () { btn.disabled = false; input.disabled = false; });
  }

  function loadAll() {
    return Promise.all([loadStatus(), loadSubscriptions(), loadNotify(), loadCommentWatchConfig()]);
  }

  // ---------- 订阅编辑 ----------

  function resolveLabel(umo) {
    for (var i = 0; i < state.groups.length; i++) {
      if (state.groups[i].value === umo) return state.groups[i].label;
    }
    return umo;
  }

  function openSubModal(idx) {
    var rows = currentRows();
    if (idx == null) {
      state.editing = { uid: "", targets: {}, idx: null };
      byId("subTitle").textContent = "新增 UP 主（" + kindLabel(state.kind) + "）";
    } else {
      var row = rows[idx];
      if (!row) return;
      var targets = {};
      (row.targets || []).forEach(function (t) { targets[t.umo] = !!t.at_all; });
      state.editing = { uid: row.uid, targets: targets, idx: idx };
      byId("subTitle").textContent =
        "编辑 UP 主 " + (row.uname ? row.uname + "（" + row.uid + "）" : row.uid) + "（" + kindLabel(state.kind) + "）";
    }
    byId("subUid").value = state.editing.uid;
    byId("subSearch").value = "";
    byId("subErr").textContent = "";
    byId("subMask").hidden = false;
    renderSubPicker();
    byId("subUid").focus();
  }

  function subCandidateGroups() {
    // 候选群 = 后端给的全部候选 + 正在编辑的那几个群（后端还没见过的也留着）
    var rows = {};
    state.groups.forEach(function (g) { rows[g.value] = g; });
    var targets = (state.editing && state.editing.targets) || {};
    Object.keys(targets).forEach(function (umo) {
      if (!rows[umo]) {
        rows[umo] = { value: umo, label: resolveLabel(umo), group_name: "", group_id: "" };
      }
    });
    var list = Object.keys(rows).map(function (k) { return rows[k]; });
    list.sort(function (a, b) {
      var an = a.group_name || "";
      var bn = b.group_name || "";
      if (!an && bn) return 1;
      if (an && !bn) return -1;
      return String(a.group_id || a.label).localeCompare(String(b.group_id || b.label));
    });
    return list;
  }

  function renderSubPicker() {
    var box = byId("subGroups");
    if (!state.editing) return;
    var kw = (byId("subSearch").value || "").trim().toLowerCase();
    var rows = subCandidateGroups().filter(function (g) {
      if (!kw) return true;
      return (String(g.label) + " " + (g.group_name || "") + " " + (g.group_id || ""))
        .toLowerCase().indexOf(kw) >= 0;
    });
    if (!rows.length) {
      box.innerHTML = '<div class="empty">没有匹配的群：点「刷新群列表」，或先在群里说句话</div>';
      return;
    }
    box.innerHTML = rows.map(function (g) {
      var picked = Object.prototype.hasOwnProperty.call(state.editing.targets, g.value);
      var at = picked && state.editing.targets[g.value];
      var sub = [];
      if (g.group_id) sub.push("群号 " + g.group_id);
      if (g.variant_count > 1) sub.push(g.variant_count + " 种写法");
      sub.push(g.source === "referenced" ? "已被订阅" : "机器人所见");
      return '<div class="pick-item">' +
        '<input type="checkbox" data-sub-pick="' + esc(g.value) + '"' +
          (picked ? " checked" : "") + ">" +
        '<span class="nm">' + esc(g.group_name || g.label) + "</span>" +
        '<span class="sub">' + esc(sub.join(" · ")) + "</span>" +
        '<span class="at-wrap"><input type="checkbox" data-sub-at="' + esc(g.value) + '"' +
          (at ? " checked" : "") + (picked ? "" : " disabled") +
          "><label>@全体</label></span>" +
        "</div>";
    }).join("");
  }

  function toggleSubPick(umo, checked) {
    if (!state.editing) return;
    if (checked) {
      if (!Object.prototype.hasOwnProperty.call(state.editing.targets, umo)) {
        state.editing.targets[umo] = false;
      }
    } else {
      delete state.editing.targets[umo];
    }
    renderSubPicker();
  }

  function applySubModal() {
    if (!state.editing) return;
    var uid = (byId("subUid").value || "").trim();
    if (!/^\d+$/.test(uid)) {
      byId("subErr").textContent = "UID 必须是纯数字";
      return;
    }
    var targets = Object.keys(state.editing.targets).map(function (umo) {
      return { umo: umo, at_all: !!state.editing.targets[umo], label: resolveLabel(umo) };
    });
    var rows = currentRows().slice();
    var dup = -1;
    for (var i = 0; i < rows.length; i++) {
      if (String(rows[i].uid) === uid) { dup = i; }
    }
    if (dup >= 0 && dup !== state.editing.idx) {
      byId("subErr").textContent = "这个 UID 已经在列表里了";
      return;
    }
    var prev = (state.editing.idx != null) ? rows[state.editing.idx] : null;
    var row = { uid: uid, uname: (prev && prev.uname) || "",
                watch_comment: !!(prev && prev.watch_comment),
                targets: targets, group_count: targets.length };
    if (state.editing.idx == null) rows.push(row);
    else rows[state.editing.idx] = row;
    rows.sort(function (a, b) { return String(a.uid).localeCompare(String(b.uid)); });
    state.subs[state.kind] = Object.assign({}, state.subs[state.kind], {
      rows: rows,
      group_links: rows.reduce(function (n, r) { return n + (r.targets || []).length; }, 0)
    });
    byId("subMask").hidden = true;
    renderSubs();
    toast("已修改，记得点「保存订阅」");
  }

  // ---------- 保存与开关 ----------

  function saveSubs() {
    var btn = byId("btnSaveSubs");
    btn.disabled = true;
    var rows = currentRows().map(function (row) {
      return {
        uid: row.uid,
        watch_comment: !!row.watch_comment,
        targets: (row.targets || []).map(function (t) {
          return { umo: t.umo, at_all: !!t.at_all };
        })
      };
    });
    apiPost("page/subscriptions/save", { kind: state.kind, rows: rows }).then(function (d) {
      toast(d && d.lines ? "已保存：" + (d.uids || 0) + " 个 UP 主 → " + state.kind : "已保存");
      return loadAll();
    }).catch(fail).then(function () { btn.disabled = false; });
  }

  function saveNotify(umo, kind, value) {
    apiPost("page/notify/save", { umo: umo, kind: kind, value: value }).then(function () {
      for (var i = 0; i < state.notify.length; i++) {
        if (state.notify[i].umo === umo) state.notify[i][kind] = value;
      }
      toast("已保存");
    }).catch(fail);
  }

  function dedupeSubs() {
    confirmModal("整理订阅里的重复与失效写法？会把前缀发不出去的写法改写到同类型实例上，" +
      "并合并同一个群的多种写法，立刻写回配置。", "整理写法").then(function (ok) {
      if (!ok) return;
      var btn = byId("btnDedupe");
      btn.disabled = true;
      apiPost("page/subscriptions/dedupe", {}).then(function (d) {
        toast(d.merged || d.rewritten ? d.message || "已整理" : "没有需要整理的写法");
        return loadAll();
      }).catch(fail).then(function () { btn.disabled = false; });
    });
  }

  function deleteRow(idx) {
    var row = currentRows()[idx];
    if (!row) return;
    confirmModal("从" + kindLabel(state.kind) + "里删掉 UP 主 " + row.uid + "？", "删除订阅").then(function (ok) {
      if (!ok) return;
      var rows = currentRows().slice();
      rows.splice(idx, 1);
      state.subs[state.kind] = Object.assign({}, state.subs[state.kind], {
        rows: rows,
        group_links: rows.reduce(function (n, r) { return n + (r.targets || []).length; }, 0)
      });
      renderSubs();
      toast("已修改，记得点「保存订阅」");
    });
  }

  // ---------- 绑定 & 启动 ----------

  function bind() {
    byId("btnReload").addEventListener("click", function () {
      loadAll().then(function () { toast("已刷新"); }).catch(fail);
    });
    byId("tabLive").addEventListener("click", function () {
      state.kind = "live";
      renderSubs();
    });
    byId("tabDynamic").addEventListener("click", function () {
      state.kind = "dynamic";
      renderSubs();
    });
    byId("btnAddSub").addEventListener("click", function () { openSubModal(null); });
    byId("btnSaveCommentWatch").addEventListener("click", saveCommentWatchConfig);
    byId("btnSaveSubs").addEventListener("click", saveSubs);
    byId("btnDedupe").addEventListener("click", dedupeSubs);

    byId("subBody").addEventListener("click", function (e) {
      var el = e.target;
      if (!el || el.tagName !== "BUTTON") return;
      var act = el.getAttribute("data-act");
      var idx = parseInt(el.getAttribute("data-idx"), 10);
      if (act === "edit-sub") openSubModal(idx);
      else if (act === "del-sub") deleteRow(idx);
    });

    byId("subBody").addEventListener("change", function (e) {
      var box = e.target;
      if (!box || box.type !== "checkbox") return;
      if (box.getAttribute("data-act") !== "watch-comment") return;
      var idx = parseInt(box.getAttribute("data-idx"), 10);
      var rows = currentRows();
      if (rows[idx]) {
        rows[idx].watch_comment = box.checked;
        toast("已" + (box.checked ? "开启" : "关闭") + "盯置顶评论，记得点「保存订阅」");
      }
    });

    byId("subGroups").addEventListener("change", function (e) {
      var box = e.target;
      if (!box || box.type !== "checkbox") return;
      var umo = box.getAttribute("data-sub-pick");
      if (umo) { toggleSubPick(umo, box.checked); return; }
      var at = box.getAttribute("data-sub-at");
      if (at) { toggleSubAt(at, box.checked); }
    });
    byId("subSearch").addEventListener("input", renderSubPicker);
    byId("btnSubRefresh").addEventListener("click", function () {
      apiGet("page/groups", { refresh: "1" }).then(function (d) {
        state.groups = d.groups || [];
        renderSubPicker();
        toast("群列表已刷新（" + (d.refreshed || 0) + " 个群有更新）");
      }).catch(fail);
    });
    byId("btnSubOk").addEventListener("click", applySubModal);
    byId("subCancel").addEventListener("click", function () { byId("subMask").hidden = true; });

    byId("notifyBody").addEventListener("change", function (e) {
      var box = e.target;
      if (!box || box.type !== "checkbox") return;
      saveNotify(box.getAttribute("data-umo"), box.getAttribute("data-kind"), box.checked);
    });

    byId("subMask").addEventListener("click", function (e) {
      if (e.target === this) this.hidden = true;
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") byId("subMask").hidden = true;
    });
  }

  initTheme();
  bind();
  byId("subBody").innerHTML = '<tr><td colspan="4" class="empty">正在连接面板…</td></tr>';
  waitForBridge()
    .then(loadAll)
    .catch(function (err) {
      byId("subBody").innerHTML = '<tr><td colspan="4" class="empty">' +
        esc((err && err.message) || "加载失败") + "</td></tr>";
      fail(err);
    });
})();
