import express from 'express';
import { requireAuth } from '../auth.js';
import { upstream } from '../upstream.js';

export const protocolsRouter = express.Router();

// 透传上游的协议管理动作：list_protocols / set_protocol / get_protocol_token
async function relay(res, action, params, timeoutMs = 10000) {
  try {
    const resp = await upstream.call(action, params, timeoutMs);
    if (!resp || resp.status !== 'ok') {
      return res.status(400).json({ error: resp?.message || '上游响应异常' });
    }
    res.json(resp.data);
  } catch (err) {
    res.status(err.offline ? 502 : 500).json({ error: err.message || '执行异常' });
  }
}

// GET /api/protocols
protocolsRouter.get('/protocols', requireAuth, (req, res) => relay(res, 'list_protocols', {}));

// POST /api/protocols/:name  body: { enabled?, host?, port?, platform?, reverse_urls?, token?, regenerate_token? }
protocolsRouter.post('/protocols/:name', requireAuth, (req, res) => {
  const allowed = ['enabled', 'host', 'port', 'platform', 'reverse_urls', 'token', 'regenerate_token'];
  const changes = {};
  for (const k of allowed) {
    if (req.body && k in req.body) changes[k] = req.body[k];
  }
  // 启停会起/停监听端口，给足时间
  return relay(res, 'set_protocol', { name: req.params.name, ...changes }, 20000);
});

// GET /api/protocols/:name/token —— 令牌只在用户点「显示」时取，不随列表下发
protocolsRouter.get('/protocols/:name/token', requireAuth, (req, res) =>
  relay(res, 'get_protocol_token', { name: req.params.name }));
