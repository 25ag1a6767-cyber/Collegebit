const { test, expect } = require('@playwright/test');

test('homepage loads branded app and live catalogue', async ({ page }) => {
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));

  await page.goto('/');
  await expect(page).toHaveTitle(/ACE Canteen/);
  await expect(page.getByRole('heading', { name: /Good food/i })).toBeVisible();
  await expect(page.locator('#catalogue-grid .product-card').first()).toBeVisible();
  await expect(page.locator('#catalogue-message')).toContainText(/items loaded/);
  await expect(page.locator('#pickup-queue-heading')).toBeVisible();
  expect(pageErrors).toEqual([]);
});

test('search and item-type filter update catalogue', async ({ page }) => {
  await page.goto('/');
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

test('guest is directed to sign in before adding an item to cart', async ({ page }) => {
  await page.goto('/');
  const firstCard = page.locator('#catalogue-grid .product-card').first();
  await expect(firstCard).toBeVisible();
  await firstCard.getByRole('button', { name: 'Add to cart' }).click();
  await expect(page.locator('#auth-message')).toContainText(/sign in as a student/i);
  await expect(page.locator('#cart-items')).toBeEmpty();
  await expect(page.locator('#cart-subtotal')).toHaveText('₹0.00');
});

test('account form exposes accessible sign-in and registration modes', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('region', { name: 'Account sign in' })).toBeVisible();
  await expect(page.getByLabel('Email address')).toBeVisible();
  await expect(page.locator('#auth-password')).toBeVisible();
  await page.getByRole('button', { name: 'Create account' }).click();
  await expect(page.locator('#auth-name')).toBeVisible();
  await expect(page.locator('#auth-submit')).toHaveText(/Create student account/i);
});

test('staff portal reports backend availability and network interruption', async ({ page }) => {
  await page.goto('/staff');
  await expect(page.locator('#staff-ws-label')).toHaveText('Service online');
  await page.context().setOffline(true);
  await expect(page.locator('#staff-ws-label')).toHaveText('Network disconnected');
  await page.context().setOffline(false);
  await expect(page.locator('#staff-ws-label')).toHaveText('Service online');
});

test('staff sign-in explains invalid credentials and account authorization', async ({ page }) => {
  let loginError = {
    status: 401,
    body: { success: false, error: { code: 'INVALID_CREDENTIALS', message: 'Invalid credentials.' } },
  };
  await page.route('**/api/v1/auth/staff/login', route => route.fulfill({
    status: loginError.status,
    contentType: 'application/json',
    body: JSON.stringify(loginError.body),
  }));
  await page.goto('/staff');
  await page.locator('#staff-login-id').fill('staff@example.edu');
  await page.locator('#staff-login-pw').fill('incorrect-password');
  await page.locator('#staff-login-form button[type="submit"]').click();
  await expect(page.locator('#staff-banner')).toContainText('Invalid email or password.');

  loginError = {
    status: 403,
    body: { success: false, error: { code: 'STAFF_PORTAL_FORBIDDEN', message: 'Not authorized.' } },
  };
  await page.locator('#staff-login-form button[type="submit"]').click();
  await expect(page.locator('#staff-banner')).toContainText('This account is not authorized for the staff portal.');
});

