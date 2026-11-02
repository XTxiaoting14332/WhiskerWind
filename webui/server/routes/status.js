import express from 'express';
import os from 'os';
import { requireAuth } from '../auth.js';
import { upstream } from '../upstream.js';
import { state } from '../state.js';
import { config } from '../config.js';

export const statusRouter = express.Router();

statusRouter.get('/status', requireAuth, async (req, res) => {
  const result = {
    upstream_online: upstream.isOnline,
    upstream_url: config.qqcacheUpstream,
    online: false,
    good: false,
    self_id: 0,
    self_name: '',
    avatar_url: '',
    version: null,
    uptime_s: Math.floor((Date.now() - state.eventStats.startedAt) / 1000),
    login_state: {
      state: upstream.isOnline ? 'unknown' : 'offline',
      qr_expired: false,
      self_id: 0,
      self_name: '',
    },
    system_info: null,
    transports: [],
    transport_summary: {
      http: null,
      post_urls: [],
      reverse_ws: [],
      ws_clients: state.wsClients.size,
    },
    event_stats: state.getStats(),
    recent_events: state.getRecentEvents(20),
  };

  if (!upstream.isOnline) {
    // Provide local system info when upstream container is offline
    const cpus = os.cpus() || [];
    const totalMem = os.totalmem();
    const freeMem = os.freemem();
    const usedMem = totalMem - freeMem;
    result.system_info = {
      self_id: 0,
      self_name: '',
      avatar_url: '',
      login_state: 'offline',
      versions: { qqcache: '0.1.0', onebot: 'v11', qq: '' },
      system: {
        platform: `${os.type()} ${os.arch()}`,
        uptime_s: Math.floor(os.uptime()),
        cpu_model: cpus[0]?.model || 'Processor',
        cpu_cores: cpus.length,
        cpu_percent: 0,
        mem: {
          total: totalMem,
          used: usedMem,
          percent: totalMem ? Math.round((usedMem / totalMem) * 1000) / 10 : 0,
        },
        disk: { path: '/', total: 0, used: 0, percent: 0 },
      },
      qq_proc: { pid: null, process_count: 0, rss_bytes: 0 },
      stats: { conversations: 0, aliases: 0, seen_messages: 0, transports: 0 },
    };
    return res.json(result);
  }

  // Fetch status, login_state, version, transports, system_info in parallel
  const [statusRes, loginStateRes, versionRes, transportsRes, sysInfoRes] = await Promise.allSettled([
    upstream.call('get_status', {}, 4000),
    upstream.call('get_login_state', {}, 4000),
    upstream.call('get_version_info', {}, 4000),
    upstream.call('list_transports', {}, 4000),
    upstream.call('get_system_info', {}, 4000),
  ]);

  if (statusRes.status === 'fulfilled' && statusRes.value?.status === 'ok') {
    result.online = !!statusRes.value.data?.online;
    result.good = !!statusRes.value.data?.good;
  }

  if (loginStateRes.status === 'fulfilled' && loginStateRes.value?.status === 'ok') {
    result.login_state = loginStateRes.value.data;
    result.self_id = loginStateRes.value.data?.self_id || 0;
    result.self_name = loginStateRes.value.data?.self_name || '';

    // Upstream get_status returns online=true whenever the QQ process is running,
    // but from the user perspective QQ is only online when successfully logged in.
    if (result.login_state?.state && result.login_state.state !== 'logged_in') {
      result.online = false;
    }
  }

  if (versionRes.status === 'fulfilled' && versionRes.value?.status === 'ok') {
    result.version = versionRes.value.data;
  }

  if (sysInfoRes.status === 'fulfilled' && sysInfoRes.value?.status === 'ok') {
    result.system_info = sysInfoRes.value.data;
    if (result.system_info?.avatar_url) {
      result.avatar_url = result.system_info.avatar_url;
    }
  } else {
    const cpus = os.cpus() || [];
    const totalMem = os.totalmem();
    const freeMem = os.freemem();
    const usedMem = totalMem - freeMem;
    result.system_info = {
      self_id: result.self_id,
      self_name: result.self_name,
      avatar_url: result.self_id ? `https://q1.qlogo.cn/g?b=qq&nk=${result.self_id}&s=640` : '',
      login_state: result.login_state?.state || 'unknown',
      versions: {
        qqcache: result.version?.app_version || '0.1.0',
        onebot: 'v11',
        qq: '',
      },
      system: {
        platform: `${os.type()} ${os.arch()}`,
        uptime_s: Math.floor(os.uptime()),
        cpu_model: cpus[0]?.model || 'Processor',
        cpu_cores: cpus.length,
        cpu_percent: 0,
        mem: {
          total: totalMem,
          used: usedMem,
          percent: totalMem ? Math.round((usedMem / totalMem) * 1000) / 10 : 0,
        },
        disk: { path: '/', total: 0, used: 0, percent: 0 },
      },
      qq_proc: { pid: null, process_count: 0, rss_bytes: 0 },
      stats: {
        conversations: 0,
        aliases: 0,
        seen_messages: result.event_stats?.total || 0,
        transports: 0,
      },
    };
  }


  if (transportsRes.status === 'fulfilled' && transportsRes.value?.status === 'ok') {
    const list = transportsRes.value.data?.transports || [];
    result.transports = list;

    // Summarize
    for (const t of list) {
      if (t.type === 'http' && !result.transport_summary.http) {
        result.transport_summary.http = t.address || t.http_url;
      } else if (t.type === 'http_post') {
        result.transport_summary.post_urls.push({
          id: t.id,
          url: t.address || t.url,
          status: t.status,
          source: t.source,
        });
      } else if (t.type === 'ws_reverse') {
        result.transport_summary.reverse_ws.push({
          id: t.id,
          url: t.address || t.url,
          status: t.status,
          source: t.source,
        });
      }
    }
  }

  res.json(result);
});
