import http from "k6/http";
import { check, sleep } from "k6";
import { BASE_URL, STANDARD_THRESHOLDS, RAMP_STAGES_3000, authHeaders } from "./config/thresholds.js";

export const options = {
  stages: RAMP_STAGES_3000,
  thresholds: STANDARD_THRESHOLDS,
};

export default function () {
  const menuRes = http.get(`${BASE_URL}/api/v1/menu?limit=20`, { tags: { endpoint: "menu" } });
  check(menuRes, {
    "menu status 200": (r) => r.status === 200,
    "menu has items": (r) => (r.json("data.items") || []).length > 0,
  });

  const token = __ENV.STUDENT_TOKEN || "";
  if (token) {
    const idem = `k6-indiv-${__VU}-${__ITER}-${Date.now()}`;
    const ordRes = http.post(
      `${BASE_URL}/api/v1/orders`,
      JSON.stringify({ counterId: 1, items: [{ menuItemId: 1, quantity: 1 }] }),
      { ...authHeaders(token, idem), tags: { endpoint: "orders" } }
    );
    check(ordRes, {
      "order created or out-of-stock": (r) => r.status === 201 || r.status === 409,
    });
  }
  sleep(0.2);
}
