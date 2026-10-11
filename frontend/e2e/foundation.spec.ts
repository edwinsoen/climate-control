import { expect, test, type Page } from '@playwright/test'

function deviceCard(page: Page, name: string) {
  return page.getByTestId('device-card').filter({ has: page.getByTestId('device-name').filter({ hasText: name }) })
}

function accountCard(page: Page, name: string) {
  return page.getByTestId('account-card').filter({ has: page.getByTestId('account-name').filter({ hasText: name }) })
}

test.beforeEach(async ({ page }, testInfo) => {
  await page.setExtraHTTPHeaders({ 'X-Test-Client': testInfo.testId })
})

test('sign in, connect two accounts, inspect status, reconnect, remove, and sign out', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByTestId('login-title')).toBeVisible()
  await page.getByTestId('login-password').fill('test-admin-password')
  await page.getByTestId('login-submit').click()
  await expect(page.getByTestId('empty-devices')).toHaveText('No devices connected.')

  async function connect(label: string, email: string) {
    await page.getByTestId('app-header').getByTestId('connect-account').click()
    const dialog = page.getByTestId('account-dialog')
    await expect(dialog.getByTestId('connection-provider')).toHaveValue('vesync')
    await dialog.getByTestId('connection-label').fill(label)
    await dialog.getByTestId('connection-email').fill(email)
    await dialog.getByTestId('connection-password').fill('vesync-test-password')
    await dialog.getByTestId('connection-country').selectOption({ label: 'Singapore' })
    await dialog.getByTestId('connection-time-zone').selectOption('Asia/Singapore')
    await dialog.getByTestId('connection-submit').click()
    await expect(dialog).not.toBeVisible()
  }

  await connect('Home', 'home@example.com')
  await expect(deviceCard(page, 'Home purifier').getByTestId('device-name')).toBeVisible()
  await expect(deviceCard(page, 'Home purifier').getByTestId('device-pm25')).toHaveText('8')
  await expect(deviceCard(page, 'Home purifier').getByTestId('device-filter-life')).toHaveText('86%')
  const chart = deviceCard(page, 'Home purifier').getByTestId('history-chart')
  const historyToggle = page.getByTestId('device-card').first().getByTestId('history-toggle')
  await expect(chart).not.toBeVisible()
  await historyToggle.click()
  await expect(chart).toBeVisible()
  await deviceCard(page, 'Home purifier').getByTestId('history-periods').getByTestId('history-period-7d').click()
  await expect(chart).toBeVisible()
  await expect(page.getByRole('combobox', { name: /^History metric/ })).toHaveCount(0)
  await deviceCard(page, 'Home purifier').getByTestId('history-periods').getByTestId('history-period-24h').click()
  await historyToggle.focus()
  await historyToggle.press('Enter')
  await expect(chart).not.toBeVisible()
  await expect(page.getByRole('button', { name: /Refresh/ })).toHaveCount(0)
  await connect('Bedroom', 'bedroom@example.com')
  await expect(page.getByTestId('device-card')).toHaveCount(2)
  await page.getByTestId('nav-accounts').click()
  await expect(page.getByTestId('page-title')).toHaveText('Connected accounts')
  const homeCard = accountCard(page, 'Home')
  await expect(homeCard).not.toContainText('home@example.com')
  await expect(homeCard).not.toContainText('Asia/Singapore')
  await expect(homeCard).not.toContainText('SG')
  const actions = homeCard.getByTestId('account-actions')
  await actions.focus()
  await actions.press('ArrowDown')
  await expect(page.getByTestId('account-reconnect')).toBeFocused()
  await page.getByTestId('account-reconnect').press('ArrowDown')
  await expect(page.getByTestId('account-remove')).toBeFocused()
  await page.getByTestId('account-remove').press('Escape')
  await expect(page.getByTestId('account-menu')).toHaveCount(0)
  await expect(actions).toBeFocused()
  await actions.click()
  await page.getByTestId('page-title').click()
  await expect(page.getByTestId('account-menu')).toHaveCount(0)

  await actions.click()
  await page.getByTestId('account-reconnect').click()
  await expect(page.getByTestId('account-dialog').getByTestId('connection-provider')).toHaveValue('vesync')
  await expect(page.getByTestId('account-dialog').getByTestId('connection-provider')).toBeDisabled()
  await expect(page.getByTestId('account-dialog').getByTestId('connection-country')).toHaveValue('SG')
  await expect(page.getByTestId('account-dialog').getByTestId('connection-time-zone')).toHaveValue('Asia/Singapore')
  await page.getByTestId('account-dialog').getByTestId('connection-password').fill('new-vesync-test-password')
  await page.getByTestId('account-dialog').getByTestId('connection-submit').click()
  await expect(page.getByTestId('account-dialog')).not.toBeVisible()

  await actions.click()
  await page.getByTestId('account-remove').click()
  const confirmation = homeCard.getByTestId('account-remove-confirmation')
  await expect(confirmation).toBeVisible()
  await expect(confirmation.getByTestId('account-cancel-remove')).toBeFocused()
  await expect(page.getByTestId('account-card')).toHaveCount(2)
  await confirmation.getByTestId('account-cancel-remove').click()
  await expect(confirmation).not.toBeVisible()
  await expect(actions).toBeFocused()
  await actions.click()
  await page.getByTestId('account-remove').click()
  await confirmation.getByTestId('account-confirm-remove').click()
  await expect(page.getByTestId('account-card')).toHaveCount(1)
  await page.getByTestId('nav-overview').click()
  await expect(page.getByTestId('device-card')).toHaveCount(1)
  await page.screenshot({ path: 'test-results/dashboard.png', fullPage: true })
  await page.getByTestId('user-menu-trigger').focus()
  await page.getByTestId('user-menu-trigger').press('ArrowDown')
  await expect(page.getByTestId('sign-out')).toBeFocused()
  await page.getByTestId('sign-out').press('Escape')
  await expect(page.getByTestId('user-menu-trigger')).toBeFocused()
  await page.getByTestId('user-menu-trigger').click()
  await page.getByTestId('sign-out').click()
  await expect(page.getByTestId('login-title')).toBeVisible()
})

