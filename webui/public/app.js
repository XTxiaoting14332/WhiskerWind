/**
 * qqcache WebUI - Single Page Application
 * Vercel Console Design System
 */

'use strict';

// ----------------------------------------------------------------------------
// Utilities & Selectors
// ----------------------------------------------------------------------------
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const TOKEN_KEY = 'qqcache_webui_token';
const THEME_KEY = 'qqcache_webui_theme';

// 构建版本：必须与 index.html 的 <meta name="ww-build"> 一致。
// 浏览器缓存可能混装新旧 index.html/app.js（旧 navTo 会导致导航失灵），
// 握手不匹配时强制重载一次自愈；sessionStorage 标记防止重载死循环。
const WW_BUILD = 'ww16';

function enforceBuildVersion() {
  try {
    const meta = document.querySelector('meta[name="ww-build"]');
    if (meta && meta.content === WW_BUILD) return true;
    const key = 'ww_reload_' + WW_BUILD;
    if (!sessionStorage.getItem(key)) {
      sessionStorage.setItem(key, '1');
      location.reload();
      return false;
    }
    console.warn('[ww] build mismatch persisted after reload, continuing best-effort');
  } catch {}
  return true;
}

function getToken() {
  return localStorage.getItem(TOKEN_KEY) || '';
}

function escapeHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatTime(ts) {
  if (!ts) return '';
  const d = new Date(ts > 1e11 ? ts : ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function formatDate(ts) {
  if (!ts) return '';
  const d = new Date(ts > 1e11 ? ts : ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function showToast(msg, duration = 2800) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove('show'), duration);
}

// ----------------------------------------------------------------------------
// API Client with Auth & 401 Interception
// ----------------------------------------------------------------------------
async function api(path, options = {}) {
  const token = localStorage.getItem(TOKEN_KEY);
  const headers = {
    Accept: 'application/json',
    ...(options.headers || {}),
  };

  // fetch 对 string body 默认 text/plain——express.json() 只认
  // application/json，不显式补头的话 JSON POST 在后端全是空 body。
  // FormData/Blob 不能动，浏览器要自己生成 boundary。
  if (typeof options.body === 'string' && !Object.keys(headers).some(
    (h) => h.toLowerCase() === 'content-type',
  )) {
    headers['Content-Type'] = 'application/json';
  }

  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  try {
    const resp = await fetch(path, { ...options, headers });

    if (resp.status === 401) {
      localStorage.removeItem(TOKEN_KEY);
      showLoginModal('会话已失效，请重新输入密码');
      throw new Error('Unauthorized');
    }

    if (!resp.ok) {
      let errMsg = `HTTP ${resp.status}`;
      try {
        const errJson = await resp.json();
        errMsg = errJson.error || errJson.message || errMsg;
      } catch {}
      throw new Error(errMsg);
    }

    const ct = resp.headers.get('content-type') || '';
    if (ct.includes('application/json')) {
      return await resp.json();
    }
    return resp;
  } catch (err) {
    if (err.message !== 'Unauthorized') {
      console.warn(`[API] Error on ${path}:`, err.message);
    }
    throw err;
  }
}

// ----------------------------------------------------------------------------
// Theme Manager (Vercel 3-State: System / Light / Dark)
// ----------------------------------------------------------------------------
function initTheme() {
  const saved = localStorage.getItem(THEME_KEY) || 'dark';
  applyTheme(saved);

  $$('.theme-opt').forEach((btn) => {
    btn.onclick = () => {
      const val = btn.getAttribute('data-theme-val');
      applyTheme(val);
    };
  });
}

function applyTheme(theme) {
  localStorage.setItem(THEME_KEY, theme);

  $$('.theme-opt').forEach((btn) => {
    btn.classList.toggle('active', btn.getAttribute('data-theme-val') === theme);
  });

  let effective = theme;
  if (theme === 'auto') {
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    effective = prefersDark ? 'dark' : 'light';
  }

  document.documentElement.setAttribute('data-theme', effective);
  document.documentElement.style.colorScheme = effective;
}

// ----------------------------------------------------------------------------
// Navigation & Views
// ----------------------------------------------------------------------------
const VIEW_TITLES = {
  overview: '运行概览',
  events: '事件日志',
  transports: '传输管理',
  logs: '服务日志',
  settings: '状态与设置',
};

let currentView = 'overview';

function navTo(viewName) {
  if (!VIEW_TITLES[viewName]) return;
  currentView = viewName;

  $$('.nav-item').forEach((item) => {
    item.classList.toggle('active', item.getAttribute('data-v') === viewName);
  });

  $$('.view').forEach((v) => {
    v.classList.remove('active');
  });

  const targetView = $(`#v-${viewName}`);
  if (targetView) {
    targetView.classList.add('active');
  }

  $('#pageTitle').textContent = VIEW_TITLES[viewName];

  // Close mobile drawer if open
  $('#navDrawer').classList.remove('open');
  $('#drawerScrim').classList.remove('show');

  // Trigger view data refresh
  if (viewName === 'overview') loadOverview();
  else if (viewName === 'events') loadEvents();
  else if (viewName === 'transports') { loadTransports(); loadProtocols(); }
  else if (viewName === 'logs') loadLogs();
  else if (viewName === 'settings') loadSettings();
}

function initNavigation() {
  $$('.nav-item').forEach((item) => {
    item.onclick = () => {
      const v = item.getAttribute('data-v');
      navTo(v);
    };
  });

  $('#btnMenuToggle').onclick = () => {
    $('#navDrawer').classList.toggle('open');
    $('#drawerScrim').classList.toggle('show');
  };

  $('#drawerScrim').onclick = () => {
    $('#navDrawer').classList.remove('open');
    $('#drawerScrim').classList.remove('show');
  };
}

// ----------------------------------------------------------------------------
// Login & Auth Flow
// ----------------------------------------------------------------------------
let qqLoginPollTimer = null;

function showLoginModal(msg = '') {
  stopQQLoginPolling();
  $('#appShell').style.display = 'none';
  $('#qqLoginModal').style.display = 'none';
  const modal = $('#loginModal');
  const errBox = $('#loginError');
  modal.style.display = 'flex';
  if (msg) {
    errBox.textContent = msg;
    errBox.style.display = 'block';
  } else {
    errBox.style.display = 'none';
  }
  $('#loginPassword').value = '';
  $('#loginPassword').focus();
}

function hideLoginModal() {
  $('#loginModal').style.display = 'none';
}

let userDismissedQQLogin = false;

function showQQLoginModal(statusData = null) {
  stopQQLoginPolling();
  userDismissedQQLogin = false;
  try { sessionStorage.removeItem('qq_login_dismissed'); } catch {}
  // Keep appShell visible underneath the modal backdrop
  $('#appShell').style.display = 'flex';
  $('#loginModal').style.display = 'none';
  const qrModal = $('#qqLoginModal');
  if (qrModal) {
    qrModal.style.display = 'flex';
  }

  reloadStandaloneQr();
  startQQLoginPolling();
}

function hideQQLoginModal() {
  userDismissedQQLogin = true;
  try { sessionStorage.setItem('qq_login_dismissed', 'true'); } catch {}
  stopQQLoginPolling();
  const qrModal = $('#qqLoginModal');
  if (qrModal) {
    qrModal.style.display = 'none';
  }
  $('#appShell').style.display = 'flex';
}

// 下线通知：同一条（按 time 识别）只弹一次，点过「知道了」就不再弹
const KICK_TITLES = ['下线通知', '登录失败', '登录失效'];
function showKickNotice(notice) {
  if (!notice || !notice.title) return;
  const key = String(notice.time || notice.text || '');
  try { if (localStorage.getItem('kick_notice_ack') === key) return; } catch {}
  $('#kickNoticeTitle').textContent = `QQ ${notice.title}`;
  $('#kickNoticeText').textContent = notice.text || '';
  $('#kickNoticeTime').textContent = notice.time
    ? new Date(notice.time * 1000).toLocaleString() : '';
  const modal = $('#kickNoticeModal');
  modal.dataset.key = key;
  modal.classList.add('show');
}
function ackKickNotice() {
  const modal = $('#kickNoticeModal');
  try { localStorage.setItem('kick_notice_ack', modal.dataset.key || ''); } catch {}
  modal.classList.remove('show');
}
document.addEventListener('DOMContentLoaded', () => {
  $('#btnKickNoticeOk')?.addEventListener('click', ackKickNotice);
  $('#btnKickNoticeLogin')?.addEventListener('click', () => {
    ackKickNotice();
    showQQLoginModal();
  });
});

function startQQLoginPolling() {
  stopQQLoginPolling();
  qqLoginPollTimer = setInterval(async () => {
    try {
      const data = await api('/api/status');
      const state = data.login_state?.state;
      showKickNotice(data.login_state?.notice);

      if (state === 'logged_in') {
        stopQQLoginPolling();
        $('#standaloneScanStatus').textContent = '扫码登录成功！';
        showToast(`QQ 账号 (${data.login_state?.self_id || ''}) 登录成功！`);
        setTimeout(() => {
          hideQQLoginModal();
          loadOverview();
        }, 600);
        return;
      }

      if (data.login_state?.qr_expired) {
        $('#standaloneQrOverlay').style.display = 'flex';
      } else {
        $('#standaloneQrOverlay').style.display = 'none';
      }
    } catch {}
  }, 2500);
}

function stopQQLoginPolling() {
  if (qqLoginPollTimer) {
    clearInterval(qqLoginPollTimer);
    qqLoginPollTimer = null;
  }
}

let currentStandaloneQrBlobUrl = null;

async function reloadStandaloneQr() {
  const placeholder = $('#standaloneQrPlaceholder');
  const img = $('#standaloneQrImg');
  const overlay = $('#standaloneQrOverlay');

  if (overlay) overlay.style.display = 'none';
  if (placeholder) {
    placeholder.style.display = 'flex';
    placeholder.innerHTML = `
      <span class="spinner" style="margin-bottom:10px"></span>
      <span style="font-size:12.5px;color:var(--muted)">正在获取二维码…</span>
    `;
  }
  if (img) img.style.display = 'none';

  const token = getToken();
  if (!token) {
    showLoginModal();
    return;
  }

  try {
    const res = await fetch(`/api/qr?t=${Date.now()}&token=${encodeURIComponent(token)}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) {
      if (res.status === 401) {
        showLoginModal();
        return;
      }
      const errText = await res.text().catch(() => '');
      throw new Error(errText || `获取二维码失败 (HTTP ${res.status})`);
    }

    const blob = await res.blob();
    if (!blob || blob.size === 0) {
      throw new Error('获取到的二维码数据为空');
    }

    if (currentStandaloneQrBlobUrl) {
      URL.revokeObjectURL(currentStandaloneQrBlobUrl);
    }
    currentStandaloneQrBlobUrl = URL.createObjectURL(blob);

    if (img) {
      img.onload = () => {
        if (placeholder) placeholder.style.display = 'none';
        img.style.display = 'block';
      };
      img.onerror = () => {
        if (placeholder) {
          placeholder.style.display = 'flex';
          placeholder.innerHTML = `
            <div style="color:var(--accent-red);font-size:12.5px;margin-bottom:8px">二维码图片渲染失败</div>
            <button class="btn btn-secondary btn-sm" onclick="reloadStandaloneQr()">重试</button>
          `;
        }
        img.style.display = 'none';
      };
      img.src = currentStandaloneQrBlobUrl;
    }
  } catch (err) {
    if (placeholder) {
      placeholder.style.display = 'flex';
      placeholder.innerHTML = `
        <div style="color:var(--accent-red);font-size:12.5px;margin-bottom:8px">${escapeHtml(err.message || '二维码获取失败')}</div>
        <button class="btn btn-secondary btn-sm" onclick="reloadStandaloneQr()">点击重试</button>
      `;
    }
    if (img) img.style.display = 'none';
    showToast(err.message || '获取二维码失败', 'error');
  }
}

function initQQLoginModalControls() {
  $('#btnReloadStandaloneQr').onclick = reloadStandaloneQr;
  $('#btnRefreshStandaloneQr').onclick = reloadStandaloneQr;

  const closeBtn = $('#btnCloseQQLoginModal');
  if (closeBtn) closeBtn.onclick = hideQQLoginModal;

  const qrModal = $('#qqLoginModal');
  if (qrModal) {
    qrModal.onclick = (e) => {
      if (e.target === qrModal) {
        hideQQLoginModal();
      }
    };
  }

  $('#btnViewContainerScreen').onclick = async () => {
    try {
      showToast('正在获取容器屏幕…');
      const token = getToken();
      const res = await fetch(`/api/screen?t=${Date.now()}&token=${encodeURIComponent(token)}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error('截屏获取失败');
      const blob = await res.blob();
      openLightbox(URL.createObjectURL(blob));
    } catch (err) {
      showToast(err.message, 'error');
    }
  };

  $('#btnSkipToConsole').onclick = () => {
    hideQQLoginModal();
  };

  $('#btnLogoutFromQr').onclick = async () => {
    try {
      await api('/api/logout', { method: 'POST' });
    } catch {}
    localStorage.removeItem(TOKEN_KEY);
    if (wsClient) {
      wsClient.close();
    }
    showLoginModal();
  };

  $('#btnTopBarQQLogin').onclick = () => {
    showQQLoginModal();
  };
}

async function checkQQLoginAndEnter(forceConsole = false) {
  initWebSocket();
  $('#appShell').style.display = 'flex';
  navTo('overview');

  if (forceConsole) {
    hideQQLoginModal();
    return;
  }

  try {
    const statusData = await api('/api/status');
    const isUpstreamOnline = statusData.upstream_online;
    const isQQLoggedIn = statusData.login_state?.state === 'logged_in';
    showKickNotice(statusData.login_state?.notice);

    const dismissedInSession = sessionStorage.getItem('qq_login_dismissed') === 'true';
    // If upstream is online and QQ is not logged in, overlay the QR popup on top of the main console
    if (isUpstreamOnline && !isQQLoggedIn && !userDismissedQQLogin && !dismissedInSession) {
      showQQLoginModal(statusData);
    } else {
      hideQQLoginModal();
    }
  } catch {
    hideQQLoginModal();
  }
}

function initAuth() {
  initQQLoginModalControls();

  const form = $('#loginForm');
  form.onsubmit = async (e) => {
    e.preventDefault();
    const btn = $('#loginBtn');
    const errBox = $('#loginError');
    const pwd = $('#loginPassword').value.trim();

    btn.disabled = true;
    btn.textContent = '登录中…';
    errBox.style.display = 'none';

    try {
      const resp = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: pwd }),
      });

      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.error || '登录失败');
      }

      localStorage.setItem(TOKEN_KEY, data.token);
      hideLoginModal();
      showToast('登录成功');

      // Check QQ login state: if not logged in, directly show standalone QQ login
      await checkQQLoginAndEnter();
    } catch (err) {
      errBox.textContent = err.message;
      errBox.style.display = 'block';
    } finally {
      btn.disabled = false;
      btn.textContent = '登 录';
    }
  };

  $('#btnLogout').onclick = async () => {
    try {
      await api('/api/logout', { method: 'POST' });
    } catch {}
    localStorage.removeItem(TOKEN_KEY);
    if (wsClient) {
      wsClient.close();
    }
    showLoginModal();
  };
}

