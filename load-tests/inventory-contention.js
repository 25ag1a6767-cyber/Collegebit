import http from "k6/http";
import { check } from "k6";
import { BASE_URL, authHeaders } from "./config/thresholds.js";

// 600 competing virtual students attempting to reserve from 500 available meals simultaneously
export const options = {
  scenarios: {
    six_hundred_buyers: {
      executor: "shared-iterations",
      vus: 100,
      iterations: 600,
      maxDuration: "60s",
    },
  },
};

export default function () {
  const token = __ENV.STUDENT_TOKEN || "";
  const menuItemId = Number(__ENV.LIMITED_ITEM_ID || 1);
  const idem = `cont-500v600-${__VU}-${__ITER}-${Date.now()}`;
  const res = http.post(
    `${BASE_URL}/api/v1/orders`,
    JSON.stringify({ counterId: 1, items: [{ menuItemId, quantity: 1 }] }),
    authHeaders(token, idem)
  );
  check(res, {
    "atomic reservation 201 or 409 out-of-stock": (r) => r.status === 201 || r.status === 409,
  });
}
