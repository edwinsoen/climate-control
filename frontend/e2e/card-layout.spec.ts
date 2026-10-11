import { expect, test } from '@playwright/test'

test('outdoor and device cards keep expanded histories usable on desktop and mobile', async ({ page }, testInfo) => {
  await page.setExtraHTTPHeaders({ 'X-Test-Client': testInfo.testId })
  const now = new Date().toISOString()
  await page.route('**/api/accounts', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/devices', (route) => route.fulfill({ json: [{
    id: 'layout', account_id: 'home', account_label: 'Home', name: 'Living room purifier',
    model: 'Core 600S', supported: true, capabilities: ['power', 'mode', 'fan_level', 'pm25', 'air_quality', 'filter_life'],
    controls: { power: true, modes: ['auto', 'sleep'], fan_levels: [1, 2, 3, 4] },
    control_available: true, control_retry_at: null,
    state: { connectivity: 'online', power: 'on', mode: 'auto', pm25: 8, air_quality: 'good', filter_life: 86 },
    retrieved_at: now, stale: false, error: null,
  }] }))
  await page.route('**/api/outdoor', (route) => route.fulfill({ json: {
    available: true, configured: true, source: 'aqicn', location_id: 1, coordinates: null,
    station: { name: 'Central Singapore', url: 'https://aqicn.org/city/singapore/central/', attribution: [
      { name: 'NEA - Singapore National Environment Agency', url: null },
      { name: 'World Air Quality Index Project', url: null },
    ] },
    capabilities: ['aqi', 'pm25', 'pm10'], preview: null, loading: false,
    metrics: Object.fromEntries(['aqi', 'pm25', 'pm10'].map((metric, index) => [metric, {
      value: [86, 86, 24][index], unit: 'AQI', measured_at: now, stale: false,
    }])), last_success_at: now, error: null, backfill: { status: 'unsupported', error: null },
  } }))
  const end = Date.now() / 1000
  await page.route('**/history?hours=*', (route) => route.fulfill({ json: {
    source: 'recorded', start: end - 86400, end, interval_seconds: 240,
    metrics: Object.fromEntries(['aqi', 'pm25', 'pm10'].map((metric) => [metric, [
      { recorded_at: end - 43200, value: 8, min: 8, max: 8, count: 1 },
      { recorded_at: end - 42960, value: 12, min: 12, max: 12, count: 1 },
    ]])),
  } }))
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto('/')
  await page.getByTestId('login-password').fill('test-admin-password')
  await page.getByTestId('login-submit').click()
  const outdoor = page.getByTestId('outdoor-card')
  const device = page.getByTestId('device-card')
  for (const card of [outdoor, device]) {
    await card.getByTestId('history-toggle').click()
    await expect(card.getByTestId('history-chart')).toBeVisible()
  }
  await page.screenshot({ path: 'test-results/cards-desktop.png', fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  for (const card of [outdoor, device]) {
    await expect(card.getByTestId('history-period-30d')).toBeVisible()
    await card.getByTestId('history-period-30d').click()
    await expect(card.getByTestId('history-period-30d')).toHaveAttribute('aria-pressed', 'true')
    const chart = card.getByTestId('history-chart')
    await expect(chart).toBeVisible()
    const bounds = await chart.boundingBox()
    expect(bounds).not.toBeNull()
    await chart.hover({ position: { x: bounds!.width * 0.65, y: bounds!.height / 2 } })
    await expect(chart.getByTestId('history-reading')).toContainText('12')
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  await page.getByTestId('devices-title').hover()
  await page.screenshot({ path: 'test-results/cards-mobile.png', fullPage: true })
})