// ----------------------------------------------------------------------------
// WebSocket Realtime Manager
// ----------------------------------------------------------------------------
let wsClient = null;
let wsReconnectTimer = null;

function initWebSocket() {
  const token = localStorage.getItem(TOKEN_KEY);
  if (!token) return;

  if (wsClient) {
    try { wsClient.close(); } catch {}
  }

  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${proto}//${window.location.host}/ws?token=${encodeURIComponent(token)}`;

  wsClient = new WebSocket(url);

  wsClient.onopen = () => {
    updateUpstreamPill(true);
  };

  wsClient.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      handleWsMessage(msg);
    } catch (e) {
      console.warn('[WS] Parse error:', e);
    }
  };

  wsClient.onclose = () => {
    wsClient = null;
    updateUpstreamPill(false);
    // Reconnect after 3s if still logged in
    clearTimeout(wsReconnectTimer);
    if (localStorage.getItem(TOKEN_KEY)) {
      wsReconnectTimer = setTimeout(initWebSocket, 3000);
    }
  };

  wsClient.onerror = () => {
    if (wsClient) {
      try { wsClient.close(); } catch {}
    }
  };
}

function updateUpstreamPill(online) {
  const dot = $('#upstreamDot');
  const txt = $('#upstreamTxt');
  if (online) {
    dot.className = 'pulse-dot pulse';
    txt.textContent = '上游在线';
  } else {
    dot.className = 'pulse-dot offline';
    txt.textContent = '容器离线';
  }
}

function handleWsMessage(msg) {
  if (!msg || !msg.type) return;

  if (msg.type === 'status') {
    updateUpstreamPill(msg.data?.online);
  } else if (msg.type === 'event') {
    handleIncomingEvent(msg.data);
  } else if (msg.type === 'log') {
    handleIncomingLog(msg.data);
  }
}