test('staff session refreshes on expiry and logout clears the browser session', async ({ page }) => {
  let meRequests = 0;
  let refreshRequests = 0;
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/v1/auth/staff/login') {
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ success: true, data: { accessToken: 'access-1', refreshToken: 'refresh-1' } }),
      });
    }
    if (url.pathname === '/api/v1/me') {
      meRequests += 1;
      if (meRequests === 1) {
        return route.fulfill({
          status: 401,
          contentType: 'application/json',
          body: JSON.stringify({ success: false, error: { code: 'UNAUTHORIZED', message: 'Expired.' } }),
        });
      }
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          data: { role: 'staff', fullName: 'Kitchen Staff', mustChangePassword: false, staffPermissions: {}, assignedCounterIds: [] },
        }),
      });
    }
    if (url.pathname === '/api/v1/auth/refresh') {
      refreshRequests += 1;
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ success: true, data: { accessToken: 'access-2', refreshToken: 'refresh-2' } }),
      });
    }
    if (url.pathname === '/api/v1/counters') {
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ success: true, data: { items: [] } }) });
    }
    if (url.pathname === '/api/v1/staff/orders') {
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ success: true, data: { summary: {}, items: [] } }) });
    }
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ success: true, data: {} }) });
  });

  await page.goto('/staff');
  await page.locator('#staff-login-id').fill('staff@example.edu');
  await page.locator('#staff-login-pw').fill('valid-password');
  await page.locator('#staff-login-form button[type="submit"]').click();
  await expect(page.locator('#staff-workspace')).toBeVisible();
  expect(refreshRequests).toBe(1);
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('cb_staff_refresh_token'))).toBe('refresh-2');

  await page.locator('#staff-logout-btn').click();
  await expect(page.locator('#staff-login-section')).toBeVisible();
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('cb_staff_refresh_token'))).toBeNull();
});

test('staff MFA challenge and temporary-password change both complete in the portal', async ({ page }) => {
  let meRequests = 0;
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/v1/auth/staff/login') {
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ success: true, data: { mfaRequired: true, mfaChallengeToken: 'challenge-token-for-e2e' } }),
      });
    }
    if (url.pathname === '/api/v1/auth/mfa/verify-login') {
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ success: true, data: { accessToken: 'access-1', refreshToken: 'refresh-1' } }),
      });
    }
    if (url.pathname === '/api/v1/me') {
      meRequests += 1;
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          success: true,
          data: { role: 'staff', fullName: 'Kitchen Staff', mustChangePassword: meRequests === 1, staffPermissions: {}, assignedCounterIds: [] },
        }),
      });
    }
    if (url.pathname === '/api/v1/users/me/change-password') {
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ success: true, data: { accessToken: 'access-2', refreshToken: 'refresh-2' } }),
      });
    }
    if (url.pathname === '/api/v1/counters') {
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ success: true, data: { items: [] } }) });
    }
    if (url.pathname === '/api/v1/staff/orders') {
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ success: true, data: { summary: {}, items: [] } }) });
    }
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ success: true, data: {} }) });
  });

  await page.goto('/staff');
  await page.locator('#staff-login-id').fill('staff@example.edu');
  await page.locator('#staff-login-pw').fill('temporary-password');
  await page.locator('#staff-login-form button[type="submit"]').click();
  await expect(page.locator('#staff-mfa-section')).toBeVisible();
  await page.locator('#staff-mfa-code').fill('123456');
  await page.locator('#staff-mfa-form button[type="submit"]').click();
  await expect(page.locator('#staff-change-password-section')).toBeVisible();
  await page.locator('#staff-current-password').fill('temporary-password');
  await page.locator('#staff-new-password').fill('new-staff-password-123');
  await page.locator('#staff-change-password-form button[type="submit"]').click();
  await expect(page.locator('#staff-workspace')).toBeVisible();
});

test('staff password reset form returns an account-neutral response', async ({ page }) => {
  await page.route('**/api/auth/forgot-password', route => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ message: 'If an account exists for that email, a reset link has been sent.' }),
  }));
  await page.goto('/staff');
  await page.locator('#staff-forgot-btn').click();
  await expect(page.locator('#staff-reset-form')).toBeVisible();
  await page.locator('#staff-reset-email').fill('staff@example.edu');
  await page.locator('#staff-reset-form button[type="submit"]').click();
  await expect(page.locator('#staff-reset-form')).toBeHidden();
  await expect(page.locator('#staff-banner')).toContainText('If an account exists for that email');
});

test('staff portal reports a backend network failure', async ({ page }) => {
  await page.route('**/api/ready', route => route.abort('failed'));
  await page.goto('/staff');
  await expect(page.locator('#staff-ws-label')).toHaveText('Backend unavailable');
});
