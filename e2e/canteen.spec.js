const { test, expect } = require('@playwright/test');

test('homepage loads branded app and live catalogue', async ({ page }) => {
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));

  await page.goto('/customer/');
  await expect(page).toHaveTitle(/ACE Canteen/);
  await expect(page.getByRole('heading', { name: /Good food/i })).toBeVisible();
  await expect(page.locator('#catalogue-grid .product-card').first()).toBeVisible();
  await expect(page.locator('#catalogue-message')).toContainText(/items loaded/);
  await expect(page.locator('#pickup-queue-heading')).toBeVisible();
  expect(pageErrors).toEqual([]);
});

test('search and item-type filter update catalogue', async ({ page }) => {
  await page.goto('/customer/');
  await expect(page.locator('#catalogue-grid .product-card').first()).toBeVisible();

  await page.locator('#product-type').selectOption('stationery');
  await expect(page.locator('#catalogue-grid .product-card').first()).toBeVisible();
  const stationeryCards = page.locator('#catalogue-grid .product-card');
  await expect(stationeryCards.first()).toBeVisible();
  await expect(stationeryCards.first()).toContainText(/stationery|pen|notebook|pencil|paper|book/i);

  await page.locator('#product-search').fill('this-item-should-not-exist-ace-e2e');
  await expect(page.locator('#catalogue-message')).toContainText(/No matching/i);
  await expect(page.locator('#catalogue-grid .product-card')).toHaveCount(0);
});

test('guest is prompted to sign in before adding an item to cart', async ({ page }) => {
  await page.goto('/customer/');
  const firstCard = page.locator('#catalogue-grid .product-card').first();
  await expect(firstCard).toBeVisible();
  await firstCard.getByRole('button', { name: 'Add to cart' }).click();
  await expect(page.locator('#auth-message')).toContainText(/sign in as a student/i);
  await expect(page.locator('#cart-items')).toBeEmpty();
  await expect(page.locator('#cart-subtotal')).toHaveText('₹0.00');
});

test('account form exposes accessible sign-in and registration modes', async ({ page }) => {
  await page.goto('/customer/');
  await expect(page.locator('#v1-guest-otp-box')).toHaveCount(0);
  await expect(page.locator('#rewards-wallet-panel')).toHaveCount(0);
  await expect(page.locator('#pwa-banner-ios-btn')).toHaveCount(0);
  await expect(page.locator('#pwa-banner-apk-btn')).toHaveCount(0);
  const account = page.getByRole('region', { name: 'Account sign in' });
  await expect(account.getByLabel('Email address')).toBeVisible();
  await expect(account.getByLabel('Password', { exact: true })).toBeVisible();
  await account.getByRole('button', { name: 'Create account' }).click();
  await expect(account.getByLabel('Full name')).toBeVisible();
  await expect(page.locator('#auth-submit')).toHaveText(/Create student account/i);

  const uniqueEmail = `e2e-reg-${Date.now()}@college.edu`;
  const uniquePass = 'StudentPass!2026';
  await account.getByLabel('Full name').fill('E2E Registered Student');
  await account.getByLabel('Email address').fill(uniqueEmail);
  await account.getByLabel('Password', { exact: true }).fill(uniquePass);
  await page.locator('#auth-submit').click();

  await expect(page.locator('#auth-message')).toContainText('Signed in successfully');
  await expect(page.locator('#account-name')).toHaveText('E2E Registered Student');
  await page.locator('#auth-logout').click();
  await expect(page.locator('#auth-message')).toContainText('You have signed out');
  await account.getByLabel('Email address').fill(uniqueEmail);
  await account.getByLabel('Password', { exact: true }).fill(uniquePass);
  await page.locator('#auth-submit').click();
  await expect(page.locator('#auth-message')).toContainText('Signed in successfully');
});

test('staff and admin website is separate from the customer app', async ({ page }) => {
  await page.goto('/');
  await expect(page).toHaveTitle(/Staff & Administration/);
  await expect(page.getByRole('link', { name: /Staff Portal/ })).toHaveAttribute('href', '/staff');
  await expect(page.getByRole('link', { name: /Administration Portal/ })).toHaveAttribute('href', '/admin');
  await expect(page.locator('#catalogue-grid')).toHaveCount(0);
});