test('history charts collapse, preserve gaps, and show values only on hover', async ({ page }) => {
  const end = Date.now() / 1000
  await page.route('**/api/devices/*/history?hours=*', async (route) => {
    await route.fulfill({ json: {
      source: 'recorded', start: end - 86400, end, interval_seconds: 240,
      metrics: {
        pm25: [
          { recorded_at: end - 43200, value: 10, min: 8, max: 12, count: 2 },
          { recorded_at: end - 42960, value: 20, min: 15, max: 25, count: 2 },
          { recorded_at: end - 240, value: 5, min: 5, max: 5, count: 1 },
        ], pm1: [], pm10: [], air_quality: [
          { recorded_at: end - 43200, value: 2, min: 2, max: 2, count: 2 },
          { recorded_at: end - 42960, value: 4, min: 4, max: 4, count: 2 },
        ],
      },
    } })
  })
  await page.goto('/')
  await page.getByTestId('login-password').fill('test-admin-password')
  await page.getByTestId('login-submit').click()
  const card = page.getByTestId('device-card').first()
  const chart = card.getByTestId('history-chart')
  await card.getByTestId('history-toggle').click()
  await expect(chart).toBeVisible()
  await expect(chart.getByTestId('history-segment')).toHaveCount(2)
  await expect(card.getByTestId('history-reading')).toHaveCount(0)
  const bounds = await chart.boundingBox()
  expect(bounds).not.toBeNull()
  await chart.hover({ position: { x: bounds!.width * 0.65, y: bounds!.height / 2 } })
  await expect(card.getByTestId('history-reading')).toContainText('20 µg/m³')
  await expect(card.getByTestId('history-selection')).toHaveClass(/\bquality-poor\b/)
  await expect(chart.getByTestId('history-reading')).not.toContainText('Range')
  await expect(chart.getByTestId('history-reading')).not.toContainText('Poor')
  await card.getByTestId('device-name').hover()
  await expect(card.getByTestId('history-reading')).toHaveCount(0)
  await card.getByTestId('history-toggle').click()
  await expect(card.getByTestId('history-periods')).toHaveCount(0)
  await expect(chart).not.toBeVisible()
  await card.getByTestId('history-toggle').click()
  await card.getByTestId('history-period-30d').click()
  await expect(card.getByTestId('history-period-30d')).toHaveAttribute('aria-pressed', 'true')
  await expect(chart).toBeVisible()
})

