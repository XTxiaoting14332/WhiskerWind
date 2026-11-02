import express from 'express';
import { requireAuth } from '../auth.js';
import { state } from '../state.js';
import { upstream } from '../upstream.js';

export const settingsRouter = express.Router();

async function checkNwTriggerStatus() {
  try {
    const resp = await upstream.call('get_test_trigger', {}, 4000);
    if (resp && resp.status === 'ok' && resp.data) {
      state.nwTriggerEnabled = !!resp.data.enabled;
    }
  } catch {
    // 上游不可达时沿用已知状态——开关默认开（与 --test-trigger 启动参数一致）
  }
  return state.nwTriggerEnabled ?? true;
}

// GET /api/settings/nw_trigger
settingsRouter.get('/settings/nw_trigger', requireAuth, async (req, res) => {
  const enabled = await checkNwTriggerStatus();
  res.json({ enabled });
});

// POST /api/settings/nw_trigger
settingsRouter.post('/settings/nw_trigger', requireAuth, async (req, res) => {
  const { enabled } = req.body || {};

  try {
    const resp = await upstream.call(
      'set_test_trigger', { enabled: !!enabled }, 8000
    );
    if (!resp || resp.status !== 'ok' || !resp.data) {
      return res.status(502).json({
        error: '切换 #ww 测试指令失败: ' + (resp?.message || '上游响应异常'),
      });
    }
    const isNowEnabled = !!resp.data.enabled;
    state.nwTriggerEnabled = isNowEnabled;
    res.json({ ok: true, enabled: isNowEnabled });
  } catch (err) {
    res.status(err.offline ? 502 : 500).json({
      error: '切换 #ww 测试指令失败: ' + (err.message || '执行异常'),
    });
  }
});

// GET /api/settings/member_ttl — 名↔号绑定缓存窗口（秒，0 = 关闭）
settingsRouter.get('/settings/member_ttl', requireAuth, async (req, res) => {
  try {
    const resp = await upstream.call('get_member_ttl', {}, 4000);
    if (resp && resp.status === 'ok' && resp.data) {
      state.memberTtl = Number(resp.data.ttl);
    }
  } catch {
    // 上游不可达时沿用已知值——窗口默认 3600s（与上游默认一致）
  }
  res.json({ ttl: state.memberTtl ?? 3600 });
});

// POST /api/settings/member_ttl
settingsRouter.post('/settings/member_ttl', requireAuth, async (req, res) => {
  const ttl = Number(req.body?.ttl);
  if (!Number.isFinite(ttl) || ttl < 0) {
    return res.status(400).json({ error: 'ttl 必须是不小于 0 的数字（秒，0 = 关闭）' });
  }
  try {
    const resp = await upstream.call('set_member_ttl', { ttl }, 8000);
    if (!resp || resp.status !== 'ok' || !resp.data) {
      return res.status(502).json({
        error: '设置成员缓存窗口失败: ' + (resp?.message || '上游响应异常'),
      });
    }
    state.memberTtl = Number(resp.data.ttl);
    res.json({ ok: true, ttl: state.memberTtl });
  } catch (err) {
    res.status(err.offline ? 502 : 500).json({
      error: '设置成员缓存窗口失败: ' + (err.message || '执行异常'),
    });
  }
});

// GET /api/settings/selftest — 最近一次自检报告（跨页面刷新留存）
settingsRouter.get('/settings/selftest', requireAuth, async (req, res) => {
  res.json({ report: state.lastSelftest || null });
});

// POST /api/settings/selftest — 分阶段链路自检；GUI 密集操作给足上游超时
settingsRouter.post('/settings/selftest', requireAuth, async (req, res) => {
  const conv = String(req.body?.conv || '').trim();
  if (!conv) {
    return res.status(400).json({ error: '缺少 conv（目标会话名）' });
  }
  try {
    const resp = await upstream.call('run_selftest', { conv }, 180000);
    if (!resp || resp.status !== 'ok' || !resp.data) {
      return res.status(502).json({
        error: '自检失败: ' + (resp?.message || '上游响应异常'),
      });
    }
    state.lastSelftest = resp.data;
    res.json(resp.data);
  } catch (err) {
    res.status(err.offline ? 502 : 500).json({
      error: '自检失败: ' + (err.message || '执行异常'),
    });
  }
});
