import React, { useEffect, useState } from "react";
import {
  Linking,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View
} from "react-native";
import { apiRequest, formatRupeesFromPaise } from "../api/client";
import { useCart } from "../context/CartContext";
import { OrderRecord, UpiPaymentIntent } from "../types/api";

interface Props {
  onOrderCreated: () => void;
}

export const CartCheckoutScreen: React.FC<Props> = ({ onOrderCreated }) => {
  const {
    lines,
    selectedCounterId,
    discountCode,
    setDiscountCode,
    quote,
    updateQuantity,
    clearCart,
    refreshQuote
  } = useCart();

  const [slots, setSlots] = useState<Array<{ slot: string; label: string; remaining: number }>>([]);
  const [pickupSlot, setPickupSlot] = useState<string>("");
  const [notes, setNotes] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);

  useEffect(() => {
    refreshQuote(pickupSlot || null).catch(() => null);
  }, [lines, discountCode, pickupSlot, refreshQuote]);

  useEffect(() => {
    (async () => {
      try {
        const qs = selectedCounterId ? `?counterId=${selectedCounterId}` : "";
        const data = await apiRequest<{ slots?: Array<{ slot: string; label: string; remaining: number }> }>(
          `/api/v1/pickup-slots${qs}`
        );
        setSlots(data.slots || []);
      } catch {
        setSlots([]);
      }
    })();
  }, [selectedCounterId]);

  const handlePlaceAndPayUpi = async () => {
    if (!lines.length) return;
    setBusy(true);
    setStatusMessage(null);
    try {
      const idempotencyKey = `mob-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
      const createdRes = await apiRequest<OrderRecord | { order: OrderRecord }>("/api/v1/orders", {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: JSON.stringify({
          items: lines.map(l => ({
            menuItemId: l.menuItemId,
            quantity: l.quantity,
            customizations: l.customizations
          })),
          counterId: selectedCounterId,
          pickupSlot: pickupSlot || null,
          couponCode: discountCode.trim() || null,
          notes: notes.trim() || null
        })
      });
      const created = "order" in createdRes && createdRes.order ? createdRes.order : (createdRes as OrderRecord);

      const intent = await apiRequest<UpiPaymentIntent>("/api/v1/payments/upi/intent", {
        method: "POST",
        body: JSON.stringify({ orderId: created.id })
      });

      if (intent.upiIntentUri) {
        const supported = await Linking.canOpenURL(intent.upiIntentUri);
        if (supported) {
          await Linking.openURL(intent.upiIntentUri);
        }
      }

      await clearCart();
      setStatusMessage(
        `Order #${created.id} created (${formatRupeesFromPaise(intent.amountPaise)}). Payment status updates automatically once UPI confirms.`
      );
      onOrderCreated();
    } catch (err) {
      setStatusMessage(err instanceof Error ? err.message : "Could not complete checkout");
    } finally {
      setBusy(false);
    }
  };

  return (
    <ScrollView style={styles.screen} contentContainerStyle={styles.content}>
      <Text style={styles.heading}>Your Basket & UPI Checkout</Text>

      {lines.length === 0 ? (
        <Text style={styles.empty}>Your basket is empty. Add items from the Menu tab.</Text>
      ) : (
        <>
          {lines.map(line => (
            <View key={line.menuItemId} style={styles.lineCard}>
              <View style={styles.lineInfo}>
                <Text style={styles.lineName}>{line.name}</Text>
                <Text style={styles.lineMeta}>
                  {formatRupeesFromPaise(line.unitPricePaise)} × {line.quantity}
                </Text>
              </View>
              <View style={styles.stepper}>
                <TouchableOpacity
                  style={styles.stepBtn}
                  onPress={() => updateQuantity(line.menuItemId, line.quantity - 1)}
                >
                  <Text style={styles.stepText}>−</Text>
                </TouchableOpacity>
                <Text style={styles.qty}>{line.quantity}</Text>
                <TouchableOpacity
                  style={styles.stepBtn}
                  onPress={() => updateQuantity(line.menuItemId, line.quantity + 1)}
                >
                  <Text style={styles.stepText}>+</Text>
                </TouchableOpacity>
              </View>
            </View>
          ))}

          {slots.length > 0 && (
            <>
              <Text style={styles.label}>Pickup Break Slot</Text>
              <ScrollView horizontal showsHorizontalScrollIndicator={false} style={styles.slotScroll}>
                <TouchableOpacity
                  style={[styles.slotChip, !pickupSlot && styles.slotChipActive]}
                  onPress={() => setPickupSlot("")}
                >
                  <Text style={[styles.slotText, !pickupSlot && styles.slotTextActive]}>ASAP</Text>
                </TouchableOpacity>
                {slots.map(s => (
                  <TouchableOpacity
                    key={s.slot}
                    disabled={s.remaining <= 0}
                    style={[styles.slotChip, pickupSlot === s.slot && styles.slotChipActive]}
                    onPress={() => setPickupSlot(s.slot)}
                  >
                    <Text style={[styles.slotText, pickupSlot === s.slot && styles.slotTextActive]}>
                      {s.label} ({s.remaining})
                    </Text>
                  </TouchableOpacity>
                ))}
              </ScrollView>
            </>
          )}

          <Text style={styles.label}>Promo / Discount Code</Text>
          <View style={styles.couponRow}>
            <TextInput
              style={styles.couponInput}
              value={discountCode}
              onChangeText={setDiscountCode}
              autoCapitalize="characters"
              placeholder="e.g. BREAK20"
            />
            <TouchableOpacity
              style={styles.applyBtn}
              onPress={() => refreshQuote(pickupSlot || null).catch(e => setStatusMessage(e.message))}
            >
              <Text style={styles.applyBtnText}>Apply</Text>
            </TouchableOpacity>
          </View>

          <Text style={styles.label}>Preparation Notes (optional)</Text>
          <TextInput
            style={styles.notesInput}
            value={notes}
            onChangeText={setNotes}
            placeholder="Less spicy, no cutlery, etc."
          />

          {quote && (
            <View style={styles.quoteBox}>
              <View style={styles.quoteRow}>
                <Text style={styles.quoteLabel}>Subtotal</Text>
                <Text style={styles.quoteVal}>{formatRupeesFromPaise(quote.subtotalPaise)}</Text>
              </View>
              <View style={styles.quoteRow}>
                <Text style={styles.quoteLabel}>Approved Discount</Text>
                <Text style={styles.quoteVal}>−{formatRupeesFromPaise(quote.discountPaise)}</Text>
              </View>
              <View style={styles.quoteRow}>
                <Text style={styles.quoteLabel}>Tax</Text>
                <Text style={styles.quoteVal}>{formatRupeesFromPaise(quote.taxPaise)}</Text>
              </View>
              <View style={[styles.quoteRow, styles.totalRow]}>
                <Text style={styles.totalLabel}>Payable Total</Text>
                <Text style={styles.totalVal}>{formatRupeesFromPaise(quote.totalPaise)}</Text>
              </View>
            </View>
          )}

          <TouchableOpacity
            style={styles.payBtn}
            onPress={handlePlaceAndPayUpi}
            disabled={busy}
          >
            <Text style={styles.payBtnText}>
              {busy ? "Reserving Stock & Creating UPI Intent…" : "Confirm & Pay via UPI"}
            </Text>
          </TouchableOpacity>
        </>
      )}

      {statusMessage && <Text style={styles.statusMsg}>{statusMessage}</Text>}
    </ScrollView>
  );
};

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: "#faf9f5" },
  content: { padding: 16, paddingBottom: 32 },
  heading: { fontSize: 20, fontWeight: "800", color: "#12372a", marginBottom: 12 },
  empty: { color: "#5e6e64", fontSize: 14, marginTop: 20 },
  lineCard: {
    backgroundColor: "#fff",
    borderRadius: 14,
    padding: 14,
    borderWidth: 1,
    borderColor: "#dce7de",
    marginBottom: 10,
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center"
  },
  lineInfo: { flex: 1, marginRight: 10 },
  lineName: { fontWeight: "800", fontSize: 15, color: "#17251d" },
  lineMeta: { fontSize: 12, color: "#5e6e64", marginTop: 3 },
  stepper: { flexDirection: "row", alignItems: "center", gap: 10 },
  stepBtn: {
    width: 32,
    height: 32,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: "#c6d4c8",
    alignItems: "center",
    justifyContent: "center"
  },
  stepText: { fontSize: 16, fontWeight: "800", color: "#12372a" },
  qty: { fontWeight: "800", fontSize: 15 },
  label: { fontSize: 13, fontWeight: "700", color: "#234638", marginTop: 12, marginBottom: 6 },
  slotScroll: { marginBottom: 4 },
  slotChip: {
    paddingHorizontal: 12,
    paddingVertical: 8,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: "#cfdcd2",
    marginRight: 8,
    backgroundColor: "#fff"
  },
  slotChipActive: { backgroundColor: "#12372a", borderColor: "#12372a" },
  slotText: { fontSize: 12, fontWeight: "700", color: "#234638" },
  slotTextActive: { color: "#fff" },
  couponRow: { flexDirection: "row", gap: 8 },
  couponInput: {
    flex: 1,
    backgroundColor: "#fff",
    borderWidth: 1,
    borderColor: "#d5dfd6",
    borderRadius: 10,
    paddingHorizontal: 12,
    paddingVertical: 10
  },
  applyBtn: {
    backgroundColor: "#26734e",
    paddingHorizontal: 16,
    borderRadius: 10,
    justifyContent: "center"
  },
  applyBtnText: { color: "#fff", fontWeight: "700" },
  notesInput: {
    backgroundColor: "#fff",
    borderWidth: 1,
    borderColor: "#d5dfd6",
    borderRadius: 10,
    paddingHorizontal: 12,
    paddingVertical: 10
  },
  quoteBox: {
    marginTop: 14,
    backgroundColor: "#eef6f0",
    borderRadius: 14,
    padding: 14
  },
  quoteRow: { flexDirection: "row", justifyContent: "space-between", marginBottom: 6 },
  quoteLabel: { color: "#365244", fontSize: 13 },
  quoteVal: { color: "#17251d", fontWeight: "700", fontSize: 13 },
  totalRow: { borderTopWidth: 1, borderColor: "#cad9ce", paddingTop: 8, marginTop: 4 },
  totalLabel: { fontSize: 15, fontWeight: "800", color: "#12372a" },
  totalVal: { fontSize: 16, fontWeight: "800", color: "#12372a" },
  payBtn: {
    marginTop: 16,
    backgroundColor: "#12372a",
    paddingVertical: 15,
    borderRadius: 12,
    alignItems: "center"
  },
  payBtnText: { color: "#fff", fontWeight: "800", fontSize: 15 },
  statusMsg: { marginTop: 12, color: "#1b5e3a", fontWeight: "600", fontSize: 13 }
});
