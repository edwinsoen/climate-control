import { expect, test } from '@playwright/test'

test.beforeEach(async ({ page }, testInfo) => {
  await page.setExtraHTTPHeaders({ 'X-Test-Client': testInfo.testId })
  await page.route('**/api/devices', (route) => route.fulfill({ json: [] }))
  await page.route('**/api/accounts', (route) => route.fulfill({ json: [] }))
})

test('set and change location in the app, inspect outdoor history, then remove it', async ({ page, context }) => {
  test.setTimeout(90000)
  await context.grantPermissions(['geolocation'])
  await context.setGeolocation({ latitude: 1.35, longitude: 103.8 })
  await page.goto('/')
  await page.getByTestId('login-password').fill('test-admin-password')
  await page.getByTestId('login-submit').click()
  await expect(page.getByTestId('outdoor-no-location')).toContainText('Set your location')
  await page.getByTestId('outdoor-set-location').click()
  await page.getByTestId('outdoor-city-query').fill('Singapore')
  await page.getByTestId('outdoor-search-stations').click()
  await expect(page.getByTestId('outdoor-station-option').first()).toContainText('AQI 42')
  await page.getByTestId('outdoor-station-1').check()
  await page.getByTestId('outdoor-save-location').click()
  const card = page.getByTestId('outdoor-card')
  await expect(card.getByTestId('outdoor-station')).toHaveText('Outdoor test station')
  await expect(card.getByTestId('outdoor-pm25')).toHaveText('42 AQI', { timeout: 25000 })
  await expect(card.getByTestId('outdoor-pm10')).toHaveText('20 AQI')
  await expect(card.getByTestId('outdoor-pm25-age')).toHaveText('1 hr ago')
  await expect(card.getByTestId('outdoor-pm10-age')).toHaveClass(/outdoor-stale/)
  await expect(page.getByTestId('empty-devices')).toBeVisible()
  await expect(card.getByRole('link', { name: 'AQICN / WAQI', exact: true })).toHaveAttribute('href', 'https://aqicn.org')
  await expect(card.getByTestId('history-chart')).not.toBeVisible()
  await expect(card.getByTestId('outdoor-history-metric')).toHaveCount(0)
  await card.getByTestId('history-toggle').click()
  await expect(card.getByTestId('history-chart')).toBeVisible()
  await card.getByTestId('history-controls').getByTestId('outdoor-history-metric').selectOption('pm10')
  await expect(card.getByTestId('history-title')).toHaveText('PM10 AQI history')
  await expect(card.getByTestId('history-chart')).toHaveAttribute('aria-label', /PM10 AQI history/)
  const response = page.waitForResponse('**/api/outdoor/history?hours=720')
  await card.getByTestId('history-period-30d').click()
  await response
  await expect(card.getByTestId('history-period-30d')).toHaveAttribute('aria-pressed', 'true')
  await expect(card.getByTestId('history-chart')).toBeVisible()
  await card.getByTestId('history-toggle').click()
  await expect(card.getByTestId('history-chart')).not.toBeVisible()
  await expect(card.getByTestId('outdoor-history-metric')).toHaveCount(0)
  await card.getByTestId('history-toggle').click()
  await expect(card.getByTestId('outdoor-history-metric')).toHaveValue('pm10')
  await expect(card.getByTestId('history-period-30d')).toHaveAttribute('aria-pressed', 'true')
  await card.getByTestId('history-toggle').click()
  await page.reload()
  await expect(card.getByTestId('outdoor-station')).toHaveText('Outdoor test station')
  await expect(page.getByTestId('outdoor-set-location')).toHaveCount(0)
  await card.getByTestId('outdoor-actions').click()
  await card.getByTestId('outdoor-change-location').click()
  await expect(card).toHaveCount(0)
  await expect(page.getByTestId('outdoor-location-form')).toBeVisible()
  await expect(page.getByTestId('outdoor-city-query')).toBeFocused()
  await page.getByTestId('outdoor-cancel-location').click()
  await expect(card.getByTestId('outdoor-station')).toHaveText('Outdoor test station')
  await card.getByTestId('outdoor-actions').click()
  await card.getByTestId('outdoor-change-location').click()
  await page.getByTestId('outdoor-use-location').click()
  await expect(page.getByTestId('outdoor-station-1')).toBeVisible()
  await page.getByTestId('outdoor-city-query').fill('Singapore')
  await page.getByTestId('outdoor-search-stations').click()
  await page.getByTestId('outdoor-station-2').check({ timeout: 35000 })
  let finishSave!: () => void
  const savePending = new Promise<void>((resolve) => { finishSave = resolve })
  await page.route('**/api/outdoor/location', async (route) => {
    await savePending
    await route.continue()
  }, { times: 1 })
  await page.getByTestId('outdoor-save-location').click()
  await expect(page.getByTestId('outdoor-location-loading')).toContainText('Saving location')
  await expect(card).toHaveCount(0)
  finishSave()
  await expect(card.getByTestId('outdoor-station')).toHaveText('Other test station')
  await page.reload()
  await expect(card.getByTestId('outdoor-station')).toHaveText('Other test station')
  const actions = card.getByTestId('outdoor-actions')
  await expect(card.getByTestId('outdoor-remove-location')).toHaveCount(0)
  await actions.focus()
  await actions.press('ArrowDown')
  await expect(card.getByTestId('outdoor-change-location')).toBeFocused()
  await page.keyboard.press('ArrowDown')
  await expect(card.getByTestId('outdoor-remove-location')).toBeFocused()
  await page.keyboard.press('ArrowDown')
  await expect(card.getByTestId('outdoor-change-location')).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(card.getByTestId('outdoor-menu')).toHaveCount(0)
  await expect(actions).toBeFocused()
  await actions.click()
  await page.getByTestId('outdoor-station').click()
  await expect(card.getByTestId('outdoor-menu')).toHaveCount(0)
  await actions.click()
  await page.getByTestId('outdoor-remove-location').click()
  await expect(page.getByTestId('outdoor-no-location')).toContainText('Set your location')
  await expect(page.getByTestId('outdoor-card')).toHaveCount(0)
})

