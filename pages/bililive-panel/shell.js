/* Standalone panel shell. Local resources only; no framework or external assets. */
(function () {
  "use strict";
  var c = {"title": "B站开播监测", "brand": "BILIBILI / LIVE DESK", "mark": "B", "hero": "不错过开播，\n也不错过每一次更新。", "sub": "连接你关注的 UP 主与群聊，让直播与动态消息及时抵达。", "color": "pink", "version": "2.4.1", "old": "2.4.0", "views": [["subscriptions", "订阅管理", "SUBSCRIPTIONS", [0]], ["notifications", "群通知", "NOTIFICATIONS", [1]], ["diagnostics", "订阅诊断", "DIAGNOSTICS", [2]]]};
  var root = document.documentElement;
  var wrap = document.querySelector('.wrap');
  var bar = wrap.querySelector('.topbar');
  var stats = document.getElementById('stats');
  var cards = Array.from(wrap.querySelectorAll(':scope > .card, :scope > .grid2 > .card'));
  var prefs = { theme: 'follow', compact: false };
  var storageKey = 'panelAppearance:' + c.color;
  var follow = 'light';
  var themeObserver;
  var pending = 0;
  var failed = false;
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }
  function button(text, fn, cls) {
    var node = el('button', cls, text); node.type = 'button';
    node.addEventListener('click', fn); return node;
  }
  root.dataset.palette = c.color;
  var layout = el('div', 'workspace');
  var rail = el('aside', 'rail');
  var brand = el('a', 'brand'); brand.href = '#overview';
  brand.append(el('span', 'brand-symbol', c.mark));
  var brandText = el('span'); brandText.append(el('strong', '', c.title), el('small', '', c.brand)); brand.append(brandText);
  rail.append(brand, el('div', 'nav-label', '工作空间 / WORKSPACE'));
  var nav = el('nav'); nav.setAttribute('aria-label','页面导航'); rail.append(nav);
  var railFooter = el('div', 'rail-footer');
  var connection = el('span','connection','等待连接');
  railFooter.append(connection, el('small','',c.version + ' · ASTRBOT PLUGIN')); rail.append(railFooter);
  var stage = el('main','stage'); stage.id='main-content'; stage.tabIndex=-1;
  var header = el('header','workspace-header');
  var crumb=el('div','breadcrumb', c.brand);
  var tools=el('div','header-tools');
  var pill=bar.querySelector('.pill'); if(pill) tools.append(pill);
  var themeButton=button('☾ 深色',function(){prefs.theme=root.dataset.theme==='dark'?'light':'dark'; savePrefs();});
  themeButton.id='panelTheme'; tools.append(themeButton);
  // Preserve the original business action nodes and all their event handlers.
  var actionBar=el('div','workspace-actions');
  Array.from(bar.querySelectorAll('button')).forEach(function(node){actionBar.append(node);});
  header.append(crumb,tools); stage.append(header);
  var content=el('div','workspace-content');
  var heading=el('div','page-heading'); var headingText=el('div');
  var eyebrow=el('span','eyebrow'); var title=el('h1');
  headingText.append(eyebrow,title); heading.append(headingText,actionBar); content.append(heading);
  var feedback=el('div','request-feedback'); feedback.hidden=true; feedback.setAttribute('role','alert'); content.append(feedback);
  var sync=el('div','sync-line','数据尚未同步'); sync.setAttribute('role','status'); content.append(sync);
  var views={};
  function addView(key,label,english,node) {
    node.classList.add('panel-view'); node.id='panel-'+key; node.hidden=true;
    var labelId='panel-heading-'+key; node.setAttribute('aria-label',label);
    content.append(node);
    var item=button('',function(){location.hash=key;},'nav-item');
    var index=Object.keys(views).length+1;
    item.append(el('span','nav-number',String(index).padStart(2,'0')),el('span','nav-text',label),el('span','nav-arrow','↗'));
    item.setAttribute('aria-label',label); nav.append(item);
    views[key]={label:label,english:english,node:node,button:item};
  }
  var overview=el('section');
  var hero=el('div','hero'); var copy=el('div','hero-copy');
  copy.append(el('span','eyebrow',c.brand),el('h2','',c.hero),el('p','',c.sub));
  copy.append(button('进入'+c.views[0][1]+'  ↗',function(){location.hash=c.views[0][0];},'primary'));
  var art=el('div','hero-art'); art.setAttribute('aria-hidden','true');
  art.append(el('span','art-orbit'),el('span','art-symbol',c.mark),el('span','art-caption', c.color==='pink'?'ON AIR / STAY CONNECTED':c.color==='jade'?'山河如故 · 江湖常新':'IF MESSAGE → REPLY'));
  hero.append(copy,art); overview.append(hero);
  var statsTitle=el('div','section-heading'); statsTitle.append(el('h2','','运行速览'),el('span','hint','数据以最近一次读取为准')); overview.append(statsTitle,stats);
  var shortcuts=el('div','shortcuts');
  c.views.forEach(function(v){var b=button('',function(){location.hash=v[0];},'shortcut'); b.append(el('span','eyebrow',v[2]),el('strong','',v[1]+'  →'));shortcuts.append(b);});
  overview.append(shortcuts); addView('overview','总览','OVERVIEW',overview);
  c.views.forEach(function(v){var section=el('section');v[3].forEach(function(i){if(cards[i])section.append(cards[i]);});addView(v[0],v[1],v[2],section);});
  var appearance=el('section');
  var appearanceCard=el('div','card appearance-card');
  appearanceCard.innerHTML='<span class="eyebrow">PERSONALIZE YOUR WORKSPACE</span><h2>选择舒适的工作方式</h2><p class="hint">仅影响本浏览器中的面板，不会改变插件配置、消息卡片或其他管理员的界面。</p><div class="field"><label for="panelThemeSelect">界面主题</label><select id="panelThemeSelect"><option value="follow">跟随 AstrBot</option><option value="light">浅色 · 日间</option><option value="dark">深色 · 夜间</option></select></div><label class="compact-row"><span>紧凑布局<small>减小卡片和表格间距，显示更多内容</small></span><input id="panelCompact" type="checkbox"></label><p class="hint" id="panelPreferenceHint" role="status"></p>';
  appearance.append(appearanceCard);addView('appearance','界面外观','APPEARANCE',appearance);
  content.append(el('footer','workspace-footer', c.brand+'  /  为每一次连接而设计'));
  stage.append(content);layout.append(rail,stage);
  wrap.replaceWith(layout);
  var skip=el('a','skip-link','跳到主要内容');skip.href='#main-content';document.body.prepend(skip);
  function showView() {
    var key=location.hash.slice(1);if(!Object.prototype.hasOwnProperty.call(views,key)) key='overview';
    Object.keys(views).forEach(function(k){var v=views[k];v.node.hidden=k!==key;v.button.classList.toggle('active',k===key);if(k===key)v.button.setAttribute('aria-current','page');else v.button.removeAttribute('aria-current');});
    title.textContent=views[key].label;eyebrow.textContent=views[key].english;
  }
  window.addEventListener('hashchange',showView);showView();
  var themeSelect=document.getElementById('panelThemeSelect');var compact=document.getElementById('panelCompact');
  function applyPrefs(){
    if(themeObserver)themeObserver.disconnect();
    root.dataset.theme=prefs.theme==='follow'?follow:prefs.theme;root.dataset.compact=String(prefs.compact);
    themeSelect.value=prefs.theme;compact.checked=prefs.compact;
    themeButton.textContent=root.dataset.theme==='dark'?'☀ 浅色':'☾ 深色';themeButton.setAttribute('aria-label',root.dataset.theme==='dark'?'切换为浅色主题':'切换为深色主题');
    if(themeObserver)themeObserver.observe(root,{attributes:true,attributeFilter:['data-theme']});
  }
  function savePrefs(){applyPrefs();var hint=document.getElementById('panelPreferenceHint');try{localStorage.setItem(storageKey,JSON.stringify(prefs));hint.textContent='偏好已保存到当前浏览器。';}catch(e){hint.textContent='浏览器限制存储，外观仅在本次打开时生效。';}}
  themeSelect.addEventListener('change',function(){prefs.theme=this.value;savePrefs();});compact.addEventListener('change',function(){prefs.compact=this.checked;savePrefs();});
  var params=new URLSearchParams(location.search);var host=params.get('theme')||root.dataset.theme;
  if(host!=='dark'&&host!=='light'){var d=params.get('isDark');try{if(d===null)d=localStorage.getItem('astrbot_plugin_theme_is_dark');}catch(e){}host=d==='true'?'dark':'light';}follow=host;
  try{var saved=JSON.parse(localStorage.getItem(storageKey)||'{}');if(['light','dark','follow'].includes(saved.theme))prefs.theme=saved.theme;if(typeof saved.compact==='boolean')prefs.compact=saved.compact;}catch(e){}
  themeObserver=new MutationObserver(function(){var theme=root.dataset.theme;if(theme==='dark'||theme==='light')follow=theme;applyPrefs();});applyPrefs();
  window.addEventListener('storage',function(e){if(e.key==='astrbot_plugin_theme_is_dark'){follow=e.newValue==='true'?'dark':'light';applyPrefs();}});
  // Make every existing table scroll inside its card instead of widening the page.
  document.querySelectorAll('.card > table').forEach(function(table){var scroll=el('div','table-wrap');table.replaceWith(scroll);scroll.append(table);});
  document.querySelectorAll('.table-wrap').forEach(function(node){node.tabIndex=0;node.setAttribute('aria-label','数据表格，可横向滚动');});
  var toast=document.getElementById('toast');toast.setAttribute('role','status');toast.setAttribute('aria-live','polite');
  document.querySelectorAll('.modal-mask').forEach(function(mask,index){
    var dialog=mask.querySelector('.modal');dialog.setAttribute('role','dialog');dialog.setAttribute('aria-modal','true');
    var h=dialog.querySelector('h2');if(h){if(!h.id)h.id='dialog-heading-'+index;dialog.setAttribute('aria-labelledby',h.id);}
    var previous=null;var wasHidden=mask.hidden;
    new MutationObserver(function(){if(mask.hidden===wasHidden)return;wasHidden=mask.hidden;
      if(!mask.hidden){previous=document.activeElement;if(!mask.contains(previous)){var focus=mask.querySelector('input:not([hidden]),select,textarea,button');if(focus)focus.focus();}}
      else if(previous&&!mask.contains(previous)&&previous.isConnected)previous.focus();
    }).observe(mask,{attributes:true,attributeFilter:['hidden']});
    mask.addEventListener('keydown',function(e){
      if(e.key!=='Tab')return;
      var items=Array.from(mask.querySelectorAll('button,input,textarea,select,[tabindex="0"]')).filter(function(n){return !n.disabled&&n.getClientRects().length;});
      if(!items.length)return;var first=items[0],last=items[items.length-1];
      if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}
    });
  });
  window.PanelUI={
    request:function(promise){
      if(!pending){failed=false;feedback.hidden=true;}pending++;connection.textContent='正在同步';
      return promise.then(function(data){sync.textContent='最近读取 · '+new Date().toLocaleTimeString('zh-CN',{hour12:false});return data;},function(error){failed=true;feedback.hidden=false;feedback.textContent='操作未完成：'+(error.message||'请求失败')+'。请重试原操作，或刷新页面重新连接。';throw error;}).finally(function(){pending--;if(!pending){connection.textContent=failed?'连接或操作异常':'面板已连接';connection.classList.toggle('error',failed);}});
    }
  };
})();
