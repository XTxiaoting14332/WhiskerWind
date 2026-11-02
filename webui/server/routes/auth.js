import express from 'express';
import { verifyPassword, createSession, destroySession, requireAuth } from '../auth.js';
import { upstream } from '../upstream.js';

export const authRouter = express.Router();

authRouter.post('/login', async (req, res) => {
  const { password } = req.body || {};

  if (!password) {
    return res.status(400).json({ error: '缺少面板密码' });
  }

  if (!verifyPassword(password)) {
    return res.status(401).json({ error: '密码错误' });
  }

  const token = createSession();

  // Set cookie for seamless <img>, media, and download requests
  res.setHeader('Set-Cookie', `webui_session=${token}; Path=/; SameSite=Lax; Max-Age=604800`);

  // Test upstream connectivity to inform frontend right away
  let upstreamOnline = upstream.isOnline;
  try {
    const statusResp = await upstream.call('get_status', {}, 2500);
    upstreamOnline = statusResp?.status === 'ok';
  } catch {
    upstreamOnline = false;
  }

  res.json({
    token,
    upstream_online: upstreamOnline,
  });
});

authRouter.post('/logout', (req, res) => {
  const token = req.sessionToken || (req.headers['authorization']?.replace(/^Bearer\s+/i, ''));
  if (token) {
    destroySession(token);
  }
  res.setHeader('Set-Cookie', 'webui_session=; Path=/; Max-Age=0');
  res.json({ ok: true });
});

authRouter.get('/me', requireAuth, (req, res) => {
  res.json({
    authenticated: true,
    upstream_online: upstream.isOnline,
  });
});
