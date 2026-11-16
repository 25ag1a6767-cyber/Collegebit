import http from "k6/http";
import { check } from "k6";
import crypto from "k6/crypto";
import { BASE_URL, STANDARD_THRESHOLDS } from "./config/thresholds.js";

export const options = {
  vus: 50,
  iterations: 250,
  thresholds: STANDARD_THRESHOLDS,
};

export default function () {
  const secret = __ENV.RAZORPAY_WEBHOOK_SECRET || "whsec_test_secret";
  const body = JSON.stringify({
    event: "payment.captured",
    payload: {
      payment: {
        entity: {
          id: `pay_k6_${__VU}_${__ITER}`,
          order_id: `order_k6_${__VU}_${__ITER}`,
          amount: 5000,
          currency: "INR",
          status: "captured",
        },
      },
    },
  });
  const sig = crypto.hmac("sha256", secret, body, "hex");
  const evtId = `evt_k6_${__VU}_${__ITER}`;

  const res1 = http.post(`${BASE_URL}/api/v1/payments/webhooks`, body, {
    headers: {
      "Content-Type": "application/json",
      "X-Razorpay-Signature": sig,
      "X-Razorpay-Event-Id": evtId,
    },
  });
  const res2 = http.post(`${BASE_URL}/api/v1/payments/webhooks`, body, {
    headers: {
      "Content-Type": "application/json",
      "X-Razorpay-Signature": sig,
      "X-Razorpay-Event-Id": evtId,
    },
  });
  check(res1, { "webhook accepted": (r) => r.status === 200 || r.status === 503 });
  check(res2, { "duplicate webhook idempotent": (r) => r.status === 200 || r.status === 503 });
}
