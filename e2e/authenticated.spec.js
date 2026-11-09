const { test, expect } = require('@playwright/test');
const { randomUUID } = require('node:crypto');

const accounts = {
  student: { email: 'e2e-student@example.com', password: 'E2E-student-password-2026!' },
  staff: { email: 'e2e-staff@example.com', password: 'E2E-staff-password-2026!' },
  admin: { email: 'e2e-admin@example.com', password: 'E2E-admin-password-2026!' },
};

test.skip(Boolean(process.env.BASE_URL), 'Authenticated journeys run only against the isolated local E2E database.');

async function login(request, account) {
  const response = await request.post('/api/v1/auth/login', {
    data: { identifier: account.email, password: account.password },
  });
  expect(response.status(), await response.text()).toBe(200);
  return (await response.json()).data;
}

function authHeaders(token) {
  return { Authorization: `Bearer ${token}` };
}

test('student can sign in and add a menu item to the cart', async ({ page }) => {
  await page.goto('/customer/');
  await page.locator('#auth-email').fill(accounts.student.email);
  await page.locator('#auth-password').fill(accounts.student.password);
  await page.locator('#auth-submit').click();

  await expect(page.locator('#auth-message')).toContainText('Signed in successfully');
  await expect(page.locator('#account-role')).toHaveText('Role: student');
  const firstProduct = page.locator('#catalogue-grid .product-card').first();
  await expect(firstProduct).toBeVisible();
  await firstProduct.getByRole('button', { name: 'Add to cart' }).click();
  await expect(page.locator('#cart-items .cart-line').first()).toBeVisible();
  await expect(page.locator('#cart-subtotal')).not.toHaveText('₹0.00');
});

test('staff can sign in and queue refreshes through HTTP polling', async ({ page }) => {
  let queueRequests = 0;
  page.on('request', request => {
    if (request.url().includes('/api/v1/staff/orders')) queueRequests += 1;
  });

  await page.goto('/staff');
  await page.locator('#staff-login-id').fill(accounts.staff.email);
  await page.locator('#staff-login-pw').fill(accounts.staff.password);
  await page.locator('#staff-login-submit').click();
  await expect(page.locator('#staff-user-badge')).toContainText('E2E Staff');
  await expect(page.locator('#staff-ws-label')).toHaveText('Polling updates');
  const initialRequests = queueRequests;
  await expect.poll(() => queueRequests, { timeout: 16000, intervals: [500, 1000] })
    .toBeGreaterThan(initialRequests);
  await expect(page.locator('#kds-tickets')).toBeVisible();
});

