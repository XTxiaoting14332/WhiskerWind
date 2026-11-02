import crypto from 'crypto';
import { config } from './config.js';

// In-memory token store: token -> { createdAt, expiresAt }
const sessions = new Map();

function parseCookies(cookieHeader) {
  const list = {};
  if (!cookieHeader) return list;
  cookieHeader.split(';').forEach((cookie) => {
    let [name, ...rest] = cookie.split('=');
    name = name?.trim();
    if (!name) return;
    const value = rest.join('=').trim();
    list[name] = decodeURIComponent(value);
  });
  return list;
}

export function createSession() {
  const token = crypto.randomBytes(32).toString('hex');
  const now = Date.now();
  sessions.set(token, {
    createdAt: now,
    expiresAt: now + config.sessionTtlMs,
  });
  return token;
}

export function verifySession(token) {
  if (!token) return false;
  const sess = sessions.get(token);
  if (!sess) return false;
  if (Date.now() > sess.expiresAt) {
    sessions.delete(token);
    return false;
  }
  return true;
}

export function destroySession(token) {
  if (token) {
    sessions.delete(token);
  }
}

export function verifyPassword(password) {
  if (typeof password !== 'string') return false;
  const expected = config.webuiPassword;
  if (password.length !== expected.length) return false;
  try {
    return crypto.timingSafeEqual(Buffer.from(password), Buffer.from(expected));
  } catch {
    return password === expected;
  }
}

export function extractToken(req) {
  // 1. Authorization header: Bearer <token>
  const authHeader = req.headers['authorization'];
  if (authHeader && authHeader.startsWith('Bearer ')) {
    return authHeader.slice(7).trim();
  }

  // 2. Query parameter: ?token=<token> (useful for <img>, WebSocket, direct media links)
  if (req.query && req.query.token) {
    return String(req.query.token).trim();
  }
  if (req.url && req.url.includes('?')) {
    try {
      const u = new URL(req.url, 'http://localhost');
      const t = u.searchParams.get('token');
      if (t) return t.trim();
    } catch {}
  }

  // 3. Cookie header: webui_session=<token>
  const cookies = parseCookies(req.headers['cookie']);
  if (cookies.webui_session) {
    return cookies.webui_session;
  }

  return null;
}

export function requireAuth(req, res, next) {
  const token = extractToken(req);
  if (!token || !verifySession(token)) {
    return res.status(401).json({
      error: 'unauthorized',
      message: '需要登录或会话已过期',
    });
  }
  req.sessionToken = token;
  next();
}

export function verifyWsAuth(req) {
  const token = extractToken(req);
  return token ? verifySession(token) : false;
}
