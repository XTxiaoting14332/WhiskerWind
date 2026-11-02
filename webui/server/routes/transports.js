import express from 'express';
import { requireAuth } from '../auth.js';
import { upstream } from '../upstream.js';

export const transportsRouter = express.Router();

// GET /api/transports
transportsRouter.get('/transports', requireAuth, async (req, res) => {
  try {
    const resp = await upstream.call('list_transports', {}, 5000);
    if (resp?.status === 'ok') {
      res.json(resp.data?.transports || []);
    } else {
      res.status(400).json({ error: resp?.message || '获取传输列表失败' });
    }
  } catch (err) {
    res.status(502).json({
      error: err.message || '获取传输列表失败',
      offline: !!err.offline,
    });
  }
});

// POST /api/transports
transportsRouter.post('/transports', requireAuth, async (req, res) => {
  const { type, host, port, url, token } = req.body || {};

  if (!type || !['http', 'ws_reverse', 'http_post'].includes(type)) {
    return res.status(400).json({ error: '无效的传输类型 (必须为 http, ws_reverse, http_post 之一)' });
  }

  const params = { type };

  if (type === 'http') {
    if (!port) {
      return res.status(400).json({ error: 'HTTP 监听必须指定 port 端口' });
    }
    params.host = host || '127.0.0.1';
    params.port = parseInt(port, 10);
  } else if (type === 'ws_reverse') {
    if (!url) {
      return res.status(400).json({ error: '反向 WS 必须指定 url 地址' });
    }
    params.url = url;
    if (token) params.token = token;
  } else if (type === 'http_post') {
    if (!url) {
      return res.status(400).json({ error: 'HTTP POST 上报必须指定 url 地址' });
    }
    params.url = url;
  }

  try {
    const resp = await upstream.call('add_transport', params, 5000);
    if (resp?.status === 'ok') {
      res.json(resp.data);
    } else {
      res.status(400).json({ error: resp?.message || '添加传输失败' });
    }
  } catch (err) {
    res.status(502).json({
      error: err.message || '添加传输失败',
      offline: !!err.offline,
    });
  }
});

// DELETE /api/transports/:id
transportsRouter.delete('/transports/:id', requireAuth, async (req, res) => {
  const { id } = req.params;

  try {
    const resp = await upstream.call('del_transport', { id }, 5000);
    if (resp?.status === 'ok') {
      res.json(resp.data);
    } else {
      res.status(400).json({ error: resp?.message || '删除传输失败' });
    }
  } catch (err) {
    res.status(502).json({
      error: err.message || '删除传输失败',
      offline: !!err.offline,
    });
  }
});