test('outdoor failures keep cached values and show missing measurements', async ({ page }) => {
  await page.route('**/api/outdoor', (route) => route.fulfill({ json: {
    available: true, configured: true, source: 'aqicn', location_id: 1, coordinates: null,
    station: { name: 'Station', url: 'https://aqicn.org/city/test/',
      attribution: [{ name: 'Agency', url: 'https://example.com' }] },
    capabilities: ['pm25'], metrics: {
      pm25: { value: 42, unit: 'AQI', measured_at: new Date(Date.now() - 5 * 3600000).toISOString(), stale: true },
    }, last_success_at: new Date(Date.now() - 3600000).toISOString(),
    error: 'Outdoor data could not be updated. Updates will retry automatically.',
    backfill: { status: 'unsupported', error: null },
  } }))
  await page.goto('/')
  await page.getByTestId('login-password').fill('test-admin-password')
  await page.getByTestId('login-submit').click()
  const card = page.getByTestId('outdoor-card')
  await expect(card.getByTestId('outdoor-pm25')).toHaveText('42 AQI')
  await expect(card.getByTestId('outdoor-pm25-age')).toContainText('Stale')
  await expect(card.getByTestId('outdoor-pm10')).toHaveText('Unavailable')
  await expect(card).toContainText('Outdoor data could not be updated.')
  await expect(card.getByTestId('outdoor-history-metric')).toHaveCount(0)
})

test('location access denial allows city search and empty search results', async ({ page }) => {
  await page.addInitScript(() => {
    navigator.geolocation.getCurrentPosition = (_success, failure) => failure?.({
      code: 1, message: 'Denied', PERMISSION_DENIED: 1, POSITION_UNAVAILABLE: 2, TIMEOUT: 3,
    })
  })
  await page.route('**/api/outdoor/stations/search', (route) => route.fulfill({ json: { stations: [] } }))
  await page.goto('/')
  await page.getByTestId('login-password').fill('test-admin-password')
  await page.getByTestId('login-submit').click()
  await page.getByTestId('outdoor-set-location').click()
  await page.getByTestId('outdoor-use-location').click()
  await expect(page.getByTestId('outdoor-location-error')).toContainText('Location access is blocked')
  await page.getByTestId('outdoor-city-query').fill('Unknown')
  await page.getByTestId('outdoor-search-stations').click()
  await expect(page.getByTestId('outdoor-no-stations')).toContainText('No reporting stations found')
  await expect(page.getByTestId('outdoor-save-location')).toHaveCount(0)
})