// ----------------------------------------------------------------------------
// CQ Code Parser & Renderer
// ----------------------------------------------------------------------------
function renderCQMessage(rawCQ) {
  if (!rawCQ) return '';
  const text = String(rawCQ);

  // Match [CQ:type,key=val,...]
  const cqRegex = /\[CQ:([a-zA-Z0-9_-]+)((?:,[a-zA-Z0-9_-]+=[^,\]]*)*)\]/g;
  let lastIdx = 0;
  let html = '';
  let match;

  while ((match = cqRegex.exec(text)) !== null) {
    // Append preceding raw text (escaped)
    if (match.index > lastIdx) {
      html += escapeHtml(text.substring(lastIdx, match.index)).replace(/\n/g, '<br>');
    }
    lastIdx = cqRegex.lastIndex;

    const type = match[1];
    const rawParams = match[2];
    const params = {};
    if (rawParams) {
      for (const pair of rawParams.split(',')) {
        if (!pair) continue;
        const eq = pair.indexOf('=');
        if (eq !== -1) {
          const k = pair.substring(0, eq);
          const v = pair.substring(eq + 1);
          params[k] = v;
        }
      }
    }

    if (type === 'image') {
      const fileOrUrl = params.url || params.file || '';
      const token = getToken();
      const src = fileOrUrl.startsWith('data:') || fileOrUrl.startsWith('http')
        ? fileOrUrl
        : `/api/media?path=${encodeURIComponent(fileOrUrl)}&token=${encodeURIComponent(token)}`;
      html += `<img class="msg-img" src="${src}" alt="图片" onclick="openLightbox('${src}')" onerror="this.onerror=null;this.alt='[图片已失效]'">`;
    } else if (type === 'at') {
      const qq = params.qq || '';
      if (qq === 'all') {
        html += `<span class="msg-at">@全体成员</span>`;
      } else {
        html += `<span class="msg-at">@${escapeHtml(qq)}</span>`;
      }
    } else if (type === 'reply') {
      const id = params.id || '';
      html += `<div class="msg-quote">引用消息 #${escapeHtml(id)}</div>`;
    } else if (type === 'face') {
      html += `[表情]`;
    } else if (type === 'record') {
      html += `[语音]`;
    } else if (type === 'video') {
      html += `[视频]`;
    } else {
      html += `[${escapeHtml(type)}]`;
    }
  }

  // Remainder text
  if (lastIdx < text.length) {
    html += escapeHtml(text.substring(lastIdx)).replace(/\n/g, '<br>');
  }

  return html;
}

// Lightbox
function openLightbox(src) {
  const lb = $('#imageLightbox');
  const img = $('#lightboxImg');
  img.src = src;
  lb.classList.add('show');
}

$('#imageLightbox').onclick = () => {
  $('#imageLightbox').classList.remove('show');
};

// ----------------------------------------------------------------------------
// 1. Overview Page Logic (NapCat-style Dashboard)
// ----------------------------------------------------------------------------
// 最近一次看到的 QQ 登录态：页面加载时由 overview/settings 的数据初始化，
// 之后由心跳/login_state 事件推进——只有真正变化才刷新视图、弹提示
let lastQQLoginState = null;

function onQQLoginState(state) {
  if (!state || state === lastQQLoginState) return;
  const prev = lastQQLoginState;
  lastQQLoginState = state;
  if (prev === null) return; // 首次观测：当前视图刚用新数据渲染过
  if (state === 'logged_in') {
    hideQQLoginModal();
    showToast('QQ 账号登录成功！');
  }
  if (currentView === 'overview') loadOverview();
  else if (currentView === 'settings') loadSettings();
}

async function loadOverview() {
  try {
    const data = await api('/api/status');
    const ls = data.login_state || {};
    if (ls.state) lastQQLoginState = ls.state;
    showKickNotice(ls.notice);
    const sys = data.system_info || {};
    const system = sys.system || {};
    const qqProc = sys.qq_proc || {};
    const versions = sys.versions || {};
    const stats = sys.stats || {};
    const lsState = ls.state;
    const isLoggedIn = lsState === 'logged_in';
    const isLoginRequired = lsState === 'login_required' || lsState === 'qr_expired';
    const isUpstreamOnline = !!data.upstream_online;

    // 1. Account Avatar & Dot
    const avatarEl = $('#ovAccountAvatar');
    const dotEl = $('#ovAccountStatusDot');
    const selfId = data.self_id || ls.self_id || 0;
    const selfName = data.self_name || ls.self_name || '';

    if (avatarEl) {
      avatarEl.onerror = () => {
        avatarEl.onerror = null;
        avatarEl.src = '/favicon.svg';
      };
      avatarEl.src = data.avatar_url || (selfId ? `https://q1.qlogo.cn/g?b=qq&nk=${selfId}&s=640` : '/favicon.svg');
      // 未登录时弱化头像——这些资料来自本机缓存文件，不代表在线，别让卡片看着像已登录
      avatarEl.style.filter = isLoggedIn ? '' : 'grayscale(75%)';
      avatarEl.style.opacity = isLoggedIn ? '' : '0.6';
    }

    if (dotEl) {
      if (!isUpstreamOnline) {
        dotEl.className = 'account-status-dot offline';
        dotEl.title = '容器离线';
      } else if (isLoggedIn) {
        dotEl.className = 'account-status-dot online';
        dotEl.title = '在线';
      } else if (isLoginRequired) {
        dotEl.className = 'account-status-dot amber';
        dotEl.title = '待扫码登录';
      } else {
        dotEl.className = 'account-status-dot offline';
        dotEl.title = '未在线';
      }
    }

    // Account Name & Status Badge
    const nameEl = $('#ovAccountName');
    const badgeEl = $('#ovAccountStatusBadge');
    if (nameEl) {
      if (!isUpstreamOnline) {
        nameEl.textContent = '容器离线';
      } else if (isLoggedIn) {
        nameEl.textContent = selfName || (selfId ? `QQ: ${selfId}` : 'QQ 机器人');
      } else if (isLoginRequired) {
        nameEl.textContent = selfName || 'QQ 账号';
      } else {
        nameEl.textContent = '未登录账号';
      }
    }

    if (badgeEl) {
      if (!isUpstreamOnline) {
        badgeEl.className = 'badge badge-red';
        badgeEl.textContent = '容器离线';
      } else if (isLoggedIn) {
        badgeEl.className = 'badge badge-green';
        badgeEl.textContent = '已登录在线';
      } else if (isLoginRequired) {
        badgeEl.className = 'badge badge-amber';
        badgeEl.textContent = '待扫码登录';
      } else {
        badgeEl.className = 'badge badge-gray';
        badgeEl.textContent = '未登录';
      }
    }

    // Account Sub Row: QQ Tag, Versions
    const uinTag = $('#ovAccountUinTag');
    if (uinTag) {
      uinTag.textContent = selfId
        ? (isLoggedIn ? `QQ: ${selfId}` : `上次登录 · QQ: ${selfId}`)
        : 'QQ: 未绑定';
    }

    const oneBotVer = $('#ovOneBotVer');
    if (oneBotVer) {
      oneBotVer.textContent = `OneBot ${versions.onebot || 'v11'}`;
    }

    const coreVer = $('#ovCoreVer');
    if (coreVer) {
      coreVer.textContent = `v${versions.qqcache || '0.1.0'}`;
    }

    const qqVer = $('#ovQQVer');
    if (qqVer) {
      qqVer.textContent = versions.qq ? `v${versions.qq}` : '—';
    }

    // Quick Login button & Topbar Login button
    const quickLoginBtn = $('#btnOvQuickLogin');
    const topBarLoginBtn = $('#btnTopBarQQLogin');
    if (isLoginRequired && isUpstreamOnline) {
      if (quickLoginBtn) quickLoginBtn.style.display = 'inline-flex';
      if (topBarLoginBtn) topBarLoginBtn.style.display = 'inline-flex';
    } else {
      if (quickLoginBtn) quickLoginBtn.style.display = 'none';
      if (topBarLoginBtn) topBarLoginBtn.style.display = 'none';
    }

    // Account Stats Strip
    const statIn = $('#ovStatIn');
    const statOut = $('#ovStatOut');
    const statConvs = $('#ovStatConvs');
    const statSeen = $('#ovStatSeen');
    if (statIn) statIn.textContent = data.event_stats?.message || 0;
    if (statOut) statOut.textContent = data.event_stats?.message_sent || 0;
    if (statConvs) statConvs.textContent = stats.conversations || 0;
    if (statSeen) statSeen.textContent = stats.seen_messages || data.event_stats?.total || 0;

    // 2. CPU (处理器)
    const cpuPct = typeof system.cpu_percent === 'number' ? system.cpu_percent : 0;
    const cpuBadge = $('#ovCpuPercentBadge');
    const cpuVal = $('#ovCpuPercent');
    const cpuModel = $('#ovCpuModel');
    const cpuCores = $('#ovCpuCores');
    const cpuGaugeRing = $('#ovCpuGaugeRing');

    const cpuFormatted = `${cpuPct.toFixed(1)}%`;
    if (cpuBadge) cpuBadge.textContent = cpuFormatted;
    if (cpuVal) cpuVal.textContent = cpuFormatted;
    if (cpuModel) cpuModel.textContent = system.cpu_model || '—';
    if (cpuCores) cpuCores.textContent = `${system.cpu_cores || 1} 核心 / ${system.platform || 'Linux'}`;

    if (cpuGaugeRing) {
      const circ = 251.2;
      const offset = circ - (circ * Math.min(100, Math.max(0, cpuPct)) / 100);
      cpuGaugeRing.style.strokeDashoffset = offset;
      cpuGaugeRing.style.stroke = cpuPct > 80 ? 'var(--accent-red)' : (cpuPct > 50 ? 'var(--accent-amber)' : 'var(--accent-blue)');
    }

    // 3. 系统占用 (Memory & Disk)
    const mem = system.mem || {};
    const memPct = typeof mem.percent === 'number' ? mem.percent : 0;
    const memUsedGb = ((mem.used || 0) / (1024 ** 3)).toFixed(1);
    const memTotalGb = ((mem.total || 0) / (1024 ** 3)).toFixed(1);

    const memBadge = $('#ovMemPercentBadge');
    const memVal = $('#ovMemPercent');
    const memText = $('#ovMemText');
    const memGaugeRing = $('#ovMemGaugeRing');

    if (memBadge) memBadge.textContent = `${memPct}%`;
    if (memVal) memVal.textContent = `${memPct}%`;
    if (memText) memText.textContent = `${memUsedGb} / ${memTotalGb} GB (${memPct}%)`;

    if (memGaugeRing) {
      const circ = 251.2;
      const offset = circ - (circ * Math.min(100, Math.max(0, memPct)) / 100);
      memGaugeRing.style.strokeDashoffset = offset;
      memGaugeRing.style.stroke = memPct > 85 ? 'var(--accent-red)' : (memPct > 65 ? 'var(--accent-amber)' : 'var(--accent-green)');
    }

    const disk = system.disk || {};
    const diskPct = typeof disk.percent === 'number' ? disk.percent : 0;
    const diskUsedGb = ((disk.used || 0) / (1024 ** 3)).toFixed(1);
    const diskTotalGb = ((disk.total || 0) / (1024 ** 3)).toFixed(1);

    const diskText = $('#ovDiskText');
    if (diskText) diskText.textContent = `${diskUsedGb} / ${diskTotalGb} GB (${diskPct}%)`;

    // 4. QQ 占用 (QQ Process Metrics)
    const qqBadge = $('#ovQqProcBadge');
    const qqRss = $('#ovQqRss');
    const qqProcTree = $('#ovQqProcTree');

    const rssBytes = qqProc.rss_bytes || 0;
    const rssMb = (rssBytes / (1024 * 1024)).toFixed(1);

    if (qqBadge) {
      if (!isUpstreamOnline) {
        qqBadge.className = 'badge badge-red';
        qqBadge.textContent = '容器离线';
      } else if (qqProc.process_count > 0) {
        if (isLoggedIn) {
          qqBadge.className = 'badge badge-green';
          qqBadge.textContent = '运行中';
        } else {
          qqBadge.className = 'badge badge-amber';
          qqBadge.textContent = '待登录';
        }
      } else {
        qqBadge.className = 'badge badge-gray';
        qqBadge.textContent = '未启动';
      }
    }
    if (qqRss) qqRss.textContent = `${rssMb} MB`;
    if (qqProcTree) qqProcTree.textContent = `${qqProc.process_count || 0} 个子进程 (PID: ${qqProc.pid || 1})`;

    // 5. 系统概览 (System Overview)
    const sysBadge = $('#ovSystemStatusBadge');
    const sysPlatform = $('#ovSysPlatform');
    const sysUptime = $('#ovSysUptime');

    if (sysBadge) {
      if (!isUpstreamOnline) {
        sysBadge.className = 'badge badge-red';
        sysBadge.textContent = '容器离线';
      } else if (isLoggedIn) {
        sysBadge.className = 'badge badge-green';
        sysBadge.textContent = '运行正常';
      } else {
        sysBadge.className = 'badge badge-amber';
        sysBadge.textContent = '待登录授权';
      }
    }
    if (sysPlatform) sysPlatform.textContent = `${system.platform || 'Linux'} (${system.cpu_cores || 1}核)`;

    const u = system.uptime_s || data.uptime_s || 0;
    const d = Math.floor(u / 86400);
    const h = Math.floor((u % 86400) / 3600);
    const m = Math.floor((u % 3600) / 60);
    const s = u % 60;
    const uptimeStr = d > 0 ? `${d}d ${h}h ${m}m` : (h > 0 ? `${h}h ${m}m ${s}s` : `${m}m ${s}s`);
    if (sysUptime) sysUptime.textContent = uptimeStr;

    // 6. 通道传输概况 (Transports Summary Grid)
    const transportsList = data.transports || [];
    renderOverviewTransports(data.transport_summary, transportsList);
  } catch (err) {
    console.error('[Overview] Failed to load:', err);
  }
}