test('missing or unknown account providers cannot crash the page', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  let legacy = true
  await page.route('**/api/accounts', async (route) => {
    if (route.request().method() !== 'GET') return route.continue()
    const response = await route.fetch()
    const accounts = await response.json()
    for (const account of accounts) {
      if (legacy) delete account.provider
      else account.provider = 'unavailable-provider'
    }
    await route.fulfill({ response, json: accounts })
  })
  await page.goto('/')
  await page.getByTestId('login-password').fill('test-admin-password')
  await page.getByTestId('login-submit').click()
  await page.getByTestId('connect-account').click()
  const dialog = page.getByTestId('account-dialog')
  await dialog.getByTestId('connection-label').fill('Legacy')
  await dialog.getByTestId('connection-email').fill('legacy@example.com')
  await dialog.getByTestId('connection-password').fill('vesync-test-password')
  await dialog.getByTestId('connection-submit').click()
  await expect(dialog).not.toBeVisible()
  await page.getByTestId('nav-accounts').click()
  const row = accountCard(page, 'Legacy')
  await expect(row).toContainText('Unsupported provider')
  await row.getByTestId('account-actions').click()
  await expect(page.getByTestId('account-reconnect')).toBeDisabled()
  legacy = false
  await page.goto('/accounts')
  await expect(row).toContainText('Unsupported provider')
  await row.getByTestId('account-actions').click()
  await expect(page.getByTestId('account-reconnect')).toBeDisabled()
  expect(errors).toEqual([])
})

test('card power and unified fan controls confirm changes and surface failures', async ({ page }) => {
  test.setTimeout(60000)
  await page.goto('/')
  await page.getByTestId('login-password').fill('test-admin-password')
  await page.getByTestId('login-submit').click()
  await expect(page.getByTestId('devices-title')).toHaveText('Your devices')
  const session = await (await page.request.get('/api/session')).json()
  const connection = await page.request.post('/api/accounts', {
    headers: { 'X-CSRF-Token': session.csrf_token },
    data: { provider: 'vesync', label: 'Controls', email: 'controls@example.com', password: 'test-password', country_code: 'US', time_zone: 'UTC' },
  })
  expect(connection.status()).toBe(201)
  const account = await connection.json()
  await page.reload()
  const card = deviceCard(page, 'Controls purifier')
  const power = card.getByTestId('device-power')
  const fan = card.getByTestId('device-fan')
  await expect(fan.getByTestId('device-fan-option')).toHaveText(['Auto', 'Sleep', 'Level 1', 'Level 2', 'Level 3', 'Level 4'])
  await expect(power).toHaveAttribute('aria-checked', 'true')
  await expect(fan).toBeEnabled({ timeout: 8000 })
  let releaseFan!: () => void
  const fanResponse = new Promise<void>((resolve) => { releaseFan = resolve })
  await page.route('**/api/devices/*/control', async (route) => {
    await fanResponse; await route.continue()
  }, { times: 1 })
  await fan.selectOption('sleep')
  await expect(fan).toHaveValue('sleep')
  await expect(fan).toBeDisabled()
  releaseFan()
  await expect(fan).toBeEnabled({ timeout: 8000 })
  await fan.selectOption('fan-4')
  await expect(fan).toHaveValue('fan-4')
  await expect(power).toBeEnabled({ timeout: 8000 })
  let releasePower!: () => void
  const powerResponse = new Promise<void>((resolve) => { releasePower = resolve })
  await page.route('**/api/devices/*/control', async (route) => {
    await powerResponse; await route.continue()
  }, { times: 1 })
  await power.click()
  await expect(power).toHaveAttribute('aria-checked', 'false')
  await expect(fan).toBeDisabled()
  releasePower()
  await expect(power).toBeEnabled({ timeout: 8000 })
  await power.click()
  await expect(power).toHaveAttribute('aria-checked', 'true')
  await expect(fan).toBeEnabled({ timeout: 8000 })
  await page.route('**/api/devices/*/control', (route) => route.fulfill({ status: 502,
    json: { detail: 'The device did not accept the change.' } }))
  await fan.selectOption('auto')
  await expect(card.getByTestId('device-control-error')).toContainText('The device did not accept the change.')
  await expect(fan).toHaveValue('fan-4')

  await expect(fan).toBeEnabled({ timeout: 8000 })
  await page.unroute('**/api/devices/*/control')
  const saved = (await (await page.request.get('/api/devices')).json()).find((item: { account_id: string }) => item.account_id === account.id)
  let commands = 0, statusReads = 0
  await page.route('**/api/devices/*/control', (route) => {
    commands++
    return route.fulfill({ json: { ...saved, control_confirmed: false,
      control_retry_at: new Date(Date.now() + 5000).toISOString() } })
  })
  await page.route('**/api/devices/*/status', (route) => {
    statusReads++
    return route.fulfill({ json: saved })
  })
  await fan.selectOption('auto')
  await expect(fan).toHaveValue('auto')
  await expect(card.getByTestId('device-controls')).toHaveAttribute('aria-busy', 'false')
  await expect(fan).toBeEnabled({ timeout: 8000 })
  await expect(fan).toHaveValue('auto')
  expect(commands).toBe(1)
  expect(statusReads).toBe(0)
  await expect(card.getByTestId('device-control-error')).toHaveCount(0)

  // Normal polling can confirm the setting without disabling controls again.
  await page.route('**/api/devices', (route) => route.fulfill({ json: [{ ...saved,
    retrieved_at: new Date(Date.now()).toISOString(), control_retry_at: null,
    state: { ...saved.state, mode: 'auto' },
  }] }))
  const freshPoll = page.waitForResponse('**/api/devices')
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  await freshPoll
  await expect(fan).toHaveValue('auto')
  await expect(fan).toBeEnabled()
  await page.unroute('**/api/devices')

  // An older cached UI poll must not overwrite the newly confirmed state.
  await page.route('**/api/devices', (route) => route.fulfill({ json: [saved] }))
  const oldPoll = page.waitForResponse('**/api/devices')
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  await oldPoll
  await expect(fan).toHaveValue('auto')
  await expect(fan).toBeEnabled()
  await page.request.delete(`/api/accounts/${account.id}`, { headers: { 'X-CSRF-Token': session.csrf_token } })
})

