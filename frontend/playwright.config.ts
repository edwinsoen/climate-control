import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  use: { baseURL: 'http://127.0.0.1:8091', trace: 'retain-on-failure' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'], channel: 'chrome' } }],
  webServer: {
    command: 'cd .. && .venv/bin/python tests/e2e_server.py',
    url: 'http://127.0.0.1:8091/health/ready',
    reuseExistingServer: false,
    timeout: 30000,
  },
})