function renderOverviewTransports(ts = {}, transportsList = []) {
  const container = $('#overviewTransports');
  if (!container) return;

  const httpAddress = ts.http || '0.0.0.0:5700';
  const wsClientsCount = ts.ws_clients || 0;
  const postUrls = ts.post_urls || [];
  const reverseWs = ts.reverse_ws || [];

  container.innerHTML = `
    <div class="transports-overview-grid">
      <div class="transport-summary-card">
        <div class="ts-card-header">
          <span class="badge badge-blue">HTTP API</span>
          <span class="badge badge-green">监听中</span>
        </div>
        <div class="ts-card-val" style="font-family:var(--font-mono)">${escapeHtml(httpAddress)}</div>
        <div class="ts-card-desc">OneBot v11 HTTP 接口与正向 WS 端口</div>
      </div>

      <div class="transport-summary-card">
        <div class="ts-card-header">
          <span class="badge badge-blue">正向 WS</span>
          <span class="badge ${wsClientsCount > 0 ? 'badge-green' : 'badge-gray'}">${wsClientsCount} 个客户端</span>
        </div>
        <div class="ts-card-val" style="font-family:var(--font-mono)">ws://${escapeHtml(httpAddress)}/</div>
        <div class="ts-card-desc">BFF 与下游框架正向 WebSocket 连接</div>
      </div>

      <div class="transport-summary-card">
        <div class="ts-card-header">
          <span class="badge badge-amber">反向 WS</span>
          <span class="badge ${reverseWs.length > 0 ? 'badge-blue' : 'badge-gray'}">${reverseWs.length} 个通道</span>
        </div>
        <div class="ts-card-val">${reverseWs.length ? reverseWs.map(w => `<div style="font-size:12.5px;line-height:1.4;font-family:var(--font-mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${escapeHtml(w.url)}"><span class="badge ${w.status === 'connected' ? 'badge-green' : 'badge-amber'}" style="margin-right:4px">${w.status}</span>${escapeHtml(w.url)}</div>`).join('') : '<span style="color:var(--muted);font-size:12.5px">暂无配置</span>'}</div>
        <div class="ts-card-desc">主动连接外部 NoneBot / Koishi 等框架</div>
      </div>

      <div class="transport-summary-card">
        <div class="ts-card-header">
          <span class="badge badge-green">POST 上报</span>
          <span class="badge ${postUrls.length > 0 ? 'badge-green' : 'badge-gray'}">${postUrls.length} 个目标</span>
        </div>
        <div class="ts-card-val">${postUrls.length ? postUrls.map(p => `<div style="font-size:12.5px;line-height:1.4;font-family:var(--font-mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${escapeHtml(p.url)}"><span class="badge badge-green" style="margin-right:4px">${p.status || 'active'}</span>${escapeHtml(p.url)}</div>`).join('') : '<span style="color:var(--muted);font-size:12.5px">暂无配置</span>'}</div>
        <div class="ts-card-desc">OneBot v11 HTTP 事件上报通道</div>
      </div>
    </div>
  `;
}

// ----------------------------------------------------------------------------
// Realtime Events Feed (事件日志)
// ----------------------------------------------------------------------------
let eventPauseScroll = false;
let eventsCache = [];

