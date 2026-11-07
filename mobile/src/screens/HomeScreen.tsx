import React, { useCallback, useEffect, useState } from "react";
import {
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TouchableOpacity,
  View
} from "react-native";
import { apiRequest, formatRupeesFromPaise } from "../api/client";
import { useAuth } from "../context/AuthContext";
import { useCart } from "../context/CartContext";
import {
  ActiveDiscount,
  AIRecommendationEntry,
  CounterSummary,
  OrderRecord
} from "../types/api";

interface Props {
  onNavigate: (tab: "menu" | "cart" | "orders" | "ai") => void;
}

export const HomeScreen: React.FC<Props> = ({ onNavigate }) => {
  const { user, lastRealtimeTick } = useAuth();
  const { selectedCounterId, setSelectedCounterId, setDiscountCode, addItem } = useCart();
  const [counters, setCounters] = useState<CounterSummary[]>([]);
  const [offers, setOffers] = useState<ActiveDiscount[]>([]);
  const [recommendations, setRecommendations] = useState<AIRecommendationEntry[]>([]);
  const [activeOrder, setActiveOrder] = useState<OrderRecord | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const loadDiscovery = useCallback(async () => {
    try {
      const [cRes, oRes, rRes] = await Promise.all([
        apiRequest<CounterSummary[] | { items: CounterSummary[] }>("/api/v1/counters"),
        apiRequest<ActiveDiscount[] | { items: ActiveDiscount[] }>("/api/v1/discounts/active"),
        apiRequest<{ recommendations?: AIRecommendationEntry[]; items?: AIRecommendationEntry[] }>(
          `/api/v1/ai/recommendations?limit=5${selectedCounterId ? `&counterId=${selectedCounterId}` : ""}`
        )
      ]);
      setCounters(Array.isArray(cRes) ? cRes : cRes.items || []);
      setOffers(Array.isArray(oRes) ? oRes : oRes.items || []);
      setRecommendations(rRes.recommendations || rRes.items || []);

      if (user?.role === "student") {
        const ord = await apiRequest<OrderRecord[] | { items: OrderRecord[] }>(
          "/api/v1/users/me/orders?limit=5"
        );
        const list = Array.isArray(ord) ? ord : ord.items || [];
        const live = list.find(
          x => !["completed", "collected", "cancelled", "refunded", "refund_pending"].includes(x.status.toLowerCase())
        );
        setActiveOrder(live || null);
      }
    } catch {
      // Keep last loaded snapshot
    }
  }, [selectedCounterId, user?.role]);

  useEffect(() => {
    loadDiscovery();
  }, [loadDiscovery, lastRealtimeTick]);

  const onRefresh = async () => {
    setRefreshing(true);
    await loadDiscovery();
    setRefreshing(false);
  };

  return (
    <ScrollView
      style={styles.screen}
      contentContainerStyle={styles.content}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} />}
    >
      <View style={styles.heroCard}>
        <Text style={styles.greeting}>Hi, {user?.fullName?.split(" ")[0] || "Student"} 👋</Text>
        <Text style={styles.heroSub}>
          Pre-order before your break, pay via UPI, and scan your single-use QR at the counter.
        </Text>
        <View style={styles.quickRow}>
          <TouchableOpacity style={styles.primaryChip} onPress={() => onNavigate("menu")}>
            <Text style={styles.primaryChipText}>Browse Menu</Text>
          </TouchableOpacity>
          <TouchableOpacity style={styles.secondaryChip} onPress={() => onNavigate("ai")}>
            <Text style={styles.secondaryChipText}>✨ Ask AI Concierge</Text>
          </TouchableOpacity>
        </View>
      </View>

      {activeOrder && (
        <TouchableOpacity style={styles.activeBanner} onPress={() => onNavigate("orders")}>
          <Text style={styles.activeBannerTitle}>
            Live Order #{activeOrder.id} · Token #{activeOrder.tokenNumber ?? "—"}
          </Text>
          <Text style={styles.activeBannerSub}>
            Status: {activeOrder.status.toUpperCase()} · Tap to view QR & pickup code
          </Text>
        </TouchableOpacity>
      )}

      <Text style={styles.sectionTitle}>Live Counters & Wait Times</Text>
      {counters.map(counter => {
        const selected = selectedCounterId === counter.id;
        return (
          <TouchableOpacity
            key={counter.id}
            style={[styles.counterCard, selected && styles.counterSelected]}
            onPress={() => setSelectedCounterId(selected ? null : counter.id)}
          >
            <View style={styles.rowBetween}>
              <Text style={styles.counterName}>{counter.name}</Text>
              <Text style={styles.badge}>
                {counter.isOpen !== false && counter.isActive ? "OPEN" : "PAUSED"}
              </Text>
            </View>
            <Text style={styles.metaText}>
              Approx wait ~{counter.estimatedWaitMinutes ?? 5} min · Active orders:{" "}
              {counter.activeOrders ?? 0}
            </Text>
          </TouchableOpacity>
        );
      })}

      {offers.length > 0 && (
        <>
          <Text style={styles.sectionTitle}>Available Campus Offers</Text>
          {offers.map(offer => (
            <TouchableOpacity
              key={offer.id}
              style={styles.offerCard}
              onPress={() => {
                setDiscountCode(offer.code);
                onNavigate("cart");
              }}
            >
              <Text style={styles.offerCode}>{offer.code}</Text>
              <Text style={styles.offerDesc}>
                {offer.title} · Min order {formatRupeesFromPaise(offer.minOrderPaise)}
              </Text>
            </TouchableOpacity>
          ))}
        </>
      )}

      <Text style={styles.sectionTitle}>AI-Recommended Meals</Text>
      {recommendations.map((entry, idx) => {
        const item = entry.item || (entry as unknown as AIRecommendationEntry["item"]);
        return (
          <View key={`${item.id}-${idx}`} style={styles.recoCard}>
            <Text style={styles.reasonPill}>{entry.reasons?.[0] || "Popular campus pick"}</Text>
            <View style={styles.rowBetween}>
              <Text style={styles.itemName}>{item.name}</Text>
              <Text style={styles.itemPrice}>{formatRupeesFromPaise(item.pricePaise)}</Text>
            </View>
            <Text style={styles.metaText}>
              ~{item.preparationMinutes || 10}m prep · {item.stockQuantity} in stock
            </Text>
            <TouchableOpacity
              style={styles.addBtn}
              onPress={() => addItem(item, 1, [])}
            >
              <Text style={styles.addBtnText}>Add to Basket</Text>
            </TouchableOpacity>
          </View>
        );
      })}
    </ScrollView>
  );
};

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: "#faf9f5" },
  content: { padding: 16, paddingBottom: 32 },
  heroCard: {
    backgroundColor: "#12372a",
    borderRadius: 18,
    padding: 18,
    marginBottom: 16
  },
  greeting: { color: "#fff", fontSize: 22, fontWeight: "800", marginBottom: 6 },
  heroSub: { color: "#d5e6dc", fontSize: 13, lineHeight: 19, marginBottom: 14 },
  quickRow: { flexDirection: "row", gap: 10 },
  primaryChip: { backgroundColor: "#f0a35b", paddingVertical: 10, paddingHorizontal: 14, borderRadius: 10 },
  primaryChipText: { color: "#12372a", fontWeight: "800", fontSize: 13 },
  secondaryChip: {
    backgroundColor: "#1f533e",
    paddingVertical: 10,
    paddingHorizontal: 14,
    borderRadius: 10
  },
  secondaryChipText: { color: "#fff", fontWeight: "700", fontSize: 13 },
  activeBanner: {
    backgroundColor: "#e6f5ec",
    borderWidth: 1,
    borderColor: "#8ed1ab",
    borderRadius: 14,
    padding: 14,
    marginBottom: 16
  },
  activeBannerTitle: { fontWeight: "800", color: "#114b2d", fontSize: 15 },
  activeBannerSub: { color: "#256643", fontSize: 12, marginTop: 3 },
  sectionTitle: { fontSize: 16, fontWeight: "800", color: "#12372a", marginTop: 10, marginBottom: 8 },
  counterCard: {
    backgroundColor: "#fff",
    borderWidth: 1,
    borderColor: "#dce7de",
    borderRadius: 14,
    padding: 14,
    marginBottom: 10
  },
  counterSelected: { borderColor: "#12372a", borderWidth: 2, backgroundColor: "#f3faf5" },
  rowBetween: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  counterName: { fontWeight: "800", fontSize: 15, color: "#17251d" },
  badge: { fontSize: 11, fontWeight: "800", color: "#15683c" },
  metaText: { fontSize: 12, color: "#5e6e64", marginTop: 4 },
  offerCard: {
    backgroundColor: "#fff8eb",
    borderWidth: 1,
    borderColor: "#f0cf95",
    borderRadius: 12,
    padding: 12,
    marginBottom: 8
  },
  offerCode: { fontWeight: "800", color: "#8a4b08", fontSize: 14 },
  offerDesc: { fontSize: 12, color: "#6b4718", marginTop: 2 },
  recoCard: {
    backgroundColor: "#fff",
    borderWidth: 1,
    borderColor: "#dce7de",
    borderRadius: 14,
    padding: 14,
    marginBottom: 10
  },
  reasonPill: {
    alignSelf: "flex-start",
    backgroundColor: "#eaf6ef",
    color: "#1b6840",
    fontSize: 11,
    fontWeight: "700",
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 8,
    marginBottom: 6
  },
  itemName: { fontSize: 15, fontWeight: "800", color: "#17251d" },
  itemPrice: { fontSize: 15, fontWeight: "800", color: "#155d3d" },
  addBtn: {
    marginTop: 10,
    backgroundColor: "#12372a",
    paddingVertical: 9,
    borderRadius: 9,
    alignItems: "center"
  },
  addBtnText: { color: "#fff", fontWeight: "700", fontSize: 13 }
});
