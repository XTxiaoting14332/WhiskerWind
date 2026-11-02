import dotenv from 'dotenv';
import path from 'path';
import { fileURLToPath } from 'url';

dotenv.config();

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

if (!process.env.WEBUI_PASSWORD) {
  console.error('WEBUI_PASSWORD 未设置，拒绝启动（容器内由 entrypoint 自动生成）');
  process.exit(1);
}

export const config = {
  port: parseInt(process.env.PORT || process.env.WEBUI_PORT || '3000', 10),
  host: process.env.HOST || '0.0.0.0',
  webuiPassword: process.env.WEBUI_PASSWORD,
  qqcacheUpstream: (process.env.QQCACHE_UPSTREAM || 'http://127.0.0.1:5700').replace(/\/+$/, ''),
  qqcacheToken: process.env.QQCACHE_TOKEN || '',
  sessionTtlMs: parseInt(process.env.SESSION_TTL_MS || String(7 * 24 * 60 * 60 * 1000), 10), // 7 days
  publicDir: path.resolve(__dirname, '../public'),
};
