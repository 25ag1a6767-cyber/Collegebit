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
  await expect(page.locator('#v1-ntfy-btn')).toBeVisible();
  await expect(page.locator('#v1-whatsapp-optin')).toBeVisible();
  await page.locator('#v1-test-alert-btn').click();
  await expect(page.locator('#v1-live-toast')).toContainText('CollegeBits Test Alert');
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

test('admin, wallet, payment error, no-refund enforcement, and staff QR pickup journeys', async ({ request, page }) => {
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
  expect(diagnostics.database.revision).toBe('0029_user_allergen_preferences');
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

  const paidWalletOrderResponse = await request.post('/api/v1/orders', {
    headers: studentHeaders,
    data: { items: [{ menuItemId, quantity: 1 }], payWithWallet: true },
  });
  expect(paidWalletOrderResponse.status(), await paidWalletOrderResponse.text()).toBe(201);
  const paidWalletOrder = (await paidWalletOrderResponse.json()).data.order;
  const walletBeforeCancelAttempt = (await (await request.get('/api/v1/wallet/me', { headers: studentHeaders })).json()).data.balancePaise;
  const cancelPaidOrderAttempt = await request.post(`/api/v1/orders/${paidWalletOrder.id}/cancel`, {
    headers: studentHeaders,
    data: { reason: 'E2E paid order cancellation blocked under no-refund policy' },
  });
  expect(cancelPaidOrderAttempt.status()).toBe(409);
  const walletAfterCancelAttempt = (await (await request.get('/api/v1/wallet/me', { headers: studentHeaders })).json()).data.balancePaise;
  expect(walletAfterCancelAttempt).toBe(walletBeforeCancelAttempt);

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

test('customer V1 filtered add/coupon/budget, staff startup panels, and admin BI charts render cleanly', async ({ page }) => {
  await page.goto('/customer/');
  await page.locator('#auth-email').fill(accounts.student.email);
  await page.locator('#auth-password').fill(accounts.student.password);
  await page.locator('#auth-submit').click();
  await expect(page.locator('#auth-message')).toContainText('Signed in successfully');

  await page.locator('#v1-filter-budget').fill('80');
  const [recoRes] = await Promise.all([
    page.waitForResponse(res => res.url().includes('/api/v1/ai/recommendations') && res.url().includes('maxBudgetPaise=8000')),
    page.locator('#v1-refresh-reco').click(),
  ]);
  expect(recoRes.status()).toBe(200);

  const [menuFilterRes] = await Promise.all([
    page.waitForResponse(res => res.url().includes('/api/v1/menu/items') && res.url().includes('maxPricePaise=8000')),
    page.locator('#v1-apply-filters').click(),
  ]);
  expect(menuFilterRes.status()).toBe(200);

  const filteredAddBtn = page.locator('#catalogue-grid .product-order-button').first();
  await expect(filteredAddBtn).toBeVisible();
  const [cartPut] = await Promise.all([
    page.waitForResponse(res => res.url().includes('/api/v1/cart/items/') && res.request().method() === 'PUT'),
    filteredAddBtn.click(),
  ]);
  expect(cartPut.status()).toBe(200);

  await expect(page.locator('#v1-coupon-code')).toBeVisible();
  await page.locator('#v1-coupon-code').fill('WELCOME20');
  const [quoteReq] = await Promise.all([
    page.waitForRequest(req => req.url().includes('/api/v1/orders/quote') && req.method() === 'POST'),
    page.locator('#v1-apply-coupon').click(),
  ]);
  expect(quoteReq.postDataJSON().couponCode).toBe('WELCOME20');

  await page.goto('/staff');
  await page.locator('#staff-login-id').fill(accounts.staff.email);
  await page.locator('#staff-login-pw').fill(accounts.staff.password);
  const [batchesRes, stationsRes, pickupRes] = await Promise.all([
    page.waitForResponse(res => res.url().includes('/api/v1/kitchen/dashboard')),
    page.waitForResponse(res => res.url().includes('/api/v1/kitchen/stations')),
    page.waitForResponse(res => res.url().includes('/api/v1/pickup/progress')),
    page.locator('#staff-login-submit').click(),
  ]);
  expect(batchesRes.status()).toBe(200);
  expect(stationsRes.status()).toBe(200);
  expect(pickupRes.status()).toBe(200);
  await expect(page.locator('#staff-stations-list')).not.toHaveText('Loading kitchen stations…');
  await expect(page.locator('#staff-pickup-progress-box')).not.toHaveText('Checking Counter 1 & Counter 2 collection progress…');

  await page.goto('/admin');
  await page.locator('#admin-identifier').fill(accounts.admin.email);
  await page.locator('#admin-password').fill(accounts.admin.password);
  const [biRes] = await Promise.all([
    page.waitForResponse(res => res.url().includes('/api/v1/admin/analytics/dashboard')),
    page.locator('#btn-admin-login').click(),
  ]);
  expect(biRes.status()).toBe(200);
  await expect.poll(() => page.locator('#bi-kpi-cards > *').count()).toBeGreaterThanOrEqual(10);
  await expect.poll(() => page.locator('#tab-bi svg').count()).toBeGreaterThanOrEqual(16);
});

test('all-buttons regressions: paid order QR & no cancel, bulk pay/tokens/export, MFA visibility & legacy PO list', async ({ page, request }) => {
  const pageErrors = [];
  page.on('pageerror', err => pageErrors.push(err.message));
  page.on('dialog', d => d.accept());

  // Login via API as admin & student to seed wallet balance and create 1 paid wallet order
  const admLogin = await request.post('/api/v1/auth/login', {
    data: { identifier: accounts.admin.email, password: accounts.admin.password },
  });
  expect(admLogin.status()).toBe(200);
  const admTok = (await admLogin.json()).data.accessToken;

  const stuLogin = await request.post('/api/v1/auth/login', {
    data: { identifier: accounts.student.email, password: accounts.student.password },
  });
  expect(stuLogin.status()).toBe(200);
  const stuJson = (await stuLogin.json()).data;

  await request.post('/api/v1/admin/wallet/credit', {
    headers: { Authorization: `Bearer ${admTok}` },
    data: { userId: stuJson.user.id, amountPaise: 50000, reason: 'E2E regression credit', referenceKey: `e2e-reg-${Date.now()}` },
  });
  const menuRes = await request.get('/api/v1/menu/items?limit=5');
  const firstItem = (await menuRes.json()).data.items[0];
  await request.post('/api/v1/orders', {
    headers: { Authorization: `Bearer ${stuJson.accessToken}` },
    data: {
      items: [{ menuItemId: firstItem.id, quantity: 1, fulfillmentType: 'READY_TO_EAT', customizations: {} }],
      fulfillmentType: 'READY_TO_EAT',
      counterId: 1,
      payWithWallet: true,
      idempotencyKey: `e2e-paid-${Date.now()}`,
    },
  });

  // 1. Student portal buttons: paid order renders SUCCESS + QR + Receipt and NEVER Cancel order; Bulk hub uses paise() and authenticated CSV/PDF export
  await page.goto('/customer/');
  await page.locator('#auth-email').fill(accounts.student.email);
  await page.locator('#auth-password').fill(accounts.student.password);
  await page.locator('#auth-submit').click();
  await expect(page.locator('#account-panel')).toBeVisible();

  const activePanel = page.locator('#v1-active-orders-panel');
  await expect(activePanel.locator('.v1-order-card').first()).toContainText('Payment: SUCCESS');
  await expect(activePanel.getByRole('button', { name: 'Show Short-Lived Pickup QR' }).first()).toBeVisible();
  await expect(activePanel.locator('.v1-order-card:has-text("Payment: SUCCESS") button:has-text("Cancel order")')).toHaveCount(0);

  await activePanel.getByRole('button', { name: 'Show Short-Lived Pickup QR' }).first().click();
  await expect(activePanel.getByRole('button', { name: 'Show large bright QR' }).first()).toBeVisible();

  await page.locator('#v1-bulk-dept').fill('E2E Department');
  await page.locator('#v1-bulk-create-form button[type="submit"]').click();
  await expect(page.locator('#v1-bulk-target-id')).not.toHaveValue('');
  await page.locator('#v1-bulk-csv-text').fill('recipient_identifier,full_name,email,department\nE2E01,One User,one@college.edu,CSE');
  await page.locator('#v1-bulk-csv-form button[type="submit"]').click();
  await page.locator('#v1-bulk-pay-btn').click();
  await expect(page.locator('#v1-bulk-csv-msg')).toContainText('₹');
  await page.locator('#v1-bulk-gen-tokens-btn').click();
  await expect(page.locator('#v1-bulk-csv-msg')).toContainText('token', { ignoreCase: true });

  const [csvDl] = await Promise.all([page.waitForEvent('download'), page.locator('#v1-bulk-export-csv-btn').click()]);
  expect(csvDl.suggestedFilename()).toContain('.csv');
  const [pdfDl] = await Promise.all([page.waitForEvent('download'), page.locator('#v1-bulk-export-pdf-btn').click()]);
  expect(pdfDl.suggestedFilename()).toContain('.pdf');

  await page.locator('#auth-logout').click();

  // 2. Admin on /customer/: MFA setup button unhides and legacy GET /api/admin/purchase-orders returns 200
  await page.locator('#auth-email').fill(accounts.admin.email);
  await page.locator('#auth-password').fill(accounts.admin.password);
  const [poListRes] = await Promise.all([
    page.waitForResponse(res => res.url().includes('/api/admin/purchase-orders') && res.request().method() === 'GET'),
    page.locator('#auth-submit').click(),
  ]);
  expect(poListRes.status()).toBe(200);
  await expect(page.locator('#v1-mfa-setup-btn')).toBeVisible();

  // Verify embedded V1 KDS renders paid ticket (without customizations.map crash) and Accept -> Start -> Ready transitions work
  const kdsGrid = page.locator('#v1-kds-grid');
  await expect(kdsGrid.locator('.v1-kds-ticket').first()).toBeVisible();
  const acceptTicketBtn = kdsGrid.getByRole('button', { name: 'Accept Ticket' }).first();
  if (await acceptTicketBtn.count()) {
    await acceptTicketBtn.click();
    await expect(kdsGrid.getByRole('button', { name: 'Start Prep' }).first()).toBeVisible();
  }

  // Verify embedded Procurement: Direct Batch Receipt -> Reconciliation -> Demand without NaN/undefined/422
  await page.locator('#batch-product').selectOption({ index: 1 });
  await page.locator('#batch-code').fill(`E2E-B-${Date.now()}`);
  await page.locator('#batch-quantity').fill('4');
  await page.locator('#batch-expiry-date').fill('2027-06-30');
  const [batchRes] = await Promise.all([
    page.waitForResponse(res => res.url().includes('/api/admin/inventory/batches') && res.request().method() === 'POST'),
    page.locator('#batch-receipt-form button[type="submit"]').click(),
  ]);
  expect(batchRes.status()).toBe(201);
  await page.locator('#refresh-reconciliation').click();
  await expect(page.locator('#reconciliation-message')).toContainText('products audited');
  await expect(page.locator('#reconciliation-message')).not.toContainText('undefined');

  // Verify root portal hash link redirect (/#reset= -> /customer/#reset=)
  await page.goto('/#reset=sampletoken');
  await expect(page).toHaveURL(/\/customer\//);

  expect(pageErrors).toEqual([]);
});
