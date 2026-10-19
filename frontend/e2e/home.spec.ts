import { expect, test } from '@playwright/test'

test.beforeEach(async ({ page }, testInfo) => {
  await page.setExtraHTTPHeaders({ 'X-Test-Client': testInfo.testId })
})

test('home turns on Auto for every purifier, pauses and resumes all, and lists overrides as activity', async ({ page }) => {
  test.setTimeout(60000)
  await page.goto('/')
  await page.getByTestId('login-password').fill('test-admin-password')
  await page.getByTestId('login-submit').click()
  await expect(page.getByTestId('app-header')).toBeVisible()
  const session = await (await page.request.get('/api/session')).json()
  const headers = { 'X-CSRF-Token': session.csrf_token }
  const account = await (await page.request.post('/api/accounts', { headers, data: {
    provider: 'vesync', label: 'Home test', email: 'home-e2e@example.com', password: 'p', country_code: 'SG', time_zone: 'Asia/Singapore',
  } })).json()
  // Other specs share the server, so Home sees only this account's purifier.
  await page.route('**/api/devices', async (route) => {
    const response = await route.fetch()
    const devices = await response.json()
    await route.fulfill({ response, json: devices.filter((device: { account_id: string }) => device.account_id === account.id) })
  })
  try {
    await page.reload()
    const card = page.getByTestId('device-card').filter({ hasText: 'Home test purifier' })
    await expect(page.getByTestId('home-summary')).toHaveText('1 purifier connected. None automated yet.')
    await expect(page.getByTestId('pause-all')).toHaveCount(0)
    await page.getByTestId('auto-enable-all').click()
    await expect(card.getByTestId('device-mode-badge')).toHaveText('Auto')
    await expect(page.getByTestId('auto-banner')).toHaveCount(0)
    await expect(page.getByTestId('home-summary')).toHaveText('1 purifier on Auto.')

    await page.getByTestId('pause-all').click()
    await expect(card.getByTestId('device-mode-badge')).toHaveText('Overridden')
    await expect(page.getByTestId('home-summary')).toHaveText('1 overridden.')
    await page.getByTestId('resume-all').click()
    await expect(card.getByTestId('device-mode-badge')).toHaveText('Auto')

    // A manual change while Auto runs pauses it and shows up in today's activity.
    await card.getByTestId('device-controls-toggle').click()
    await expect(card.getByTestId('automation-pause-duration')).toHaveValue('3600')
    await expect(card.getByTestId('device-speed-4')).toBeEnabled({ timeout: 8000 })
    await card.getByTestId('device-speed-4').click()
    await expect(card.getByTestId('device-speed-4')).toHaveAttribute('aria-checked', 'true')
    await expect(card.getByTestId('device-mode-badge')).toHaveText('Overridden')
    await expect(card.getByTestId('device-override')).toContainText('Until')
    await page.reload()
    await expect(page.getByTestId('activity-item').first()).toContainText('Home test purifier · Overridden')
    await card.getByTestId('device-resume').click()
    await expect(card.getByTestId('device-mode-badge')).toHaveText('Auto')
  } finally {
    await page.request.delete(`/api/accounts/${account.id}`, { headers })
  }
})