for (const [code, message] of [[2, 'Could not determine your location'], [3, 'Location lookup timed out']] as const) {
  test(`granted location permission reports positioning error ${code} without a station request`, async ({ page, context }) => {
    await context.grantPermissions(['geolocation'])
    await page.addInitScript((code) => {
      navigator.geolocation.getCurrentPosition = (_success, failure) => failure?.({
        code, message: 'Browser positioning failed', PERMISSION_DENIED: 1, POSITION_UNAVAILABLE: 2, TIMEOUT: 3,
      })
    }, code)
    let stationRequests = 0
    await page.route('**/api/outdoor/stations/nearby', (route) => {
      stationRequests++
      return route.fulfill({ json: { stations: [] } })
    })
    await page.goto('/')
    await page.getByTestId('login-password').fill('test-admin-password')
    await page.getByTestId('login-submit').click()
    await page.getByTestId('outdoor-set-location').click()
    await page.getByTestId('outdoor-use-location').click()
    await expect(page.getByTestId('outdoor-location-error')).toContainText(message)
    await expect(page.getByTestId('outdoor-location-loading')).toHaveCount(0)
    await expect(page.getByTestId('outdoor-use-location')).toBeEnabled()
    expect(stationRequests).toBe(0)
  })
}

test('a slow location fix succeeds and nearby API failures remain distinguishable', async ({ page, context }) => {
  await context.grantPermissions(['geolocation'])
  await context.setGeolocation({ latitude: 1.35, longitude: 103.8 })
  await page.clock.install()
  await page.addInitScript(() => {
    const getPosition = navigator.geolocation.getCurrentPosition.bind(navigator.geolocation)
    navigator.geolocation.getCurrentPosition = (success, failure, options) => {
      const timeout = window.setTimeout(() => failure?.({
        code: 3, message: 'Timed out', PERMISSION_DENIED: 1, POSITION_UNAVAILABLE: 2, TIMEOUT: 3,
      }), options?.timeout ?? Infinity)
      window.setTimeout(() => {
        window.clearTimeout(timeout)
        getPosition(success, failure, options)
      }, 12000)
    }
  })
  let nearbyRequests = 0
  await page.route('**/api/outdoor/stations/nearby', async (route) => {
    nearbyRequests++
    expect(route.request().postDataJSON()).toEqual({ latitude: 1.35, longitude: 103.8 })
    if (nearbyRequests === 1) await route.fulfill({ status: 503,
      json: { detail: 'AQICN is limiting requests. Updates will resume automatically.' } })
    else await route.fulfill({ json: { stations: [{ location_id: 123, name: 'Nearby station', aqi: 42 }] } })
  })
  await page.goto('/')
  await page.getByTestId('login-password').fill('test-admin-password')
  await page.getByTestId('login-submit').click()
  await page.getByTestId('outdoor-set-location').click()
  await page.getByTestId('outdoor-use-location').click()
  await expect(page.getByTestId('outdoor-location-loading')).toContainText('Getting your location')
  await page.clock.fastForward(12000)
  await expect(page.getByTestId('outdoor-location-error')).toContainText('AQICN is limiting requests')
  await page.getByTestId('outdoor-use-location').click()
  await page.clock.fastForward(12000)
  await expect(page.getByTestId('outdoor-station-123')).toBeVisible()
  await expect(page.getByTestId('outdoor-location-error')).toHaveCount(0)
  expect(nearbyRequests).toBe(2)
})