for (const setting of ['power', 'fan'] as const) {
  test(`unconfirmed ${setting} expires despite fresh contradictory polls`, async ({ page }) => {
    const clockStart = new Date('2026-10-10T00:00:00Z')
    await page.clock.install({ time: clockStart })
    await page.clock.pauseAt(new Date(clockStart.getTime() + 1000))
    const observed = {
      id: 'unconfirmed', account_id: 'shared-account', account_label: 'Home', name: 'Unconfirmed purifier',
      model: 'Core 600S', supported: true, capabilities: ['power', 'mode', 'fan_level', 'pm25'],
      controls: { power: true, modes: ['auto', 'sleep'], fan_levels: [1, 2, 3, 4] },
      control_available: true, control_retry_at: null,
      state: { connectivity: 'online', power: 'on', mode: 'manual', fan_level: 2, pm25: 8 },
      retrieved_at: clockStart.toISOString(), stale: false, error: null,
    }
    await page.route('**/api/devices', (route) => route.fulfill({ json: [observed] }))
    await page.route('**/api/devices/unconfirmed/control', (route) => route.fulfill({
      json: { ...observed, control_confirmed: false },
    }))
    await page.goto('/')
    await page.getByTestId('login-password').fill('test-admin-password')
    await page.getByTestId('login-submit').click()
    const card = deviceCard(page, observed.name)
    const power = card.getByTestId('device-power')
    const fan = card.getByTestId('device-fan')
    if (setting === 'power') await power.click()
    else await fan.selectOption('fan-4')
    await expect(card.getByTestId('device-controls')).toHaveAttribute('aria-busy', 'false')
    const expectRequested = async () => {
      if (setting === 'power') await expect(power).toHaveAttribute('aria-checked', 'false')
      else await expect(fan).toHaveValue('fan-4')
    }
    await expectRequested()
    for (let poll = 0; poll < 2; poll++) {
      await page.clock.fastForward(30000)
      observed.retrieved_at = await page.evaluate(() => new Date().toISOString())
      observed.state.mode = 'sleep'
      const response = page.waitForResponse('**/api/devices')
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
      await response
      await expectRequested()
      await expect(power).toBeEnabled()
    }
    await page.clock.fastForward(29000)
    await expectRequested()
    await page.clock.fastForward(1000)
    await expect(power).toHaveAttribute('aria-checked', 'true')
    await expect(fan).toHaveValue('sleep')
    await expect(fan).toBeEnabled()
  })
}

