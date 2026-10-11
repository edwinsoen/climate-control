import { expect, test } from '@playwright/test'

const device = {
  id: 'setting-history', account_id: 'history-account', account_label: 'Home', name: 'History purifier',
  model: 'Core 400S', supported: true, capabilities: ['power', 'mode', 'fan_level', 'pm25'],
  controls: { power: true, modes: ['auto', 'sleep'], fan_levels: [1, 2, 3, 4] },
  control_available: true, control_retry_at: null,
  state: { connectivity: 'online', power: 'on', mode: 'manual', fan_level: 2, pm25: 8 },
  retrieved_at: new Date().toISOString(), stale: false, error: null,
}

for (const mobile of [false, true]) {
  test.describe(mobile ? 'mobile' : 'desktop', () => {
    test.use({ hasTouch: mobile })
    test(`purifier settings overlay air quality and preserve unknown periods${mobile ? ' on mobile' : ''}`, async ({ page }, testInfo) => {
      if (mobile) await page.setViewportSize({ width: 390, height: 844 })
      const end = Date.now() / 1000
      const start = end - 86400
      const at = (fraction: number) => start + fraction * 86400
      const sample = (fraction: number, value: number) => ({ recorded_at: at(fraction), value, min: value, max: value, count: 1 })
      await page.route('**/api/devices', (route) => route.fulfill({ json: [device] }))
      await page.route('**/api/devices/setting-history/history?hours=*', (route) => route.fulfill({ json: {
        source: 'recorded', start, end, interval_seconds: 240,
        metrics: { pm25: [sample(0.2, 30), sample(0.4, 20), sample(0.8, 5)] },
        settings: [
          { recorded_at: at(0.1), end_at: at(0.3), power: 'on', mode: 'manual', fan_level: 4, reported_fan_level: 2 },
          { recorded_at: at(0.3), end_at: at(0.5), power: 'on', mode: 'auto', fan_level: 3, reported_fan_level: 3 },
          { recorded_at: at(0.7), end_at: at(0.9), power: 'off', mode: 'manual', fan_level: 4, reported_fan_level: 0 },
          { recorded_at: at(0.9), end_at: end, power: 'on', mode: 'sleep', fan_level: 1, reported_fan_level: 1 },
        ],
      } }))
      await page.goto('/')
      await page.getByTestId('login-password').fill('test-admin-password')
      await page.getByTestId('login-submit').click()
      const card = page.getByTestId('device-card')
      await card.getByTestId('history-toggle').click()
      const chart = card.getByTestId('history-chart')
      await expect(chart).toBeVisible()
      await expect(card.getByTestId('history-settings-legend')).toContainText('ManualAutoOffSleep')
      await expect(card.getByTestId('history-settings-legend')).toContainText('Fan speed')
      await expect(chart.getByTestId('history-setting')).toHaveCount(4)
      await expect(chart.getByTestId('history-setting').first()).toHaveAttribute('aria-label', 'On · Manual · Fan 4')
      await expect(chart.getByTestId('history-setting').nth(1)).toHaveClass(/setting-auto/)
      await expect(chart.getByTestId('history-setting').nth(2)).toHaveClass(/setting-off/)
      await expect(card.getByTestId('history-setting-reading')).toHaveCount(0)
      const bounds = (await chart.boundingBox())!
      // The chart's time axis has 44px and 40px margins, independent of screen width.
      const position = (fraction: number) => ({ x: 44 + fraction * (bounds.width - 84), y: bounds.height / 2 })
      await chart.hover({ position: position(0.2) })
      await expect(card.getByTestId('history-reading')).toContainText('30 µg/m³')
      await expect(card.getByTestId('history-setting-reading')).toContainText('On · Manual · Fan 4')
      await expect(card.getByTestId('history-setting-reading')).toContainText('Running fan 2')
      await chart.screenshot({ path: testInfo.outputPath('setting-history.png') })
      await chart.hover({ position: position(0.4) })
      await expect(card.getByTestId('history-setting-reading')).toContainText('On · Auto · Fan 3')
      await chart.hover({ position: position(0.6) })
      await expect(card.getByTestId('history-setting-reading')).toContainText('Setting unavailable')
      await chart.hover({ position: position(0.8) })
      await expect(card.getByTestId('history-setting-reading')).toContainText('Off')
      if (mobile) {
        await chart.tap({ position: position(0.4) })
        await expect(card.getByTestId('history-reading')).toContainText('20 µg/m³')
        await expect(card.getByTestId('history-setting-reading')).toContainText('On · Auto · Fan 3')
      } else {
        await card.getByTestId('device-name').hover()
        await expect(card.getByTestId('history-reading')).toHaveCount(0)
        await expect(card.getByTestId('history-setting-reading')).toHaveCount(0)
      }
      await card.getByTestId('history-period-7d').click()
      await expect(card.getByTestId('history-period-7d')).toHaveAttribute('aria-pressed', 'true')
      await expect(chart.getByTestId('history-setting')).toHaveCount(4)
      if (mobile) expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
    })
  })
}

test('purifier without PM2.5 still shows recorded settings', async ({ page }) => {
  const end = Date.now() / 1000
  await page.route('**/api/devices', (route) => route.fulfill({ json: [{ ...device, capabilities: ['power', 'mode', 'fan_level'] }] }))
  await page.route('**/api/devices/setting-history/history?hours=*', (route) => route.fulfill({ json: {
    source: 'recorded', start: end - 86400, end, interval_seconds: 240,
    metrics: { pm25: [] },
    settings: [{ recorded_at: end - 3600, end_at: end, power: 'off', mode: 'auto', fan_level: 2, reported_fan_level: 0 }],
  } }))
  await page.goto('/')
  await page.getByTestId('login-password').fill('test-admin-password')
  await page.getByTestId('login-submit').click()
  await page.getByTestId('history-toggle').first().click()
  const card = page.getByTestId('device-card')
  await expect(card.getByTestId('history-chart')).toBeVisible()
  await expect(card.getByTestId('history-setting')).toHaveAttribute('aria-label', 'Off')
})
