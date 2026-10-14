import { expect, test, type Request, type Route } from '@playwright/test'

test.beforeEach(async ({ page }, testInfo) => {
  await page.setExtraHTTPHeaders({ 'X-Test-Client': testInfo.testId })
})

test('create an MCP access token, use it once, and revoke it', async ({ page }) => {
  await page.goto('/accounts')
  await page.getByTestId('login-password').fill('test-admin-password')
  await page.getByTestId('login-submit').click()
  const section = page.getByTestId('agent-access')
  await expect(section.getByTestId('agent-status')).toContainText('Not configured')
  await expect(section.getByTestId('mcp-url')).toHaveText('http://127.0.0.1:8091/mcp')

  const name = `assistant-${test.info().testId}`
  await section.getByTestId('token-name').fill(name)
  await section.getByTestId('token-scope-rules').check()
  await section.getByTestId('token-create').click()
  const token = await section.getByTestId('token-value').inputValue()
  expect(token).toMatch(/^cc_/)
  const row = section.getByTestId('token-row').filter({ hasText: name })
  await expect(row).toContainText('Read · Edit rules')

  const call = () => page.request.post('/mcp', {
    headers: { Authorization: `Bearer ${token}`, Accept: 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18' },
    data: { jsonrpc: '2.0', id: 1, method: 'tools/list', params: {} },
  })
  expect((await call()).status()).toBe(200)
  await page.reload()
  await expect(section.getByTestId('token-value')).toHaveCount(0)
  await expect(row).toContainText('Last used')

  await row.getByTestId('token-revoke').click()
  await expect(row).toHaveCount(0)
  expect((await call()).status()).toBe(401)
})

test('rule page shows the planner working and how it worked', async ({ page }) => {
  const steps = [
    { at: 1, kind: 'thought', text: 'Indoor PM2.5 peaks between 18:00 and 21:00.' },
    { at: 2, kind: 'tool', name: 'get_device_history', arguments: '{"hours":168}' },
    { at: 3, kind: 'result', name: 'get_device_history' },
  ]
  let finished = false
  await page.route('**/automation/runs*', route => route.fulfill({
    json: [{ id: 'r1', status: finished ? 'completed' : 'running', model: 'test-model', attempts: 1,
      llm_calls: finished ? 2 : 1, error_kind: null, started_at: 1, finished_at: finished ? 13 : null, steps }],
  }))
  const asAgent = async (route: Route, request: Request) => {
    if (request.method() !== 'GET') return route.continue()
    const state = await (await route.fetch()).json()
    state.owner = 'agent'
    state.job = { status: 'claimed', reasons: [] }
    state.run = { id: 'r1', status: finished ? 'completed' : 'running', model: 'test-model',
      attempts: 1, llm_calls: finished ? 2 : 1, error_kind: null, started_at: 1, finished_at: finished ? 13 : null, step_count: 3 }
    await route.fulfill({ json: state })
  }
  await page.route('**/automation', asAgent)
  await page.route('**/automation/context', asAgent)

  await page.goto('/')
  await page.getByTestId('login-password').fill('test-admin-password')
  await page.getByTestId('login-submit').click()
  await expect(page.getByTestId('app-header')).toBeVisible()
  const session = await (await page.request.get('/api/session')).json()
  const headers = { 'X-CSRF-Token': session.csrf_token }
  const account = await (await page.request.post('/api/accounts', { headers, data: {
    provider: 'vesync', label: 'Activity test', email: 'agent-activity@example.com', password: 'p', country_code: 'SG', time_zone: 'Asia/Singapore',
  } })).json()
  try {
    await page.reload()
    await page.getByTestId('device-card').filter({ hasText: 'Activity test purifier' }).getByTestId('device-rules-link').click()

    await expect(page.getByTestId('automation-working')).toBeVisible()
    const activity = page.getByTestId('agent-activity')
    await expect(activity).toContainText('What the agent is doing')
    await expect(activity.getByTestId('agent-run-status')).toHaveText('Working on the rules')
    await expect(activity.getByTestId('agent-steps')).toContainText('Indoor PM2.5 peaks')
    await expect(activity.getByTestId('agent-steps')).toContainText('Read reading history')

    finished = true
    await expect(activity.getByTestId('agent-run-status')).toHaveText('Updated the rules', { timeout: 10000 })
    await expect(activity).toContainText('test-model · 2 model calls · 12s')
    await page.reload()
    await expect(page.getByTestId('agent-steps')).toBeHidden()
    await page.getByTestId('agent-activity').getByText('How the agent worked').click()
    await expect(page.getByTestId('agent-steps')).toContainText('Indoor PM2.5 peaks')
  } finally {
    await page.request.delete(`/api/accounts/${account.id}`, { headers })
  }
})
