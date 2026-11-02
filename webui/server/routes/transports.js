import express from 'express';
import { requireAuth } from '../auth.js';
import { upstream } from '../upstream.js';

export const transportsRouter = express.Router();

// 通道字段的校验在上游（qqcache/transports.py）完成，这里只做白名单过滤
const FIELDS = ['type', 'host', 'port', 'url', 'token', 'platform', 'regenerate_token'];

function pick(body) {
  const out = {};
  for (const k of FIELDS) {
    if (body && k in body) out[k] = body[k];
  }
  return out;
}

async function relay(res, action, params, fallback, timeoutMs = 5000) {
  try {
    const resp = await upstream.call(action, params, timeoutMs);
    if (resp?.status !== 'ok') {
      return res.status(400).json({ error: resp?.message || fallback });
    }
    res.json(resp.data);
  } catch (err) {
    res.status(502).json({ error: err.message || fallback, offline: !!err.offline });
  }
}

// GET /api/transports
transportsRouter.get('/transports', requireAuth, async (req, res) => {
  try {
    const resp = await upstream.call('list_transports', {}, 5000);
    if (resp?.status === 'ok') {
      res.json(resp.data?.transports || []);
    } else {
      res.status(400).json({ error: resp?.message || '获取通道列表失败' });
    }
  } catch (err) {
    res.status(502).json({ error: err.message || '获取通道列表失败', offline: !!err.offline });
  }
});

// POST /api/transports —— 新建通道；起停监听端口需要时间，超时放宽
transportsRouter.post('/transports', requireAuth, (req, res) => {
  const params = pick(req.body);
  if (!params.type) return res.status(400).json({ error: '缺少通道类型' });
  return relay(res, 'add_transport', params, '新建通道失败', 20000);
});

// PATCH /api/transports/:id —— 修改通道（同一 id 原地重建）
transportsRouter.patch('/transports/:id', requireAuth, (req, res) => {
  const params = pick(req.body);
  delete params.type;
  return relay(res, 'update_transport', { id: req.params.id, ...params }, '修改通道失败', 20000);
});

// GET /api/transports/:id/token —— 令牌只在用户点「显示」时取，不随列表下发
transportsRouter.get('/transports/:id/token', requireAuth, (req, res) =>
  relay(res, 'get_transport_token', { id: req.params.id }, '获取令牌失败'));

// DELETE /api/transports/:id
transportsRouter.delete('/transports/:id', requireAuth, (req, res) =>
  relay(res, 'del_transport', { id: req.params.id }, '删除通道失败', 20000));