function formatEventSummary(event) {
  if (event.raw_message) return event.raw_message;
  if (event.message) {
    if (typeof event.message === 'string') return event.message;
    if (Array.isArray(event.message)) {
      return event.message.map((m) => m.data?.text || `[${m.type}]`).join('');
    }
    return JSON.stringify(event.message);
  }
  if (event.meta_event_type) {
    let s = event.meta_event_type;
    if (event.sub_type) s += ` (${event.sub_type})`;
    if (event.status?.login_state) s += ` · 状态: ${event.status.login_state}`;
    else if (event.state) s += ` · 状态: ${event.state}`;
    return s;
  }
  if (event.notice_type) return `通知: ${event.notice_type}${event.sub_type ? ' (' + event.sub_type + ')' : ''}`;
  if (event.request_type) return `请求: ${event.request_type}`;
  return JSON.stringify(event);
}

function renderSingleEventCardHtml(event, idx) {
  const timeStr = formatTime(event.time || event._received_at || Date.now());
  let typeBadge = '';
  if (event.post_type === 'message') {
    typeBadge = '<span class="badge badge-blue">message</span>';
  } else if (event.post_type === 'message_sent') {
    typeBadge = '<span class="badge badge-green">message_sent</span>';
  } else if (event.post_type === 'meta_event') {
    typeBadge = '<span class="badge badge-amber">meta_event</span>';
  } else if (event.post_type === 'notice') {
    typeBadge = '<span class="badge badge-blue">notice</span>';
  } else if (event.post_type === 'request') {
    typeBadge = '<span class="badge badge-red">request</span>';
  } else {
    typeBadge = `<span class="badge badge-gray">${escapeHtml(event.post_type || 'event')}</span>`;
  }

  const summary = formatEventSummary(event);

  return `
    <div class="event-card" id="evCard-${idx}">
      <div class="event-header" onclick="this.parentElement.classList.toggle('open')">
        <div class="event-header-left">
          <span class="event-time">${timeStr}</span>
          <span class="event-badge">${typeBadge}</span>
          <span class="event-summary" title="${escapeHtml(summary)}">${escapeHtml(summary)}</span>
        </div>
        <div class="event-header-right">
          <span>详情</span>
          <svg viewBox="0 0 24 24" style="width:12px;height:12px;fill:currentColor"><path d="M7 10l5 5 5-5z"/></svg>
        </div>
      </div>
      <div class="event-body"><pre style="margin:0"><code>${escapeHtml(JSON.stringify(event, null, 2))}</code></pre></div>
    </div>
  `;
}

async function loadEvents() {
  const stream = $('#eventsStreamContainer');
  if (!stream) return;

  if (eventsCache.length > 0) {
    renderEventsStream();
    return;
  }

  stream.innerHTML = `<div style="color:var(--muted);text-align:center;padding:24px">正在获取历史事件…</div>`;

  try {
    const data = await api('/api/status');
    const recent = data.recent_events || [];
    // eventsCache 语义：旧→新，尾部追加；后端 recent_events 是 DESC 所以要反转
    eventsCache = recent.slice(0, 100).reverse();
    renderEventsStream();
  } catch (err) {
    stream.innerHTML = `<div style="color:var(--accent-red);text-align:center;padding:24px">加载事件失败: ${escapeHtml(err.message)}</div>`;
  }
}

function eventMatchesFilters(ev) {
  const filterType = ($('#eventFilterType')?.value || '').trim();
  const filterUin = ($('#eventFilterUin')?.value || '').trim();
  if (filterType && ev.post_type !== filterType) return false;
  if (filterUin) {
    const match = String(ev.user_id) === filterUin ||
      String(ev.group_id) === filterUin ||
      String(ev.self_id) === filterUin;
    if (!match) return false;
  }
  return true;
}

function renderEventsStream() {
  const stream = $('#eventsStreamContainer');
  if (!stream) return;

  const list = eventsCache.filter(eventMatchesFilters);

  if (list.length === 0) {
    stream.innerHTML = `<div class="events-empty" style="color:var(--muted);text-align:center;padding:30px">暂无匹配事件记录</div>`;
    return;
  }

  // 贴底才跟滚：用户向上翻阅时不拽下去；初次渲染 scrollHeight≈clientHeight 天然成立
  const atBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 40;
  stream.innerHTML = list.map((ev, idx) => renderSingleEventCardHtml(ev, idx)).join('');
  if (atBottom) stream.scrollTop = stream.scrollHeight;
}

function handleIncomingEvent(event) {
  // 1. Update stats on overview
  const inVal = $('#ovStatIn');
  const outVal = $('#ovStatOut');
  if (event.post_type === 'message' && inVal) {
    inVal.textContent = parseInt(inVal.textContent || '0', 10) + 1;
  } else if (event.post_type === 'message_sent' && outVal) {
    outVal.textContent = parseInt(outVal.textContent || '0', 10) + 1;
  }

  // 1b. 被踢下线：实时弹出 QQ 下线通知原文
  if (event.post_type === 'notice' && event.notice_type === 'qqcache_system_notification'
      && KICK_TITLES.includes(event.title)) {
    showKickNotice({ title: event.title, text: event.text, time: event.time });
  }

  // 2. Handle login_state change dynamically（心跳每 5s 一次，只在状态变化时动作）
  if (event.post_type === 'meta_event') {
    onQQLoginState(event.state || event.status?.login_state);
  }

  // 3. Add to event cache (旧→新，尾部追加，超长砍最旧)
  eventsCache.push(event);
  if (eventsCache.length > 200) {
    eventsCache.shift();
  }

  // 4. Append to Event Log tab if active and not paused —— 增量追加，
  //    整版 innerHTML 重画会把用户展开的详情卡片收回去
  if (currentView === 'events' && !eventPauseScroll) {
    const stream = $('#eventsStreamContainer');
    if (stream && eventMatchesFilters(event)) {
      stream.querySelector('.events-empty')?.remove();
      const atBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 40;
      stream.insertAdjacentHTML('beforeend', renderSingleEventCardHtml(event, eventsCache.length));
      // 与 eventsCache 的 shift 截断对齐，DOM 不无限长
      while (stream.children.length > 200) stream.firstElementChild.remove();
      if (atBottom) stream.scrollTop = stream.scrollHeight;
    }
  }
}

function initEventControls() {
  $('#btnClearEvents').onclick = () => {
    eventsCache = [];
    renderEventsStream();
  };

  $('#btnToggleEventPause').onclick = (e) => {
    eventPauseScroll = !eventPauseScroll;
    e.target.textContent = eventPauseScroll ? '继续滚动' : '暂停滚动';
    if (!eventPauseScroll && currentView === 'events') {
      renderEventsStream();
    }
  };

  const typeSel = $('#eventFilterType');
  if (typeSel) {
    typeSel.onchange = () => {
      renderEventsStream();
    };
  }

  const uinInput = $('#eventFilterUin');
  if (uinInput) {
    uinInput.oninput = () => {
      renderEventsStream();
    };
  }
}

// ----------------------------------------------------------------------------
// 4. Transports Management Logic
// ----------------------------------------------------------------------------
async function loadTransports() {
  const tbody = $('#transportsTableBody');
  tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--muted)">加载中…</td></tr>`;

  try {
    const list = await api('/api/transports');
    if (!list || list.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--muted)">无传输通道</td></tr>`;
      return;
    }

    tbody.innerHTML = list.map((t) => {
      const isStartup = t.source === 'startup';
      const typeBadge = `<span class="badge ${t.type === 'http' ? 'badge-blue' : (t.type === 'ws_reverse' ? 'badge-amber' : 'badge-green')}">${escapeHtml(t.type)}</span>`;
      const sourceBadge = `<span class="badge ${isStartup ? 'badge-gray' : 'badge-green'}">${escapeHtml(t.source || 'runtime')}</span>`;
      const statusBadge = `<span class="badge ${t.status === 'listening' || t.status === 'connected' || t.status === 'active' ? 'badge-green' : 'badge-amber'}">${escapeHtml(t.status || '')}</span>`;

      return `
        <tr>
          <td style="font-family:var(--font-mono);font-weight:600">${escapeHtml(t.id)}</td>
          <td>${typeBadge}</td>
          <td>${sourceBadge}</td>
          <td style="font-family:var(--font-mono);font-size:12px">${escapeHtml(t.address || t.url || t.http_url || '')}</td>
          <td>${statusBadge}</td>
          <td style="text-align:right">
            <button class="btn btn-secondary btn-sm"
              ${isStartup ? 'disabled title="启动参数配置，不可删除"' : `onclick="deleteTransport('${t.id}')"`}>
              删除
            </button>
          </td>
        </tr>
      `;
    }).join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--accent-red)">加载失败: ${escapeHtml(err.message)}</td></tr>`;
  }
}

async function deleteTransport(id) {
  if (!confirm(`确定要删除传输通道 ${id} 吗？`)) return;

  try {
    await api(`/api/transports/${id}`, { method: 'DELETE' });
    showToast(`传输通道 ${id} 已删除`);
    loadTransports();
  } catch (err) {
    showToast('删除失败: ' + err.message);
  }
}

