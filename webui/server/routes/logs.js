import express from 'express';
import { requireAuth } from '../auth.js';
import { upstream } from '../upstream.js';

export const logsRouter = express.Router();

logsRouter.get('/logs', requireAuth, async (req, res) => {
  const { tail = 200, after = 0, level } = req.query;

  const params = {
    tail: Math.min(parseInt(tail, 10) || 200, 1000),
    after: parseInt(after, 10) || 0,
  };
  if (level) {
    params.level = String(level).toLowerCase();
  }

  try {
    const resp = await upstream.call('get_logs', params, 5000);
    if (resp?.status === 'ok') {
      res.json(resp.data || { logs: [], latest_seq: 0 });
    } else {
      res.status(400).json({ error: resp?.message || '获取日志失败' });
    }
  } catch (err) {
    res.status(502).json({
      error: err.message || '获取服务日志失败',
      offline: !!err.offline,
    });
  }
});
