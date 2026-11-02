import WebSocket from 'ws';
import { config } from './config.js';
import { state } from './state.js';

export class UpstreamClient {
  constructor() {
    this.isOnline = false;
    this.eventWs = null;
    this.logWs = null;
    this.latestLogSeq = 0;
    this.reconnectTimer = null;
    this.logReconnectTimer = null;
    this.healthCheckTimer = null;
    this.destroyed = false;
  }

  init() {
    this.connectEventWs();
    this.connectLogWs();
    this.startHealthCheck();
  }

  destroy() {
    this.destroyed = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    if (this.logReconnectTimer) clearTimeout(this.logReconnectTimer);
    if (this.healthCheckTimer) clearInterval(this.healthCheckTimer);
    if (this.eventWs) {
      try { this.eventWs.close(); } catch {}
    }
    if (this.logWs) {
      try { this.logWs.close(); } catch {}
    }
  }

  setOnlineStatus(online) {
    if (this.isOnline !== online) {
      this.isOnline = online;
      state.broadcast({
        type: 'status',
        data: { online: this.isOnline },
      });
    }
  }

  getWsUrl(path = '') {
    const httpUrl = new URL(config.qqcacheUpstream);
    const wsProto = httpUrl.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${wsProto}//${httpUrl.host}${path}`;
  }

  // --------------------------------------------------------------------------
  // HTTP REST API
  // --------------------------------------------------------------------------
  async call(action, params = {}, timeoutMs = 65000) {
    const url = `${config.qqcacheUpstream}/${action}`;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);

    try {
      const resp = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${config.qqcacheToken}`,
        },
        body: JSON.stringify(params),
        signal: controller.signal,
      });

      this.setOnlineStatus(true);
      const text = await resp.text();
      try {
        const json = JSON.parse(text);
        return json;
      } catch {
        return {
          status: 'failed',
          retcode: resp.status,
          message: text || `HTTP ${resp.status}`,
          data: null,
        };
      }
    } catch (err) {
      if (err.name === 'AbortError') {
        throw new Error(`上游请求超时 (${Math.round(timeoutMs / 1000)}s)`);
      }
      this.setOnlineStatus(false);
      const isConnError = err.code === 'ECONNREFUSED' || err.code === 'ENOTFOUND' || err.message?.includes('fetch failed');
      const errObj = new Error(isConnError ? '容器离线（无法连接到上游 qqcache 服务）' : err.message);
      errObj.offline = isConnError;
      throw errObj;
    } finally {
      clearTimeout(timeout);
    }
  }

  async fetchBinary(path, query = {}) {
    const url = new URL(path, config.qqcacheUpstream);
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null) {
        url.searchParams.set(k, String(v));
      }
    }

    try {
      const resp = await fetch(url.toString(), {
        headers: {
          'Authorization': `Bearer ${config.qqcacheToken}`,
        },
      });
      this.setOnlineStatus(true);
      return resp;
    } catch (err) {
      this.setOnlineStatus(false);
      const errObj = new Error('容器离线');
      errObj.offline = true;
      throw errObj;
    }
  }

  // --------------------------------------------------------------------------
  // Event WebSocket Client (Forward WS to receive all OB11 events)
  // --------------------------------------------------------------------------
  connectEventWs() {
    if (this.destroyed) return;
    const wsUrl = `${this.getWsUrl('/')}?access_token=${encodeURIComponent(config.qqcacheToken)}`;

    try {
      this.eventWs = new WebSocket(wsUrl);

      this.eventWs.on('open', () => {
        this.setOnlineStatus(true);
      });

      this.eventWs.on('message', (raw) => {
        try {
          const text = raw.toString();
          const json = JSON.parse(text);
          // An event does NOT have an echo field
          if (json && !json.echo && json.post_type) {
            state.recordEvent(json);
          }
        } catch (e) {
          console.error('[Upstream WS] Parse event error:', e);
        }
      });

      this.eventWs.on('close', () => {
        this.eventWs = null;
        if (!this.destroyed) {
          this.reconnectTimer = setTimeout(() => this.connectEventWs(), 3000);
        }
      });

      this.eventWs.on('error', () => {
        if (this.eventWs) {
          try { this.eventWs.close(); } catch {}
        }
      });
    } catch (err) {
      this.eventWs = null;
      if (!this.destroyed) {
        this.reconnectTimer = setTimeout(() => this.connectEventWs(), 3000);
      }
    }
  }

  // --------------------------------------------------------------------------
  // Live Log Stream WebSocket Client (/logs/stream)
  // --------------------------------------------------------------------------
  connectLogWs() {
    if (this.destroyed) return;
    let url = `${this.getWsUrl('/logs/stream')}?access_token=${encodeURIComponent(config.qqcacheToken)}`;
    if (this.latestLogSeq > 0) {
      url += `&after=${this.latestLogSeq}`;
    }

    try {
      this.logWs = new WebSocket(url);

      this.logWs.on('open', () => {
        // Connected to log stream
      });

      this.logWs.on('message', (raw) => {
        try {
          const text = raw.toString();
          const json = JSON.parse(text);
          if (json && json.type === 'log' && json.data) {
            if (json.data.seq && json.data.seq > this.latestLogSeq) {
              this.latestLogSeq = json.data.seq;
            }
            state.broadcast(json);
          }
        } catch {}
      });

      this.logWs.on('close', () => {
        this.logWs = null;
        if (!this.destroyed) {
          this.logReconnectTimer = setTimeout(() => this.connectLogWs(), 3000);
        }
      });

      this.logWs.on('error', () => {
        if (this.logWs) {
          try { this.logWs.close(); } catch {}
        }
      });
    } catch {
      this.logWs = null;
      if (!this.destroyed) {
        this.logReconnectTimer = setTimeout(() => this.connectLogWs(), 3000);
      }
    }
  }

  // --------------------------------------------------------------------------
  // Periodic Health Check
  // --------------------------------------------------------------------------
  startHealthCheck() {
    const check = async () => {
      try {
        const resp = await this.call('get_status', {}, 4000);
        if (resp && resp.status === 'ok') {
          this.setOnlineStatus(true);
        }
      } catch (err) {
        if (err.offline) {
          this.setOnlineStatus(false);
        }
      }
    };

    check();
    this.healthCheckTimer = setInterval(check, 6000);
  }
}

export const upstream = new UpstreamClient();
