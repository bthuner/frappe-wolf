import { defineConfig, devices } from '@playwright/test'

// Browser tests of the production build (e2e/server.mjs) with the Wolf API mocked in the page
// (e2e/api.js). `yarn build` first. WOLF_CHROMIUM points at a preinstalled Chromium; CI installs
// Playwright's own with `npx playwright install --with-deps chromium`.
const executablePath = process.env.WOLF_CHROMIUM || undefined

export default defineConfig({
  testDir: 'e2e',
  timeout: 30000,
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : [['list']],
  use: { baseURL: 'http://127.0.0.1:4173', trace: 'retain-on-failure' },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'], launchOptions: executablePath ? { executablePath } : {} } },
  ],
  webServer: { command: 'node e2e/server.mjs', url: 'http://127.0.0.1:4173/wolf/', reuseExistingServer: !process.env.CI },
})
