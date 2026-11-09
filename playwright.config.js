const { defineConfig, devices } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './e2e',
  globalSetup: './e2e/global-setup.js',
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  timeout: 30_000,
  expect: { timeout: 8_000 },
  use: {
    baseURL: process.env.BASE_URL || 'http://127.0.0.1:8000',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: process.env.BASE_URL ? undefined : {
    command: process.platform === 'win32'
      ? 'py -3 -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000'
      : 'python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000',
    url: 'http://127.0.0.1:8000/api/ready',
    reuseExistingServer: false,
    timeout: 60_000,
    env: {
      PYTHONPATH: 'backend',
      DATABASE_URL: 'sqlite:///./backend/e2e_canteen.db',
      SECRET_KEY: 'local-e2e-only-secret-key-change-this-before-deployment',
      ENVIRONMENT: 'development',
      AUTO_CREATE_TABLES: 'true',
      SEED_DEMO_DATA: 'true',
      RATE_LIMIT_ENABLED: 'false',
      RAZORPAY_KEY_ID: '',
      RAZORPAY_KEY_SECRET: '',
      RAZORPAY_WEBHOOK_SECRET: '',
    },
  },
});