function initTransportsControls() {
  $('#btnOpenAddTransportModal').onclick = () => {
    $('#modalAddTransport').classList.add('show');
  };

  $('#btnCloseAddTransportModal').onclick = () => {
    $('#modalAddTransport').classList.remove('show');
  };

  $('#btnCancelAddTransport').onclick = () => {
    $('#modalAddTransport').classList.remove('show');
  };

  $('#transTypeSelect').onchange = (e) => {
    const val = e.target.value;
    $('#fieldsHttp').style.display = val === 'http' ? 'block' : 'none';
    $('#fieldsWsReverse').style.display = val === 'ws_reverse' ? 'block' : 'none';
    $('#fieldsHttpPost').style.display = val === 'http_post' ? 'block' : 'none';
  };

  $('#formAddTransport').onsubmit = async (e) => {
    e.preventDefault();
    const type = $('#transTypeSelect').value;
    const payload = { type };

    if (type === 'http') {
      payload.host = $('#transHttpHost').value.trim() || '127.0.0.1';
      payload.port = parseInt($('#transHttpPort').value.trim(), 10);
    } else if (type === 'ws_reverse') {
      payload.url = $('#transWsUrl').value.trim();
      payload.token = $('#transWsToken').value.trim();
    } else if (type === 'http_post') {
      payload.url = $('#transPostUrl').value.trim();
    }

    try {
      await api('/api/transports', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      showToast('传输通道创建成功');
      $('#modalAddTransport').classList.remove('show');
      loadTransports();
    } catch (err) {
      showToast('创建失败: ' + err.message);
    }
  };
}

// ----------------------------------------------------------------------------
// 5. Service Logs Stream Logic
// ----------------------------------------------------------------------------
let logAutoScroll = true;
let logsCache = [];

async function loadLogs() {
  const box = $('#logStreamWindow');
  try {
    const data = await api('/api/logs?tail=200');
    logsCache = data.logs || [];
    renderLogs();
  } catch (err) {
    box.innerHTML = `<div style="color:var(--accent-red);padding:20px">获取日志失败: ${escapeHtml(err.message)}</div>`;
  }
}

function handleIncomingLog(logItem) {
  if (!logItem) return;
  logsCache.push(logItem);
  if (logsCache.length > 500) logsCache.shift();

  if (currentView === 'logs') {
    renderLogs();
  }
}

function renderLogs() {
  const box = $('#logStreamWindow');
  const levelFilter = $('#logFilterLevel').value;
  const searchQ = ($('#logSearchInput').value || '').trim().toLowerCase();

  let list = logsCache;

  if (levelFilter) {
    list = list.filter((l) => (l.level || '').toUpperCase() === levelFilter);
  }
  if (searchQ) {
    list = list.filter((l) =>
      (l.message || '').toLowerCase().includes(searchQ) ||
      (l.module || '').toLowerCase().includes(searchQ)
    );
  }

  if (list.length === 0) {
    box.innerHTML = `<div style="color:var(--muted);padding:20px;text-align:center">没有匹配的日志</div>`;
    return;
  }

  box.innerHTML = list.map((l) => {
    const time = formatTime(l.time);
    const lvl = (l.level || 'INFO').toUpperCase();
    return `
      <div class="log-entry level-${lvl}">
        <span class="log-time">${time}</span>
        <span class="log-level">${lvl}</span>
        <span class="log-module">${escapeHtml(l.module || '')}</span>
        <span class="log-msg">${escapeHtml(l.message || '')}</span>
      </div>
    `;
  }).join('');

  if (logAutoScroll) {
    box.scrollTop = box.scrollHeight;
  }
}

function initLogsControls() {
  $('#logFilterLevel').onchange = renderLogs;
  $('#logSearchInput').oninput = renderLogs;

  $('#btnToggleLogScroll').onclick = (e) => {
    logAutoScroll = !logAutoScroll;
    e.target.textContent = logAutoScroll ? '锁定滚动' : '自动滚动';
  };

  $('#btnLogScrollBottom').onclick = () => {
    const box = $('#logStreamWindow');
    box.scrollTop = box.scrollHeight;
  };

  $('#btnClearLogs').onclick = () => {
    logsCache = [];
    renderLogs();
  };
}

// ----------------------------------------------------------------------------
// 6. Settings, Login State & Screen Capture Logic
// ----------------------------------------------------------------------------
async function loadSettings() {
  const container = $('#settingsLoginStateContainer');
  container.innerHTML = `<div style="color:var(--muted)">加载登录状态中…</div>`;

  try {
    const statusData = await api('/api/status');

    // Update Env parameters table
    $('#envUpstream').textContent = statusData.upstream_url || '—';
    $('#envProtocol').textContent = statusData.version?.app_name ? `${statusData.version.app_name} ${statusData.version.app_version || ''} (${statusData.version.protocol_version || 'v11'})` : 'OneBot v11';
    $('#envSelfId').textContent = statusData.self_id || statusData.login_state?.self_id || '未配置';
    $('#envSelfName').textContent = statusData.self_name || statusData.login_state?.self_name || '—';

    // Render 4-State Login Card
    renderLoginStateCard(statusData);

    // Fetch and sync #ww trigger switch state
    try {
      const nwData = await api('/api/settings/nw_trigger');
      updateNwTriggerUI(nwData.enabled);
    } catch {}

    // 成员绑定缓存窗口
    try {
      const ttlData = await api('/api/settings/member_ttl');
      updateMemberTtlUI(ttlData.ttl);
    } catch {}

    // 上次自检报告还在就回显——刷新页面不丢诊断结果
    try {
      const st = await api('/api/settings/selftest');
      if (st && st.report) renderSelftest(st.report);
    } catch {}
  } catch (err) {
    container.innerHTML = `<div style="color:var(--accent-red)">获取状态失败: ${escapeHtml(err.message)}</div>`;
  }
}

// ----------------------------------------------------------------------------
// Satori / automatic 协议管理（启停、端口、令牌）
// ----------------------------------------------------------------------------
async function loadProtocols() {
  const box = $('#protocolsContainer');
  if (!box) return;
  try {
    const data = await api('/api/protocols');
    renderProtocols(data.protocols || []);
  } catch (err) {
    box.innerHTML = `<div style="color:var(--accent-red)">获取协议列表失败: ${escapeHtml(err.message)}</div>`;
  }
}

const PROTO_HINTS = {
  satori: (p) => `接入地址：<code>http://&lt;主机&gt;:${escapeHtml(String(p.port))}/v1</code>（事件 WebSocket <code>/v1/events</code>）。platform 供 Koishi 等框架识别平台，默认为 qq。`,
  automatic: (p) => `正向 WebSocket：<code>ws://&lt;主机&gt;:${escapeHtml(String(p.port))}/automatic/v1</code>；HTTP 调用 <code>POST /automatic/v1/&lt;action&gt;</code>。`,
};

function p_token_set(name, list) {
  return !!(list.find((x) => x.name === name) || {}).token_set;
}

// navigator.clipboard 只在安全上下文（https / localhost）可用；用局域网 IP
// 打开 WebUI 时退回 execCommand
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  const ok = document.execCommand('copy');
  ta.remove();
  if (!ok) throw new Error('浏览器不允许复制');
}

