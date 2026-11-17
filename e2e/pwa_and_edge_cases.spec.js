const { test, expect } = require('@playwright/test');

test.describe('Customer PWA feel, install prompts, offline recovery & WS reconnect', () => {
  test('mobile viewport renders bottom nav, 16px inputs, and smart iOS Safari install banner', async ({ browser }) => {
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1',
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', err => errors.push(err.message));

    await page.goto('/customer/');
    await expect(page.locator('#mobile-bottom-nav')).toBeVisible();
    await expect(page.locator('#pwa-install-banner')).toBeVisible();
    await expect(page.locator('#pwa-banner-subtitle')).toContainText(/Add to Home Screen/i);

    const inputFontSize = await page.locator('#auth-email').evaluate(el => parseFloat(getComputedStyle(el).fontSize));
    expect(inputFontSize).toBeGreaterThanOrEqual(16);

    // Dismiss persists across reload via localStorage
    await page.locator('#pwa-banner-dismiss-btn').click();
    await expect(page.locator('#pwa-install-banner')).toBeHidden();
    await page.reload();
    await expect(page.locator('#pwa-install-banner')).toBeHidden();
    expect(errors).toEqual([]);
    await context.close();
  });

  test('standalone launched PWA hides install prompts completely', async ({ browser }) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
    await context.addInitScript(() => {
      Object.defineProperty(window.navigator, 'standalone', { get: () => true });
    });
    const page = await context.newPage();
    await page.goto('/customer/');
    await expect(page.locator('#pwa-install-banner')).toBeHidden();
    await expect(page.locator('#install-app-button')).toBeHidden();
    await context.close();
  });

  test('desktop browser without beforeinstallprompt does not force install banner and handles offline/online WS reconnect', async ({ page, context }) => {
    const pageErrors = [];
    page.on('pageerror', err => pageErrors.push(err.message));

    await page.goto('/customer/');
    await expect(page.locator('#pwa-install-banner')).toBeHidden();
    await expect(page.locator('#v1-ws-label')).toHaveText(/Live updates|Connecting/i);
    await expect.poll(() => page.locator('#v1-ws-label').textContent(), { timeout: 10000 }).toBe('Live updates');

    // Simulate network drop and recovery cleanly
    await context.setOffline(true);
    await page.evaluate(() => window.dispatchEvent(new Event('offline')));
    await expect(page.locator('#v1-ws-label')).toHaveText(/Offline/i);

    await context.setOffline(false);
    await page.evaluate(() => window.dispatchEvent(new Event('online')));
    await expect.poll(() => page.locator('#v1-ws-label').textContent(), { timeout: 12000 }).toBe('Live updates');
    expect(pageErrors).toEqual([]);
  });

  test('offline fallback page renders cleanly and iOS Safari explains Home Screen prerequisite for push notifications', async ({ browser }) => {
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1',
    });
    const page = await context.newPage();
    await page.goto('/offline.html');
    await expect(page.locator('h1')).toContainText(/You are offline/i);
    await expect(page.locator('a.primary-button')).toHaveAttribute('href', '/customer/');

    // Load /customer/, tap Enable push alerts on non-standalone iOS -> shows Home Screen guidance without requesting OS permission
    await page.goto('/customer/');
    await page.evaluate(() => {
      document.querySelector('#account-panel')?.removeAttribute('hidden');
    });
    await page.locator('#v1-push-toggle-btn').click();
    await expect(page.locator('#v1-profile-msg')).toContainText(/Add to Home Screen/i);
    await context.close();
  });
});