test('changing one purifier keeps other card controls enabled', async ({ page }) => {
  const base = {
    account_id: 'shared-account', account_label: 'Home', model: 'Core 600S', supported: true,
    capabilities: ['power', 'mode', 'fan_level', 'pm25'],
    controls: { power: true, modes: ['auto', 'sleep'], fan_levels: [1, 2, 3, 4] },
    control_available: true, control_retry_at: null as string | null,
    state: { connectivity: 'online', power: 'on', mode: 'manual', fan_level: 2, pm25: 8 },
    retrieved_at: new Date().toISOString(), stale: false, error: null,
  }
  const devices = [{ ...base, id: 'first', name: 'First purifier' }, { ...base, id: 'second', name: 'Second purifier' }]
  await page.route('**/api/devices', (route) => route.fulfill({ json: devices }))
  let release!: () => void
  const responseGate = new Promise<void>((resolve) => { release = resolve })
  await page.route('**/api/devices/first/control', async (route) => {
    await responseGate
    await route.fulfill({ json: { ...devices[0], state: { ...base.state, mode: 'sleep' },
      retrieved_at: new Date().toISOString(), control_retry_at: new Date(Date.now() + 5000).toISOString(),
      control_confirmed: true } })
  })
  await page.goto('/')
  await page.getByTestId('login-password').fill('test-admin-password')
  await page.getByTestId('login-submit').click()
  const first = deviceCard(page, 'First purifier').getByTestId('device-fan')
  const second = deviceCard(page, 'Second purifier').getByTestId('device-fan')
  const secondPower = deviceCard(page, 'Second purifier').getByTestId('device-power')
  await first.selectOption('sleep')
  await expect(first).toBeDisabled()
  await expect(second).toBeEnabled()
  await expect(secondPower).toBeEnabled()
  // A normal UI poll exposes the shared backend deadline on every device.
  devices.forEach((device) => { device.control_retry_at = new Date(Date.now() + 5000).toISOString() })
  const poll = page.waitForResponse('**/api/devices')
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  await poll
  await expect(second).toBeEnabled()
  release()
  await expect(deviceCard(page, 'First purifier').getByTestId('device-controls')).toHaveAttribute('aria-busy', 'false')
  await expect(first).toBeDisabled()
  await expect(second).toBeEnabled()
  await expect(secondPower).toBeEnabled()
})

test('mobile layout and invalid credentials', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await page.getByTestId('login-password').fill('incorrect')
  await page.getByTestId('login-submit').click()
  await expect(page.getByTestId('form-error')).toContainText('Incorrect username or password')
  await page.getByTestId('login-password').fill('test-admin-password')
  await page.getByTestId('login-submit').click()
  await expect(page.getByTestId('devices-title')).toHaveText('Your devices')
  await page.getByTestId('user-menu-trigger').click()
  await expect(page.getByTestId('user-identity')).toHaveText('admin')
  await expect(page.getByTestId('sign-out')).toBeVisible()
  await page.getByTestId('nav-accounts').click()
  await expect(page.getByTestId('user-menu')).toHaveCount(0)
  await page.getByTestId('account-actions').first().click()
  await expect(page.getByTestId('account-menu')).toBeVisible()
  await page.getByTestId('account-remove').press('Escape')
  await page.getByTestId('connect-account').click()
  await expect(page.getByTestId('connection-country')).toBeVisible()
  await expect(page.getByTestId('connection-time-zone')).toBeVisible()
  const width = await page.evaluate(() => document.documentElement.scrollWidth)
  expect(width).toBeLessThanOrEqual(390)
})