function renderProtocols(list) {
  const box = $('#protocolsContainer');
  // OneBot v11 的传输通道在同页上方的表格中管理，这里只列可启停的协议
  box.innerHTML = list.filter((p) => !p.fixed).map((p) => {
    const badge = p.running
      ? '<span class="badge badge-green">运行中</span>'
      : '<span class="badge badge-gray">未启用</span>';
    if (p.fixed) {
      return `
        <div class="proto-item">
          <div class="proto-head">
            <strong>${escapeHtml(p.title)}</strong>
            <span class="badge badge-green">常驻</span>
          </div>
          <div class="proto-note">${escapeHtml(p.note || '')}</div>
        </div>`;
    }
    const stats = p.running
      ? `<div class="proto-note">已连接客户端 ${p.clients ?? 0} · WebHook ${p.webhooks?.length ?? 0} 个 · 事件序号 ${p.sn ?? 0}</div>`
      : '';
    return `
      <div class="proto-item" data-proto="${escapeHtml(p.name)}">
        <div class="proto-head">
          <strong>${escapeHtml(p.title)}</strong>
          <div style="display:flex;align-items:center;gap:12px">
            ${badge}
            <label class="toggle-switch">
              <input type="checkbox" class="proto-enable" ${p.enabled ? 'checked' : ''}>
              <span class="slider"></span>
            </label>
          </div>
        </div>
        <div class="proto-grid">
          <label>监听地址<input class="input proto-host" value="${escapeHtml(p.host || '')}"></label>
          <label>端口<input class="input proto-port" type="number" min="1" max="65535" value="${escapeHtml(String(p.port ?? ''))}"></label>
          ${'platform' in p ? `<label>platform<input class="input proto-platform" value="${escapeHtml(p.platform || '')}"></label>` : '<span></span>'}
        </div>
        ${'reverse_urls' in p ? `<label class="proto-wide">反向 WebSocket
          <textarea class="input proto-reverse" rows="2" placeholder="每行一个地址，例如 ws://127.0.0.1:8090/automatic">${escapeHtml((p.reverse_urls || []).join('\n'))}</textarea></label>` : ''}
        <div class="proto-token">
          <span>令牌</span>
          <input class="input proto-token-input" type="password" autocomplete="new-password" spellcheck="false"
                 placeholder="${p.token_set ? '已设置（点「显示」查看）' : '未设置（不鉴权，仅限监听 127.0.0.1）'}">
          <button class="btn btn-secondary btn-sm proto-token-show" type="button">显示</button>
          <button class="btn btn-secondary btn-sm proto-token-copy" type="button">复制</button>
          <button class="btn btn-secondary btn-sm proto-token-regen" type="button">随机生成</button>
        </div>
        <div class="proto-note">${PROTO_HINTS[p.name] ? PROTO_HINTS[p.name](p) : ''}</div>
        ${stats}
        <div style="margin-top:10px"><button class="btn btn-primary btn-sm proto-save">保存配置</button></div>
      </div>`;
  }).join('');

  box.querySelectorAll('.proto-item[data-proto]').forEach((el) => {
    const name = el.dataset.proto;
    const save = async (changes, okMsg) => {
      try {
        const data = await api(`/api/protocols/${encodeURIComponent(name)}`, {
          method: 'POST',
          body: JSON.stringify(changes),
        });
        renderProtocols(data.protocols || []);
        showToast(okMsg);
      } catch (err) {
        showToast('操作失败: ' + err.message, 'error');
        loadProtocols();
      }
    };
    el.querySelector('.proto-enable').onchange = (e) => {
      save({ enabled: e.target.checked }, e.target.checked ? '已启用' : '已停用');
    };
    const tokenInput = el.querySelector('.proto-token-input');
    let tokenLoaded = !p_token_set(name, list);   // 没设过令牌就没什么可加载的
    let tokenDirty = false;
    tokenInput.oninput = () => { tokenDirty = true; tokenLoaded = true; };
    const loadToken = async () => {
      if (tokenLoaded) return tokenInput.value;
      const t = await api(`/api/protocols/${encodeURIComponent(name)}/token`);
      tokenInput.value = t.token || '';
      tokenLoaded = true;
      return tokenInput.value;
    };
    const showBtn = el.querySelector('.proto-token-show');
    showBtn.onclick = async () => {
      try {
        if (tokenInput.type === 'password') {
          await loadToken();
          tokenInput.type = 'text';
          showBtn.textContent = '隐藏';
        } else {
          tokenInput.type = 'password';
          showBtn.textContent = '显示';
        }
      } catch (err) {
        showToast('获取令牌失败: ' + err.message, 'error');
      }
    };
    el.querySelector('.proto-token-copy').onclick = async () => {
      try {
        const t = await loadToken();
        if (!t) {
          showToast('令牌为空，没有可复制的内容', 'error');
          return;
        }
        await copyText(t);
        showToast('令牌已复制');
      } catch (err) {
        showToast('复制失败: ' + err.message, 'error');
      }
    };

    el.querySelector('.proto-save').onclick = () => {
      const changes = {
        host: el.querySelector('.proto-host').value.trim(),
        port: Number(el.querySelector('.proto-port').value),
      };
      const plat = el.querySelector('.proto-platform');
      if (plat) changes.platform = plat.value.trim();
      const rev = el.querySelector('.proto-reverse');
      if (rev) changes.reverse_urls = rev.value.split('\n').map((u) => u.trim()).filter(Boolean);
      if (tokenDirty) {
        const t = tokenInput.value.trim();
        if (!t && !confirm('令牌为空将关闭鉴权，仅在监听 127.0.0.1 时允许。确定继续吗？')) return;
        changes.token = t;
      }
      save(changes, '配置已保存');
    };
    el.querySelector('.proto-token-regen').onclick = () => {
      if (!confirm('生成新令牌后，使用旧令牌的客户端需要更新配置。确定继续吗？')) return;
      save({ regenerate_token: true }, '已生成新令牌，点「显示」或「复制」获取');
    };
  });
}

function renderLoginStateCard(statusData) {
  const container = $('#settingsLoginStateContainer');
  const isUpstreamOnline = statusData.upstream_online;
  const ls = statusData.login_state || {};
  const state = isUpstreamOnline ? ls.state : 'offline';
  if (isUpstreamOnline && ls.state) lastQQLoginState = ls.state;

  if (state === 'offline') {
    container.innerHTML = `
      <div style="display:flex;align-items:center;gap:12px;padding:12px;background:rgba(238,0,0,0.08);border:1px solid rgba(238,0,0,0.2);border-radius:var(--r-m)">
        <span class="pulse-dot offline" style="width:12px;height:12px"></span>
        <div>
          <strong style="color:var(--accent-red);font-size:14px">容器离线 (Offline)</strong>
          <p style="color:var(--muted);font-size:12.5px;margin-top:2px">BFF 无法连通 Docker 容器中的 WhiskerWind 服务，请确认容器及 5700 端口已正常启动。</p>
        </div>
      </div>
    `;
    return;
  }

  if (state === 'logged_in') {
    container.innerHTML = `
      <div style="display:flex;align-items:center;gap:14px;padding:16px;background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.2);border-radius:var(--r-m)">
        <span class="pulse-dot" style="width:14px;height:14px"></span>
        <div style="flex:1">
          <div style="display:flex;align-items:center;gap:8px">
            <strong style="font-size:15px">${escapeHtml(ls.self_name || 'Bot')}</strong>
            <span class="badge badge-green">已正常登录</span>
          </div>
          <p style="color:var(--muted);font-size:12.5px;margin-top:4px">
            当前绑定 QQ: <span style="font-family:var(--font-mono);font-weight:600">${ls.self_id}</span>
          </p>
        </div>
      </div>
    `;
    return;
  }

  if (state === 'login_required') {
    const expired = !!ls.qr_expired;
    const token = getToken();
    const qrUrl = `/api/qr?t=${Date.now()}&token=${encodeURIComponent(token)}`;
    container.innerHTML = `
      <div style="text-align:center;padding:14px">
        <span class="badge badge-amber" style="font-size:13px;padding:4px 10px;margin-bottom:12px">等待扫码登录</span>
        <p style="color:var(--muted);font-size:12.5px;margin-bottom:16px">请使用手机 QQ 扫描下方二维码进行登录：</p>
        <div style="position:relative;display:inline-block;border:1px solid var(--border);border-radius:var(--r-m);padding:10px;background:#fff">
          <img id="loginQrImg" src="${qrUrl}" alt="登录二维码" style="width:200px;height:200px;display:block" onerror="this.onerror=null;this.alt='[二维码加载失败]'">
          ${expired ? `
            <div id="qrExpiredOverlay" style="position:absolute;inset:0;background:rgba(0,0,0,0.7);display:flex;flex-direction:column;align-items:center;justify-content:center;color:#fff;border-radius:var(--r-m)">
              <span class="badge badge-red" style="margin-bottom:8px">二维码已过期</span>
              <button class="btn btn-primary btn-sm" onclick="reloadQrCode()">重新加载</button>
            </div>
          ` : ''}
        </div>
        <div style="margin-top:16px">
          <button class="btn btn-secondary btn-sm" onclick="reloadQrCode()">刷新二维码</button>
        </div>
      </div>
    `;
    return;
  }

  // Unknown / Not Running state
  container.innerHTML = `
    <div style="padding:14px;background:rgba(245,166,35,0.08);border:1px solid rgba(245,166,35,0.2);border-radius:var(--r-m)">
      <strong style="color:var(--accent-amber)">状态异常: ${escapeHtml(state || '未知')}</strong>
      <p style="color:var(--muted);font-size:12.5px;margin-top:4px">Linux QQ 界面当前未处于会话或登录窗口，可通过下方“实时屏幕截图”查看当前容器真实画面排查。</p>
    </div>
  `;
}

let currentSettingsQrBlobUrl = null;

