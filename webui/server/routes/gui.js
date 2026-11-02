import express from 'express';
import { requireAuth } from '../auth.js';
import { upstream } from '../upstream.js';

export const guiRouter = express.Router();

// GET /api/login_state
guiRouter.get('/login_state', requireAuth, async (req, res) => {
  try {
    const resp = await upstream.call('get_login_state', {}, 5000);
    if (resp?.status === 'ok') {
      res.json(resp.data);
    } else {
      res.status(400).json({ error: resp?.message || '获取登录态失败' });
    }
  } catch (err) {
    res.status(502).json({
      error: err.message || '获取登录态失败',
      offline: !!err.offline,
      state: 'offline',
    });
  }
});

// GET /api/qr
guiRouter.get('/qr', requireAuth, async (req, res) => {
  try {
    const upstreamResp = await upstream.fetchBinary('/qr');

    if (!upstreamResp.ok) {
      const text = await upstreamResp.text();
      return res.status(upstreamResp.status).send(text || '获取二维码失败');
    }

    res.setHeader('Content-Type', 'image/png');
    res.setHeader('Cache-Control', 'no-cache, no-store, must-revalidate');

    const arrayBuffer = await upstreamResp.arrayBuffer();
    res.send(Buffer.from(arrayBuffer));
  } catch (err) {
    res.status(502).send('获取二维码失败: ' + err.message);
  }
});

// GET /api/screen
guiRouter.get('/screen', requireAuth, async (req, res) => {
  try {
    const upstreamResp = await upstream.fetchBinary('/screen');

    if (!upstreamResp.ok) {
      const text = await upstreamResp.text();
      return res.status(upstreamResp.status).send(text || '截屏失败');
    }

    res.setHeader('Content-Type', 'image/png');
    res.setHeader('Cache-Control', 'no-cache, no-store, must-revalidate');

    const arrayBuffer = await upstreamResp.arrayBuffer();
    res.send(Buffer.from(arrayBuffer));
  } catch (err) {
    res.status(502).send('截屏失败: ' + err.message);
  }
});
