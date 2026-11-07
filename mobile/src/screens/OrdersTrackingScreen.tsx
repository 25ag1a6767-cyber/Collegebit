import React, { useCallback, useEffect, useState } from "react";
import {
  Modal,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TouchableOpacity,
  View
} from "react-native";
import { apiRequest, formatRupeesFromPaise } from "../api/client";
import { useAuth } from "../context/AuthContext";
import { OrderRecord, PickupTokenResponse } from "../types/api";
import { buildQrMatrix } from "../utils/qrMatrix";

interface DigitalReceipt {
  receiptNumber: string;
  issuedAt: string;
  canteenName: string;
  counterName: string;
  orderId: number;
  orderNumber: string;
  tokenNumber?: number | null;
  status: string;
  paymentStatus: string;
  customer: { fullName: string; email: string };
  items: Array<{
    name: string;
    quantity: number;
    unitPricePaise: number;
    lineTotalPaise: number;
  }>;
  subtotalPaise: number;
  discountPaise: number;
  taxPaise: number;
  totalPaise: number;
  payment?: {
    provider: string;
    providerPaymentId?: string | null;
  } | null;
}

export const OrdersTrackingScreen: React.FC = () => {
  const { lastRealtimeTick } = useAuth();
  const [orders, setOrders] = useState<OrderRecord[]>([]);
  const [tokensByOrder, setTokensByOrder] = useState<Record<number, PickupTokenResponse>>({});
  const [activeReceipt, setActiveReceipt] = useState<DigitalReceipt | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const loadOrders = useCallback(async () => {
    try {
      const res = await apiRequest<OrderRecord[] | { items: OrderRecord[] }>(
        "/api/v1/users/me/orders?limit=25"
      );
      setOrders(Array.isArray(res) ? res : res.items || []);
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Could not load orders");
    }
  }, []);

  useEffect(() => {
    loadOrders();
  }, [loadOrders, lastRealtimeTick]);

  const mintPickupToken = async (orderId: number) => {
    setMessage(null);
    try {
      const tok = await apiRequest<PickupTokenResponse>(
        `/api/v1/orders/${orderId}/pickup-token`,
        { method: "POST" }
      );
      setTokensByOrder(prev => ({ ...prev, [orderId]: tok }));
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Could not issue pickup QR");
    }
  };

  const openDigitalReceipt = async (orderId: number) => {
    setMessage(null);
    try {
      const rec = await apiRequest<DigitalReceipt>(
        `/api/v1/orders/${orderId}/receipt?format=json`
      );
      setActiveReceipt(rec);
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Could not load digital receipt");
    }
  };

  const cancelOrder = async (orderId: number) => {
    setMessage(null);
    try {
      await apiRequest(`/api/v1/orders/${orderId}/cancel`, {
        method: "POST",
        body: JSON.stringify({ reason: "Cancelled by student from Android app" })
      });
      await loadOrders();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Cannot cancel order");
    }
  };

  const submitQuickRating = async (orderId: number, rating: number) => {
    try {
      await apiRequest(`/api/v1/orders/${orderId}/review`, {
        method: "POST",
        body: JSON.stringify({ rating, comment: "Rated from Android app" })
      });
      setMessage(`Saved ${rating}/5 rating for Order #${orderId}.`);
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Could not save rating");
    }
  };

  return (
    <ScrollView
      style={styles.screen}
      contentContainerStyle={styles.content}
      refreshControl={
        <RefreshControl
          refreshing={refreshing}
          onRefresh={async () => {
            setRefreshing(true);
            await loadOrders();
            setRefreshing(false);
          }}
        />
      }
    >
      <Text style={styles.heading}>Orders, QR Pickup & Digital Receipts</Text>
      {message && <Text style={styles.banner}>{message}</Text>}

      {orders.map(order => {
        const tok = tokensByOrder[order.id];
        const matrix = tok ? buildQrMatrix(tok.pickupToken) : null;
        const statusLower = order.status.toLowerCase().replace("pending_payment", "pending");
        const payLower = order.paymentStatus.toLowerCase();

        return (
          <View key={order.id} style={styles.card}>
            <View style={styles.rowBetween}>
              <Text style={styles.title}>
                Order #{order.id} · Token #{order.tokenNumber ?? "—"}
              </Text>
              <Text style={styles.price}>{formatRupeesFromPaise(order.totalPaise)}</Text>
            </View>
            <Text style={styles.meta}>
              Status: {order.status.toUpperCase()} · Payment: {order.paymentStatus.toUpperCase()}
            </Text>
            <Text style={styles.items}>
              {order.items.map(i => `${(i as { itemName?: string }).itemName || i.name} × ${i.quantity}`).join(", ")}
            </Text>

            <View style={styles.timelineRow}>
              {["pending", "confirmed", "accepted", "preparing", "ready", "collected"].map(step => (
                <View
                  key={step}
                  style={[styles.stepPill, statusLower === step && styles.stepPillActive]}
                >
                  <Text
                    style={[styles.stepText, statusLower === step && styles.stepTextActive]}
                  >
                    {step.toUpperCase()}
                  </Text>
                </View>
              ))}
            </View>

            {payLower === "paid" && statusLower !== "completed" && statusLower !== "collected" && statusLower !== "cancelled" && (
              <TouchableOpacity
                style={styles.qrBtn}
                onPress={() => mintPickupToken(order.id)}
              >
                <Text style={styles.qrBtnText}>
                  {tok ? "Refresh Short-Lived QR" : "Generate Pickup QR & Fallback Code"}
                </Text>
              </TouchableOpacity>
            )}

            {payLower === "paid" && (
              <TouchableOpacity
                style={styles.receiptBtn}
                onPress={() => openDigitalReceipt(order.id)}
              >
                <Text style={styles.receiptBtnText}>View Digital Tax Receipt</Text>
              </TouchableOpacity>
            )}

            {tok && matrix && (
              <View style={styles.qrBox}>
                <View style={styles.qrGrid}>
                  {matrix.map((row, rIdx) => (
                    <View key={rIdx} style={styles.qrRow}>
                      {row.map((dark, cIdx) => (
                        <View
                          key={cIdx}
                          style={[styles.qrCell, dark ? styles.qrDark : styles.qrLight]}
                        />
                      ))}
                    </View>
                  ))}
                </View>
                <Text style={styles.manualLabel}>Manual 6-char pickup code:</Text>
                <Text style={styles.manualCode}>{tok.manualPickupCode}</Text>
                <Text style={styles.expiry}>
                  Valid until {new Date(tok.expiresAt).toLocaleTimeString()} · Single-use
                </Text>
              </View>
            )}

            {(statusLower === "pending" || statusLower === "confirmed") && (
              <TouchableOpacity
                style={styles.cancelBtn}
                onPress={() => cancelOrder(order.id)}
              >
                <Text style={styles.cancelText}>Cancel Order</Text>
              </TouchableOpacity>
            )}

            {(statusLower === "completed" || statusLower === "collected") && (
              <View style={styles.ratingRow}>
                <Text style={styles.meta}>Rate meal:</Text>
                {[5, 4, 3, 2, 1].map(stars => (
                  <TouchableOpacity
                    key={stars}
                    style={styles.starBtn}
                    onPress={() => submitQuickRating(order.id, stars)}
                  >
                    <Text style={styles.starText}>{stars}★</Text>
                  </TouchableOpacity>
                ))}
              </View>
            )}
          </View>
        );
      })}

      <Modal visible={Boolean(activeReceipt)} animationType="slide" transparent>
        <View style={styles.modalBackdrop}>
          <View style={styles.modalCard}>
            {activeReceipt && (
              <>
                <Text style={styles.modalTitle}>{activeReceipt.canteenName}</Text>
                <Text style={styles.meta}>
                  Receipt: {activeReceipt.receiptNumber} · Counter: {activeReceipt.counterName}
                </Text>
                <Text style={styles.meta}>
                  Customer: {activeReceipt.customer.fullName} ({activeReceipt.customer.email})
                </Text>
                <View style={styles.divider} />
                {activeReceipt.items.map((it, idx) => (
                  <View key={idx} style={styles.rowBetween}>
                    <Text style={styles.items}>
                      {it.quantity} × {it.name}
                    </Text>
                    <Text style={styles.items}>{formatRupeesFromPaise(it.lineTotalPaise)}</Text>
                  </View>
                ))}
                <View style={styles.divider} />
                <View style={styles.rowBetween}>
                  <Text style={styles.meta}>Subtotal</Text>
                  <Text style={styles.meta}>{formatRupeesFromPaise(activeReceipt.subtotalPaise)}</Text>
                </View>
                <View style={styles.rowBetween}>
                  <Text style={styles.meta}>Discount</Text>
                  <Text style={styles.meta}>−{formatRupeesFromPaise(activeReceipt.discountPaise)}</Text>
                </View>
                <View style={styles.rowBetween}>
                  <Text style={styles.meta}>GST / Tax</Text>
                  <Text style={styles.meta}>{formatRupeesFromPaise(activeReceipt.taxPaise)}</Text>
                </View>
                <View style={styles.rowBetween}>
                  <Text style={styles.title}>Total Paid</Text>
                  <Text style={styles.price}>{formatRupeesFromPaise(activeReceipt.totalPaise)}</Text>
                </View>
                {activeReceipt.payment?.providerPaymentId && (
                  <Text style={styles.expiry}>
                    UPI Ref: {activeReceipt.payment.providerPaymentId}
                  </Text>
                )}
                <TouchableOpacity
                  style={styles.qrBtn}
                  onPress={() => setActiveReceipt(null)}
                >
                  <Text style={styles.qrBtnText}>Close Receipt</Text>
                </TouchableOpacity>
              </>
            )}
          </View>
        </View>
      </Modal>
    </ScrollView>
  );
};

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: "#faf9f5" },
  content: { padding: 16, paddingBottom: 32 },
  heading: { fontSize: 20, fontWeight: "800", color: "#12372a", marginBottom: 12 },
  banner: {
    backgroundColor: "#eef6f0",
    color: "#164e32",
    padding: 10,
    borderRadius: 10,
    marginBottom: 12,
    fontSize: 13
  },
  card: {
    backgroundColor: "#fff",
    borderRadius: 16,
    padding: 15,
    borderWidth: 1,
    borderColor: "#dce7de",
    marginBottom: 12
  },
  rowBetween: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  title: { fontSize: 15, fontWeight: "800", color: "#17251d" },
  price: { fontSize: 15, fontWeight: "800", color: "#155d3d" },
  meta: { fontSize: 12, color: "#4f6257", marginTop: 4 },
  items: { fontSize: 13, color: "#17251d", marginTop: 6 },
  timelineRow: { flexDirection: "row", flexWrap: "wrap", gap: 5, marginTop: 10 },
  stepPill: {
    paddingHorizontal: 8,
    paddingVertical: 4,
    borderRadius: 999,
    backgroundColor: "#eff3f0"
  },
  stepPillActive: { backgroundColor: "#12372a" },
  stepText: { fontSize: 10, fontWeight: "800", color: "#5e6e64" },
  stepTextActive: { color: "#fff" },
  qrBtn: {
    marginTop: 12,
    backgroundColor: "#12372a",
    paddingVertical: 10,
    borderRadius: 10,
    alignItems: "center"
  },
  qrBtnText: { color: "#fff", fontWeight: "700", fontSize: 13 },
  receiptBtn: {
    marginTop: 8,
    backgroundColor: "#eef6f0",
    borderWidth: 1,
    borderColor: "#bfd8c6",
    paddingVertical: 9,
    borderRadius: 10,
    alignItems: "center"
  },
  receiptBtnText: { color: "#12372a", fontWeight: "700", fontSize: 13 },
  qrBox: {
    marginTop: 12,
    padding: 14,
    borderRadius: 12,
    backgroundColor: "#f6faf7",
    alignItems: "center",
    borderWidth: 1,
    borderColor: "#d5e4da"
  },
  qrGrid: { padding: 8, backgroundColor: "#fff", borderRadius: 8 },
  qrRow: { flexDirection: "row" },
  qrCell: { width: 4.5, height: 4.5 },
  qrDark: { backgroundColor: "#12372a" },
  qrLight: { backgroundColor: "#ffffff" },
  manualLabel: { fontSize: 12, color: "#4f6257", marginTop: 10 },
  manualCode: {
    fontSize: 20,
    fontWeight: "800",
    letterSpacing: 3,
    color: "#12372a",
    marginTop: 4
  },
  expiry: { fontSize: 11, color: "#65756b", marginTop: 4 },
  cancelBtn: {
    marginTop: 10,
    paddingVertical: 8,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: "#fecdca",
    alignItems: "center"
  },
  cancelText: { color: "#b42318", fontWeight: "700", fontSize: 12 },
  ratingRow: { flexDirection: "row", alignItems: "center", gap: 6, marginTop: 10 },
  starBtn: {
    paddingHorizontal: 9,
    paddingVertical: 5,
    borderRadius: 8,
    backgroundColor: "#fef3c7"
  },
  starText: { fontSize: 12, fontWeight: "800", color: "#92400e" },
  modalBackdrop: {
    flex: 1,
    backgroundColor: "rgba(15, 23, 42, 0.55)",
    justifyContent: "flex-end"
  },
  modalCard: {
    backgroundColor: "#ffffff",
    borderTopLeftRadius: 20,
    borderTopRightRadius: 20,
    padding: 20
  },
  modalTitle: { fontSize: 18, fontWeight: "800", color: "#12372a" },
  divider: { height: 1, backgroundColor: "#e2ebe4", marginVertical: 10 }
});