async function reloadQrCode() {
  const overlay = $('#qrExpiredOverlay');
  if (overlay) {
    overlay.remove();
  }
  const img = $('#loginQrImg');
  const token = getToken();
  try {
    showToast('正在刷新登录二维码…');
    const res = await fetch(`/api/qr?t=${Date.now()}&token=${encodeURIComponent(token)}`, {
      headers: { 'Authorization': `Bearer ${token}` }
    });
    if (!res.ok) {
      if (res.status === 401) {
        showLoginModal();
        return;
      }
      const text = await res.text();
      throw new Error(text || `二维码加载失败 (HTTP ${res.status})`);
    }
    const blob = await res.blob();
    if (img) {
      if (currentSettingsQrBlobUrl) {
        URL.revokeObjectURL(currentSettingsQrBlobUrl);
      }
      currentSettingsQrBlobUrl = URL.createObjectURL(blob);
      img.src = currentSettingsQrBlobUrl;
    }
    const curOverlay = $('#qrExpiredOverlay');
    if (curOverlay) {
      curOverlay.remove();
    }
    showToast('已更新登录二维码');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

function initSettingsControls() {
  $('#btnRefreshLoginState').onclick = loadSettings;

  $('#btnCaptureScreen').onclick = async () => {
    const box = $('#screenContainer');
    box.innerHTML = `<div style="color:var(--muted);padding:30px">正在截取容器屏幕…</div>`;
    const token = getToken();
    try {
      const res = await fetch(`/api/screen?t=${Date.now()}&token=${encodeURIComponent(token)}`, {
        headers: { 'Authorization': `Bearer ${token}` }
      });
      if (!res.ok) {
        if (res.status === 401) {
          showLoginModal();
          throw new Error('会话凭证失效，请重新登录');
        }
        const errText = await res.text();
        throw new Error(errText || `获取截屏失败 (HTTP ${res.status})`);
      }
      const blob = await res.blob();
      const imgUrl = URL.createObjectURL(blob);
      box.innerHTML = `
        <img src="${imgUrl}" alt="容器屏幕截图"
          style="max-width:100%;border-radius:var(--r-m);border:1px solid var(--border);box-shadow:var(--shadow-md);cursor:zoom-in"
          onclick="openLightbox('${imgUrl}')">
      `;
      showToast('已更新屏幕截图');
    } catch (err) {
      box.innerHTML = `<div style="color:var(--accent-red);padding:20px">截屏获取失败: ${escapeHtml(err.message)}</div>`;
    }
  };

  const wwSwitch = $('#wwTriggerSwitch');
  if (wwSwitch) {
    wwSwitch.onchange = async (e) => {
      const targetState = e.target.checked;
      try {
        showToast(targetState ? '正在开启 #ww 测试指令…' : '正在关闭 #ww 测试指令…');
        const res = await api('/api/settings/nw_trigger', {
          method: 'POST',
          body: JSON.stringify({ enabled: targetState }),
        });
        updateNwTriggerUI(res.enabled);
        showToast(res.enabled ? '#ww 测试指令已成功开启' : '#ww 测试指令已成功关闭');
      } catch (err) {
        e.target.checked = !targetState;
        showToast('切换失败: ' + err.message, 'error');
      }
    };
  }

  const ttlSave = $('#btnSaveMemberTtl');
  if (ttlSave) {
    const saveTtl = async (ttl) => {
      try {
        const res = await api('/api/settings/member_ttl', {
          method: 'POST',
          body: JSON.stringify({ ttl }),
        });
        updateMemberTtlUI(res.ttl);
        showToast(res.ttl > 0 ? `缓存窗口已设为 ${fmtTtl(res.ttl)}` : '缓存窗口已关闭（永久信任绑定）');
      } catch (err) {
        showToast('设置失败: ' + err.message, 'error');
      }
    };
    ttlSave.onclick = () => {
      const ttl = Number($('#memberTtlInput').value);
      if (!Number.isFinite(ttl) || ttl < 0) {
        showToast('TTL 必须是不小于 0 的数字（秒，0 = 关闭）', 'error');
        return;
      }
      saveTtl(Math.floor(ttl));
    };
    document.querySelectorAll('.ttl-preset').forEach((btn) => {
      btn.onclick = () => {
        $('#memberTtlInput').value = btn.dataset.v;
        saveTtl(Number(btn.dataset.v));
      };
    });
  }

  const protoRefresh = $('#btnRefreshProtocols');
  if (protoRefresh) protoRefresh.onclick = loadProtocols;

  const stBtn = $('#btnRunSelftest');
  if (stBtn) {
    stBtn.onclick = async () => {
      const conv = $('#selftestConv').value.trim();
      if (!conv) {
        showToast('请填写目标会话名', 'error');
        return;
      }
      stBtn.disabled = true;
      $('#selftestResult').innerHTML =
        '<div style="color:var(--muted)">自检运行中（GUI 操作串行执行，最长约两分钟；日志页可看分段进度）…</div>';
      try {
        const report = await api('/api/settings/selftest', {
          method: 'POST',
          body: JSON.stringify({ conv }),
        });
        renderSelftest(report);
        showToast(
          report.ok ? '链路自检全部通过' : `自检断在 ${report.failed_stage} 级`,
          report.ok ? 'success' : 'error',
        );
      } catch (err) {
        $('#selftestResult').innerHTML =
          `<div style="color:var(--accent-red)">自检调用失败: ${escapeHtml(err.message)}</div>`;
      } finally {
        stBtn.disabled = false;
      }
    };
  }
}

function renderSelftest(report) {
  const box = $('#selftestResult');
  if (!box || !report || !report.stages) return;
  const verdict = report.ok
    ? '<span class="badge badge-green">全部通过</span>'
    : `<span class="badge badge-red">断在 ${escapeHtml(report.failed_stage || '?')} 级</span>`;
  const when = report.started_at
    ? new Date(report.started_at * 1000).toLocaleTimeString()
    : '';
  const rows = report.stages.map((s) => `
    <div style="display:flex;gap:10px;align-items:baseline;padding:5px 0;border-bottom:1px solid var(--border)">
      <span style="font-weight:700;color:${s.ok ? 'var(--accent-green, #10b981)' : 'var(--accent-red)'}">${s.ok ? '✓' : '✗'}</span>
      <span style="font-family:var(--font-mono);font-size:12.5px;min-width:100px">${escapeHtml(s.name)}</span>
      <span style="color:var(--muted);font-size:12px;min-width:64px">${s.ms}ms</span>
      <span style="font-size:12.5px;word-break:break-all">${escapeHtml(s.detail || '')}</span>
    </div>
  `).join('');
  box.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
      ${verdict}
      <span style="color:var(--muted);font-size:12px">${when} · 目标 ${escapeHtml(report.conv || '')} · 共 ${report.ms_total || 0}ms</span>
    </div>
    ${rows}
  `;
}

function updateNwTriggerUI(enabled) {
  const sw = $('#wwTriggerSwitch');
  const badge = $('#wwTriggerBadge');
  const envVal = $('#envWwTrigger');

  if (sw) sw.checked = !!enabled;
  if (badge) {
    badge.className = enabled ? 'badge badge-green' : 'badge badge-gray';
    badge.textContent = enabled ? '已启用' : '已禁用';
  }
  if (envVal) {
    envVal.innerHTML = enabled
      ? '<span class="badge badge-green">已启用</span>'
      : '<span class="badge badge-gray">已禁用</span>';
  }
}

function fmtTtl(sec) {
  if (!sec || sec <= 0) return '已关闭';
  if (sec % 86400 === 0) return `${sec / 86400} 天`;
  if (sec % 3600 === 0) return `${sec / 3600} 小时`;
  if (sec % 60 === 0) return `${sec / 60} 分钟`;
  return `${sec} 秒`;
}

function updateMemberTtlUI(ttl) {
  const input = $('#memberTtlInput');
  const badge = $('#memberTtlBadge');
  const envVal = $('#envMemberTtl');
  const on = ttl > 0;
  if (input) input.value = ttl;
  if (badge) {
    badge.className = on ? 'badge badge-green' : 'badge badge-gray';
    badge.textContent = on ? fmtTtl(ttl) : '已关闭';
  }
  if (envVal) {
    envVal.innerHTML = on
      ? `<span class="badge badge-green">${fmtTtl(ttl)}（${ttl}s）</span>`
      : '<span class="badge badge-gray">已关闭（永久信任）</span>';
  }
}

// ----------------------------------------------------------------------------
// App Initialization
// ----------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  if (!enforceBuildVersion()) return;
  initTheme();
  initNavigation();
  initAuth();
  initEventControls();
  initTransportsControls();
  initLogsControls();
  initSettingsControls();

  const token = localStorage.getItem(TOKEN_KEY);
  if (!token) {
    showLoginModal();
  } else {
    checkQQLoginAndEnter();
  }
});

// Expose modal openers to global scope for inline onclick handlers
window.showQQLoginModal = showQQLoginModal;
window.reloadStandaloneQr = reloadStandaloneQr;
window.reloadQrCode = reloadQrCode;
