// In-memory application state & event tracking

export class AppState {
  constructor() {
    this.eventStats = {
      message: 0,
      message_sent: 0,
      meta_event: 0,
      notice: 0,
      request: 0,
      total: 0,
      startedAt: Date.now(),
    };
    this.recentEvents = []; // last 100 events
    this.maxRecentEvents = 100;
    this.wsClients = new Set();
    this.nwTriggerEnabled = null;   // #ww 测试指令开关（上游同步）
    this.memberTtl = null;          // 名↔号绑定缓存窗口秒数（上游同步）
    this.lastSelftest = null;       // 最近一次链路自检报告
  }

  addWsClient(ws) {
    this.wsClients.add(ws);
    ws.on('close', () => this.wsClients.delete(ws));
    ws.on('error', () => this.wsClients.delete(ws));
  }

  broadcast(messageObj) {
    const data = typeof messageObj === 'string' ? messageObj : JSON.stringify(messageObj);
    for (const ws of this.wsClients) {
      if (ws.readyState === 1 /* OPEN */) {
        try {
          ws.send(data);
        } catch {
          this.wsClients.delete(ws);
        }
      }
    }
  }

  recordEvent(event) {
    if (!event || typeof event !== 'object') return;

    this.eventStats.total++;
    const postType = event.post_type || 'unknown';
    if (this.eventStats[postType] !== undefined) {
      this.eventStats[postType]++;
    }

    // Add to recent events (FIFO ring buffer)
    this.recentEvents.unshift({
      ...event,
      _received_at: Date.now(),
    });
    if (this.recentEvents.length > this.maxRecentEvents) {
      this.recentEvents.pop();
    }

    // Broadcast raw event to frontend clients
    this.broadcast({
      type: 'event',
      data: event,
    });
  }

  getStats() {
    return {
      ...this.eventStats,
      uptimeSeconds: Math.floor((Date.now() - this.eventStats.startedAt) / 1000),
    };
  }

  getRecentEvents(limit = 20) {
    return this.recentEvents.slice(0, limit);
  }
}

export const state = new AppState();
