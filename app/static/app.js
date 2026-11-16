/* 表单与请求：动作集中在 ACTIONS 里，失败必须写到页面上 */
const ACTIONS = {
  async import() {
    return request('/api/schemas/import', {
      files: ['schema_v2026_06.sql', 'schema_v2026_09.sql'],
    });
  },

  async run(button) {
    const body = {};
    if (button.dataset.forceStale) {
      body.base_version_seq = 0;
    }
    const run = await request('/api/runs', body);
    return run.reused
      ? `命中已有批次 ${run.run_id}`
      : `新批次 ${run.run_id} 序号 ${run.version_seq}`;
  },

  async 'compare-types'() {
    const oldValue = document.getElementById('old-type');
    const newValue = document.getElementById('new-type');
    const result = await request('/api/types/compare', {
      old: oldValue ? oldValue.value : '',
      new: newValue ? newValue.value : '',
    });
    const target = document.querySelector('[data-compare-result]');
    if (target) {
      const detail = result.detail ? `/${result.detail}` : '';
      target.textContent = `${result.old.norm} → ${result.new.norm}　${result.change}${detail}　${result.breaking ? '破坏' : '兼容'}`;
    }
    return `比较完成 ${result.change}`;
  },

  async 'lineage-rule'(button) {
    const result = await request('/api/lineage/rulings', {
      scope: button.dataset.scope,
      target: button.dataset.target,
      chosen_line: Number(button.dataset.line),
      base_revision: Number(button.dataset.baseRevision || 0),
      reason: reasonOf(button.dataset.target),
    });
    return `已裁定 ${result.target} 采信第 ${result.chosen_line} 行，修订 ${result.revision}`;
  },

  async 'lineage-revoke'(button) {
    const result = await request('/api/lineage/rulings/revoke', {
      scope: button.dataset.scope,
      target: button.dataset.target,
      base_revision: Number(button.dataset.baseRevision || 0),
      reason: reasonOf(button.dataset.target),
    });
    return `已撤销 ${result.target}，恢复原解析`;
  },

  async 'lineage-publish'(button) {
    const result = await request('/api/lineage/publish', { scope: button.dataset.scope });
    return result.skipped ? '没有待发布的裁定' : `已发布视图 v${result.view_seq}`;
  },
};

function reasonOf(target) {
  const input = document.querySelector(`[data-reason-for="${target}"]`);
  return input ? input.value : '';
}

async function request(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const message = (payload && (payload.message || payload.error)) || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload;
}

function flash(target, message, bad) {
  if (!target) return;
  target.textContent = message;
  target.classList.toggle('is-bad', Boolean(bad));
  target.classList.toggle('is-ok', !bad);
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const name = button.dataset.action;
  const handler = ACTIONS[name];
  if (!handler) return;
  const target = document.querySelector('[data-flash]');
  button.disabled = true;
  flash(target, '…', false);
  try {
    const message = await handler(button);
    flash(target, message || '完成', false);
    if (name === 'run' || name.startsWith('lineage-')) {
      setTimeout(() => window.location.reload(), 400);
    }
  } catch (error) {
    flash(target, error.message, true);
  } finally {
    button.disabled = false;
  }
});