test('AQICN search errors can be retried and editing clears old station choices', async ({ page }) => {
  await page.goto('/')
  await page.getByTestId('login-password').fill('test-admin-password')
  await page.getByTestId('login-submit').click()
  await page.getByTestId('outdoor-set-location').click()
  await page.route('**/api/outdoor/stations/search', (route) => route.fulfill({ status: 503,
    json: { detail: 'AQICN is limiting requests. Updates will resume automatically.' } }))
  await page.getByTestId('outdoor-city-query').fill('Singapore')
  await page.getByTestId('outdoor-search-stations').click()
  await expect(page.getByTestId('outdoor-location-error')).toContainText('AQICN is limiting requests')
  await page.unroute('**/api/outdoor/stations/search')
  await page.getByTestId('outdoor-search-stations').click()
  await page.getByTestId('outdoor-station-1').check()
  await expect(page.getByTestId('outdoor-save-location')).toBeEnabled()
  await page.getByTestId('outdoor-city-query').fill('Paris')
  await expect(page.getByTestId('outdoor-station-option')).toHaveCount(0)
  await expect(page.getByTestId('outdoor-save-location')).toHaveCount(0)
})

test('selection shows search AQI immediately and a loader until the full feed arrives', async ({ page }) => {
  const empty = {
    available: true, configured: false, source: 'aqicn', location_id: null,
    coordinates: null, station: null, capabilities: [], metrics: {}, preview: null,
    loading: false, last_success_at: null, error: null,
    backfill: { status: 'unsupported', error: null },
  }
  const selected = {
    ...empty, configured: true, location_id: 123, loading: true,
    station: { name: 'Selected station', url: null, attribution: [] },
    preview: { value: 123, updated_at: '2026-10-11 08:00:00' },
  }
  let current: object = empty
  let finishSearch!: () => void
  const searchPending = new Promise<void>((resolve) => { finishSearch = resolve })
  await page.route('**/api/outdoor', (route) => route.fulfill({ json: current }))
  await page.route('**/api/outdoor/stations/search', async (route) => {
    await searchPending
    await route.fulfill({ json: { stations: [
      { location_id: 123, name: 'Selected station', aqi: 123, updated_at: '2026-10-11 08:00:00' },
    ] } })
  })
  await page.route('**/api/outdoor/location', async (route) => {
    current = selected
    await route.fulfill({ json: selected })
  })
  await page.goto('/')
  await page.getByTestId('login-password').fill('test-admin-password')
  await page.getByTestId('login-submit').click()
  await page.getByTestId('outdoor-set-location').click()
  await page.getByTestId('outdoor-city-query').fill('Singapore')
  await page.getByTestId('outdoor-search-stations').click()
  await expect(page.getByTestId('outdoor-location-loading')).toContainText('Finding stations')
  finishSearch()
  await page.getByTestId('outdoor-station-123').check()
  await page.getByTestId('outdoor-save-location').click()
  const card = page.getByTestId('outdoor-card')
  await expect(card.getByTestId('outdoor-aqi')).toHaveText('123 AQI')
  await expect(card.getByTestId('outdoor-aqi-preview-time')).toContainText('Reported')
  await expect(card.getByTestId('outdoor-pm25')).toHaveText('Loading…')
  await expect(card.getByTestId('outdoor-loading')).toBeVisible()
  await expect(card).toHaveAttribute('aria-busy', 'true')
  current = {
    ...selected, loading: false, preview: null, capabilities: ['aqi', 'pm25'],
    metrics: {
      aqi: { value: 125, unit: 'AQI', measured_at: new Date().toISOString(), stale: false },
      pm25: { value: 125, unit: 'AQI', measured_at: new Date().toISOString(), stale: false },
    }, last_success_at: new Date().toISOString(),
  }
  // Faster cache reads must show the full feed before the normal 15-second refresh.
  await expect(card.getByTestId('outdoor-aqi')).toHaveText('125 AQI', { timeout: 4000 })
  await expect(card.getByTestId('outdoor-loading')).toHaveCount(0)
  await expect(card.getByTestId('outdoor-aqi-preview-time')).toHaveCount(0)
  await expect(card).toHaveAttribute('aria-busy', 'false')
})
