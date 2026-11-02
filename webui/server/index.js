import http from 'http';
import express from 'express';
import cors from 'cors';
import { WebSocketServer } from 'ws';
import path from 'path';

import { config } from './config.js';
import { state } from './state.js';
import { upstream } from './upstream.js';
import { verifyWsAuth } from './auth.js';

import { authRouter } from './routes/auth.js';
import { statusRouter } from './routes/status.js';
import { mediaRouter } from './routes/media.js';
import { transportsRouter } from './routes/transports.js';
import { logsRouter } from './routes/logs.js';
import { guiRouter } from './routes/gui.js';
import { ob11Router } from './routes/ob11.js';
import { settingsRouter } from './routes/settings.js';
import { protocolsRouter } from './routes/protocols.js';

const app = express();
const server = http.createServer(app);

// Enable CORS and JSON parsing
app.use(cors());
app.use(express.json({ limit: '25mb' }));
app.use(express.urlencoded({ extended: true, limit: '25mb' }));

// Mount API routes
app.use('/api', authRouter);
app.use('/api', statusRouter);
app.use('/api', mediaRouter);
app.use('/api', transportsRouter);
app.use('/api', logsRouter);
app.use('/api', guiRouter);
app.use('/api', settingsRouter);
app.use('/api', protocolsRouter);
app.use('/ob11', ob11Router);

// 静态资源：no-cache 让浏览器每次回源校验（未变时 304），
// 避免 index.html 更新了而 app.js 还用旧缓存
app.use(express.static(config.publicDir, {
  setHeaders: (res, filePath) => {
    if (/\.(html|js|css)$/.test(filePath)) {
      res.setHeader('Cache-Control', 'no-cache');
    }
  },
}));

// SPA fallback for frontend client routing
app.get('*', (req, res, next) => {
  if (req.path.startsWith('/api') || req.path.startsWith('/ob11')) {
    return next();
  }
  res.sendFile(path.join(config.publicDir, 'index.html'));
});

// ----------------------------------------------------------------------------
// WebSocket Server on /ws
// ----------------------------------------------------------------------------
const wss = new WebSocketServer({ noServer: true });

server.on('upgrade', (req, socket, head) => {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);

  if (url.pathname !== '/ws') {
    socket.destroy();
    return;
  }

  req.query = Object.fromEntries(url.searchParams.entries());

  // Authenticate WebSocket connection
  if (!verifyWsAuth(req)) {
    socket.write('HTTP/1.1 401 Unauthorized\r\n\r\n');
    socket.destroy();
    return;
  }

  wss.handleUpgrade(req, socket, head, (ws) => {
    wss.emit('connection', ws, req);
  });
});

wss.on('connection', (ws) => {
  state.addWsClient(ws);

  // Send initial state message on connect
  ws.send(JSON.stringify({
    type: 'status',
    data: { online: upstream.isOnline },
  }));

  // Handle incoming messages from frontend client if needed
  ws.on('message', (raw) => {
    try {
      const msg = JSON.parse(raw.toString());
      if (msg.type === 'ping') {
        ws.send(JSON.stringify({ type: 'pong' }));
      }
    } catch {}
  });
});

// ----------------------------------------------------------------------------
// Start Server & Upstream Worker
// ----------------------------------------------------------------------------
server.listen(config.port, config.host, () => {
  console.log(`[WebUI BFF] Listening on http://${config.host}:${config.port}`);
  console.log(`[WebUI BFF] Upstream qqcache configured at: ${config.qqcacheUpstream}`);
  upstream.init();
});

function shutdown() {
  console.log('\n[WebUI BFF] Shutting down gracefully...');
  upstream.destroy();
  server.close(() => {
    process.exit(0);
  });
  // close() 等所有长连接（浏览器 WS、keep-alive）自己断——有页面开着就永远
  // 等不到，进程挂在「关闭中」：端口已关但 pgrep 还在，entrypoint 不会重拉
  setTimeout(() => process.exit(0), 3000).unref();
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
