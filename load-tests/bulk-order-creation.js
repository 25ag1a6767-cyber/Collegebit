import http from "k6/http";
import { check } from "k6";
import { BASE_URL, STANDARD_THRESHOLDS, authHeaders } from "./config/thresholds.js";

export const options = {
  vus: 50,
  iterations: 200,
  thresholds: STANDARD_THRESHOLDS,
};

export default function () {
  const token = __ENV.COORD_TOKEN || "";
  const payload = JSON.stringify({
    departmentName: `Dept-${__VU}`,
    eventTitle: `Bulk Test ${__ITER}`,
    pickupSlot: "12:30-13:00",
    counterId: (__ITER % 2) + 1,
    paymentMode: __ITER % 2 === 0 ? "centralized" : "individual",
    distributionMode: __ITER % 2 === 0 ? "representative" : "individual",
    items: [{ menuItemId: 1, quantity: 100 }],
  });
  const res = http.post(`${BASE_URL}/api/v1/bulk-orders`, payload, {
    ...authHeaders(token),
    tags: { endpoint: "orders" },
  });
  check(res, { "bulk order created 201": (r) => r.status === 201 });
}
