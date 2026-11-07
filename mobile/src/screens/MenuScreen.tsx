import React, { useCallback, useEffect, useState } from "react";
import {
  Modal,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View
} from "react-native";
import { apiRequest, formatRupeesFromPaise } from "../api/client";
import { useCart } from "../context/CartContext";
import { CustomizationOption, MenuItemDetail, MenuItemSummary } from "../types/api";

export const MenuScreen: React.FC = () => {
  const { selectedCounterId, addItem } = useCart();
  const [search, setSearch] = useState("");
  const [dietary, setDietary] = useState("");
  const [onlyFavorites, setOnlyFavorites] = useState(false);
  const [favoriteIds, setFavoriteIds] = useState<number[]>([]);
  const [maxBudgetRupees, setMaxBudgetRupees] = useState("");
  const [items, setItems] = useState<MenuItemSummary[]>([]);
  const [detail, setDetail] = useState<MenuItemDetail | null>(null);
  const [selectedCustomizations, setSelectedCustomizations] = useState<CustomizationOption[]>([]);
  const [qty, setQty] = useState(1);

  const loadFavorites = useCallback(async () => {
    try {
      const res = await apiRequest<{ items: MenuItemSummary[] }>("/api/v1/favorites");
      setFavoriteIds((res.items || []).map(x => x.id));
    } catch {

    }
  }, []);

  const loadMenu = useCallback(async () => {
    const params = new URLSearchParams({ limit: "50" });
    if (search.trim()) params.set("search", search.trim());
    if (dietary) params.set("dietaryTag", dietary);
    if (selectedCounterId) params.set("counterId", String(selectedCounterId));
    const budget = Number(maxBudgetRupees);
    if (budget > 0) params.set("maxPricePaise", String(Math.round(budget * 100)));
    try {
      const res = await apiRequest<MenuItemSummary[] | { items: MenuItemSummary[] }>(
        `/api/v1/menu/items?${params.toString()}`
      );
      setItems(Array.isArray(res) ? res : res.items || []);
    } catch {
      setItems([]);
    }
  }, [dietary, maxBudgetRupees, search, selectedCounterId]);

  useEffect(() => {
    loadMenu();
    loadFavorites();
  }, [loadMenu, loadFavorites]);

  const toggleFavorite = async (itemId: number) => {
    const isFav = favoriteIds.includes(itemId);
    try {
      if (isFav) {
        await apiRequest(`/api/v1/favorites/${itemId}`, { method: "DELETE" });
        setFavoriteIds(prev => prev.filter(id => id !== itemId));
      } else {
        await apiRequest(`/api/v1/favorites/${itemId}`, { method: "POST" });
        setFavoriteIds(prev => [...prev, itemId]);
      }
    } catch {

    }
  };

  const openDetail = async (id: number) => {
    try {
      const d = await apiRequest<MenuItemDetail>(`/api/v1/menu/items/${id}`);
      setDetail(d);
      setSelectedCustomizations([]);
      setQty(1);
    } catch {

    }
  };

  const toggleCustomization = (opt: CustomizationOption) => {
    setSelectedCustomizations(prev =>
      prev.some(x => x.name === opt.name)
        ? prev.filter(x => x.name !== opt.name)
        : [...prev, opt]
    );
  };

  const visibleItems = onlyFavorites
    ? items.filter(it => favoriteIds.includes(it.id))
    : items;

  return (
    <View style={styles.screen}>
      <View style={styles.filterBox}>
        <TextInput
          style={styles.searchInput}
          value={search}
          onChangeText={setSearch}
          placeholder="Search meals, snacks, drinks…"
        />
        <View style={styles.filterRow}>
          {["", "vegetarian", "vegan", "jain"].map(tag => (
            <TouchableOpacity
              key={tag || "all"}
              style={[styles.chip, dietary === tag && !onlyFavorites && styles.chipActive]}
              onPress={() => {
                setOnlyFavorites(false);
                setDietary(tag);
              }}
            >
              <Text style={[styles.chipText, dietary === tag && !onlyFavorites && styles.chipTextActive]}>
                {tag ? tag.toUpperCase() : "ALL"}
              </Text>
            </TouchableOpacity>
          ))}
          <TouchableOpacity
            style={[styles.chip, onlyFavorites && styles.chipActive]}
            onPress={() => setOnlyFavorites(v => !v)}
          >
            <Text style={[styles.chipText, onlyFavorites && styles.chipTextActive]}>★ FAVORITES</Text>
          </TouchableOpacity>
          <TextInput
            style={styles.budgetInput}
            value={maxBudgetRupees}
            onChangeText={setMaxBudgetRupees}
            keyboardType="numeric"
            placeholder="Max ₹"
          />
        </View>
      </View>

      <ScrollView contentContainerStyle={styles.list}>
        {visibleItems.map(item => {
          const isFav = favoriteIds.includes(item.id);
          return (
            <TouchableOpacity
              key={item.id}
              style={styles.card}
              onPress={() => openDetail(item.id)}
            >
              <View style={styles.rowBetween}>
                <Text style={styles.name}>{item.name}</Text>
                <TouchableOpacity onPress={() => toggleFavorite(item.id)}>
                  <Text style={styles.favStar}>{isFav ? "★" : "☆"}</Text>
                </TouchableOpacity>
                <Text style={styles.price}>{formatRupeesFromPaise(item.pricePaise)}</Text>
              </View>
              <Text style={styles.desc} numberOfLines={2}>
                {item.description || "Freshly prepared at your campus counter"}
              </Text>
              <Text style={styles.tags}>
                ~{item.preparationMinutes || 10}m · {item.stockQuantity} left ·{" "}
                {item.allergenTags?.length
                  ? `Allergens: ${item.allergenTags.join(", ")}`
                  : "Allergens unverified"}
              </Text>
              <View style={styles.actionRow}>
                <TouchableOpacity
                  style={styles.primaryBtn}
                  onPress={() => addItem(item, 1, [])}
                >
                  <Text style={styles.primaryBtnText}>Quick Add</Text>
                </TouchableOpacity>
                <TouchableOpacity
                  style={styles.secondaryBtn}
                  onPress={() => openDetail(item.id)}
                >
                  <Text style={styles.secondaryBtnText}>Details & Customize</Text>
                </TouchableOpacity>
              </View>
            </TouchableOpacity>
          );
        })}
      </ScrollView>

      <Modal visible={Boolean(detail)} animationType="slide" transparent>
        <View style={styles.modalBackdrop}>
          <View style={styles.modalCard}>
            {detail && (
              <>
                <Text style={styles.modalTitle}>{detail.name}</Text>
                <Text style={styles.price}>{formatRupeesFromPaise(detail.pricePaise)}</Text>
                <Text style={styles.desc}>{detail.description}</Text>
                <Text style={styles.tags}>
                  Verified Ingredients:{" "}
                  {detail.recipeIngredients?.map(r => r.name).join(", ") ||
                    detail.ingredientsSummary ||
                    "Ask counter staff"}
                </Text>
                <Text style={styles.tags}>
                  Allergens:{" "}
                  {detail.allergenTags?.length
                    ? detail.allergenTags.join(", ")
                    : "Unverified — confirm with staff if severely allergic"}
                </Text>

                {(detail.customizations || []).map(opt => {
                  const active = selectedCustomizations.some(c => c.name === opt.name);
                  return (
                    <TouchableOpacity
                      key={opt.name}
                      style={[styles.custRow, active && styles.custRowActive]}
                      onPress={() => toggleCustomization(opt)}
                    >
                      <Text style={styles.custText}>
                        {active ? "☑ " : "☐ "}
                        {opt.name} (+{formatRupeesFromPaise(opt.priceDeltaPaise)})
                      </Text>
                    </TouchableOpacity>
                  );
                })}

                <View style={styles.qtyRow}>
                  <TouchableOpacity
                    style={styles.secondaryBtn}
                    onPress={() => setQty(q => Math.max(1, q - 1))}
                  >
                    <Text style={styles.secondaryBtnText}>−</Text>
                  </TouchableOpacity>
                  <Text style={styles.qtyValue}>{qty}</Text>
                  <TouchableOpacity
                    style={styles.secondaryBtn}
                    onPress={() => setQty(q => Math.min(20, q + 1))}
                  >
                    <Text style={styles.secondaryBtnText}>+</Text>
                  </TouchableOpacity>
                </View>

                <View style={styles.actionRow}>
                  <TouchableOpacity
                    style={styles.secondaryBtn}
                    onPress={() => setDetail(null)}
                  >
                    <Text style={styles.secondaryBtnText}>Close</Text>
                  </TouchableOpacity>
                  <TouchableOpacity
                    style={styles.primaryBtn}
                    onPress={async () => {
                      await addItem(detail, qty, selectedCustomizations);
                      setDetail(null);
                    }}
                  >
                    <Text style={styles.primaryBtnText}>Add {qty} to Basket</Text>
                  </TouchableOpacity>
                </View>
              </>
            )}
          </View>
        </View>
      </Modal>
    </View>
  );
};

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: "#faf9f5" },
  filterBox: { padding: 14, backgroundColor: "#fff", borderBottomWidth: 1, borderColor: "#e2ebe4" },
  searchInput: {
    borderWidth: 1,
    borderColor: "#d5dfd6",
    borderRadius: 10,
    paddingHorizontal: 12,
    paddingVertical: 9,
    marginBottom: 8
  },
  filterRow: { flexDirection: "row", flexWrap: "wrap", gap: 6, alignItems: "center" },
  chip: {
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: 999,
    borderWidth: 1,
    borderColor: "#d5dfd6"
  },
  chipActive: { backgroundColor: "#12372a", borderColor: "#12372a" },
  chipText: { fontSize: 11, fontWeight: "700", color: "#365244" },
  chipTextActive: { color: "#fff" },
  budgetInput: {
    borderWidth: 1,
    borderColor: "#d5dfd6",
    borderRadius: 8,
    paddingHorizontal: 10,
    paddingVertical: 5,
    minWidth: 72,
    fontSize: 12
  },
  list: { padding: 14, paddingBottom: 32 },
  card: {
    backgroundColor: "#fff",
    borderRadius: 16,
    padding: 15,
    borderWidth: 1,
    borderColor: "#dce7de",
    marginBottom: 12
  },
  rowBetween: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  name: { fontSize: 16, fontWeight: "800", color: "#17251d", flex: 1, marginRight: 8 },
  favStar: { fontSize: 20, color: "#d97706", marginRight: 10 },
  price: { fontSize: 16, fontWeight: "800", color: "#155d3d" },
  desc: { fontSize: 13, color: "#5e6e64", marginTop: 5 },
  tags: { fontSize: 12, color: "#3f594b", marginTop: 6 },
  actionRow: { flexDirection: "row", gap: 8, marginTop: 12 },
  primaryBtn: {
    flex: 1,
    backgroundColor: "#12372a",
    paddingVertical: 11,
    borderRadius: 10,
    alignItems: "center"
  },
  primaryBtnText: { color: "#fff", fontWeight: "700", fontSize: 13 },
  secondaryBtn: {
    flex: 1,
    backgroundColor: "#eef3ef",
    paddingVertical: 11,
    borderRadius: 10,
    alignItems: "center"
  },
  secondaryBtnText: { color: "#12372a", fontWeight: "700", fontSize: 13 },
  modalBackdrop: {
    flex: 1,
    backgroundColor: "rgba(16, 32, 24, 0.55)",
    justifyContent: "flex-end"
  },
  modalCard: {
    backgroundColor: "#fff",
    borderTopLeftRadius: 22,
    borderTopRightRadius: 22,
    padding: 20
  },
  modalTitle: { fontSize: 19, fontWeight: "800", color: "#12372a", marginBottom: 4 },
  custRow: {
    marginTop: 8,
    padding: 10,
    borderRadius: 10,
    borderWidth: 1,
    borderColor: "#d5dfd6"
  },
  custRowActive: { backgroundColor: "#eef6f0", borderColor: "#26734e" },
  custText: { fontSize: 13, fontWeight: "600", color: "#17251d" },
  qtyRow: { flexDirection: "row", alignItems: "center", gap: 14, marginTop: 14 },
  qtyValue: { fontSize: 18, fontWeight: "800", color: "#12372a" }
});