test('admin, wallet, payment error, refund, and staff QR pickup journeys', async ({ request, page }) => {
  const student = await login(request, accounts.student);
  const staff = await login(request, accounts.staff);
  const admin = await login(request, accounts.admin);
  const studentHeaders = authHeaders(student.accessToken);
  const staffHeaders = authHeaders(staff.accessToken);
  const adminHeaders = authHeaders(admin.accessToken);

  const forbiddenDiagnostics = await request.get('/api/v1/admin/diagnostics', { headers: studentHeaders });
  expect(forbiddenDiagnostics.status()).toBe(403);

  const diagnosticsResponse = await request.get('/api/v1/admin/diagnostics', { headers: adminHeaders });
  expect(diagnosticsResponse.status()).toBe(200);
  const diagnostics = (await diagnosticsResponse.json()).data;
  expect(diagnostics.database.schemaReady).toBe(true);
  expect(diagnostics.database.revision).toBe('0022_rewards_and_wallet');
  expect(diagnostics.secretsDisplayed).toBe(false);

  const creditResponse = await request.post('/api/v1/admin/wallet/credit', {
    headers: adminHeaders,
    data: {
      userId: student.user.id,
      amountPaise: 10000,
      reason: 'E2E verified prepaid credit',
      referenceKey: `e2e-credit-${randomUUID()}`,
    },
  });
  expect(creditResponse.status(), await creditResponse.text()).toBe(201);

  const menuResponse = await request.get('/api/v1/menu/items?search=Samosa');
  expect(menuResponse.status()).toBe(200);
  const menu = (await menuResponse.json()).data.items;
  expect(menu.length).toBeGreaterThan(0);
  const menuItemId = menu[0].id;

  const onlineOrderResponse = await request.post('/api/v1/orders', {
    headers: studentHeaders,
    data: { items: [{ menuItemId, quantity: 1 }] },
  });
  expect(onlineOrderResponse.status(), await onlineOrderResponse.text()).toBe(201);
  const onlineOrder = (await onlineOrderResponse.json()).data.order;
  const paymentIntent = await request.post('/api/v1/payments/upi/intent', {
    headers: studentHeaders,
    data: { orderId: onlineOrder.id },
  });
  expect(paymentIntent.status()).toBe(503);
  expect((await paymentIntent.json()).error.code).toBe('PAYMENT_GATEWAY_NOT_CONFIGURED');

  const canceledOnlineOrder = await request.post(`/api/v1/orders/${onlineOrder.id}/cancel`, {
    headers: studentHeaders,
    data: { reason: 'E2E gateway unavailable' },
  });
  expect(canceledOnlineOrder.status()).toBe(200);

  const refundOrderResponse = await request.post('/api/v1/orders', {
    headers: studentHeaders,
    data: { items: [{ menuItemId, quantity: 1 }], payWithWallet: true },
  });
  expect(refundOrderResponse.status(), await refundOrderResponse.text()).toBe(201);
  const refundOrder = (await refundOrderResponse.json()).data.order;
  const walletBeforeRefund = (await (await request.get('/api/v1/wallet/me', { headers: studentHeaders })).json()).data.balancePaise;
  const canceledWalletOrder = await request.post(`/api/v1/orders/${refundOrder.id}/cancel`, {
    headers: studentHeaders,
    data: { reason: 'E2E wallet refund verification' },
  });
  expect(canceledWalletOrder.status()).toBe(200);
  const walletAfterRefund = (await (await request.get('/api/v1/wallet/me', { headers: studentHeaders })).json()).data.balancePaise;
  expect(walletAfterRefund).toBeGreaterThan(walletBeforeRefund);

  const pickupOrderResponse = await request.post('/api/v1/orders', {
    headers: studentHeaders,
    data: { items: [{ menuItemId, quantity: 1 }], payWithWallet: true },
  });
  expect(pickupOrderResponse.status(), await pickupOrderResponse.text()).toBe(201);
  const pickupOrder = (await pickupOrderResponse.json()).data.order;

  const acceptResponse = await request.post(`/api/v1/staff/orders/${pickupOrder.id}/accept`, {
    headers: staffHeaders,
    data: {},
  });
  expect(acceptResponse.status(), await acceptResponse.text()).toBe(200);
  const readyResponse = await request.post(`/api/v1/staff/orders/${pickupOrder.id}/ready`, {
    headers: staffHeaders,
    data: {},
  });
  expect(readyResponse.status(), await readyResponse.text()).toBe(200);

  const pickupTokenResponse = await request.post(`/api/v1/orders/${pickupOrder.id}/pickup-token`, {
    headers: studentHeaders,
  });
  expect(pickupTokenResponse.status(), await pickupTokenResponse.text()).toBe(200);
  const pickupToken = (await pickupTokenResponse.json()).data.pickupToken;

  await page.goto('/staff');
  await page.locator('#staff-login-id').fill(accounts.staff.email);
  await page.locator('#staff-login-pw').fill(accounts.staff.password);
  await page.locator('#staff-login-submit').click();
  await expect(page.locator('#staff-user-badge')).toContainText('E2E Staff');
  await page.locator('[data-staff-tab="pickup"]').click();
  await page.locator('#qr-token-input').fill(pickupToken);
  await page.locator('#btn-inspect-qr').click();
  await expect(page.locator('#inspection-card')).toContainText('E2E Student');
  await expect(page.locator('#inspection-card')).toContainText('Payment: PAID');
  await expect(page.locator('#inspection-card')).toContainText(menu[0].name);
  await page.getByRole('button', { name: /Confirm Handover & Mark Collected/ }).click();
  await expect(page.locator('#pickup-result-box')).toContainText('Verified & Collected');

  const rewards = await request.get('/api/v1/rewards/me', { headers: studentHeaders });
  expect(rewards.status()).toBe(200);
  expect((await rewards.json()).data.pointsBalance).toBeGreaterThan(0);
});
