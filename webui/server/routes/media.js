import express from 'express';
import { requireAuth } from '../auth.js';
import { upstream } from '../upstream.js';

export const mediaRouter = express.Router();

mediaRouter.get('/media', requireAuth, async (req, res) => {
  let mediaPath = req.query.path;
  if (!mediaPath) {
    return res.status(400).send('Missing path parameter');
  }

  // Strip file:// prefix if present
  if (typeof mediaPath === 'string' && mediaPath.startsWith('file://')) {
    mediaPath = mediaPath.slice(7);
  }

  try {
    const upstreamResp = await upstream.fetchBinary('/media', { path: mediaPath });

    if (!upstreamResp.ok) {
      return res.status(upstreamResp.status).send(await upstreamResp.text());
    }

    // Forward content-type and headers
    const contentType = upstreamResp.headers.get('content-type') || 'application/octet-stream';
    res.setHeader('Content-Type', contentType);
    const contentLength = upstreamResp.headers.get('content-length');
    if (contentLength) {
      res.setHeader('Content-Length', contentLength);
    }
    res.setHeader('Cache-Control', 'public, max-age=86400');

    // Stream body to client
    const arrayBuffer = await upstreamResp.arrayBuffer();
    res.send(Buffer.from(arrayBuffer));
  } catch (err) {
    res.status(502).send('Error proxying media: ' + err.message);
  }
});
