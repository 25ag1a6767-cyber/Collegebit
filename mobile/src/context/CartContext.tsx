import React, { createContext, useCallback, useContext, useRef, useState } from "react";
import { apiRequest } from "../api/client";
import { CartLineInput, CustomizationOption, FulfillmentMode, MenuItemSummary, OrderQuote } from "../types/api";
import { useAuth } from "./AuthContext";

interface CartContextValue {
  lines: CartLineInput[];
  selectedCounterId: number | null;
  setSelectedCounterId: (id: number | null) => void;
  discountCode: string;
  setDiscountCode: (code: string) => void;
  quote: OrderQuote | null;
  addItem: (item: MenuItemSummary, quantity?: number, customizations?: CustomizationOption[], fulfillmentType?: FulfillmentMode) => Promise<void>;
  updateQuantity: (menuItemId: number, quantity: number) => Promise<void>;
  setFulfillmentType: (menuItemId: number, fulfillmentType: FulfillmentMode) => Promise<void>;
  clearCart: () => Promise<void>;
  refreshQuote: (pickupSlot?: string | null, loyaltyPointsRedeemed?: number) => Promise<OrderQuote | null>;
}

const CartContext = createContext<CartContextValue | null>(null);

export const CartProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { user } = useAuth();
  const [lines, setLines] = useState<CartLineInput[]>([]);
  const linesRef = useRef<CartLineInput[]>([]);
  const [selectedCounterId, setSelectedCounterId] = useState<number | null>(null);
  const [discountCode, setDiscountCode] = useState<string>("");
  const [quote, setQuote] = useState<OrderQuote | null>(null);

  const commitLines = useCallback((nextLines: CartLineInput[]) => {
    linesRef.current = nextLines;
    setLines(nextLines);
    setQuote(null);
  }, []);

  const addItem = useCallback(
    async (
      item: MenuItemSummary,
      quantity = 1,
      customizations: CustomizationOption[] = [],
      fulfillmentType: FulfillmentMode = "READY_TO_EAT"
    ) => {
      const custMap: Record<string, unknown> = {};
      for (const c of customizations) custMap[c.name] = true;
      const prev = linesRef.current;
      const existing = prev.find(l => l.menuItemId === item.id);
      const nextQty = existing ? Math.min(50, existing.quantity + quantity) : quantity;
      const nextMode = existing ? existing.fulfillmentType || fulfillmentType : fulfillmentType;
      const nextLines = existing
        ? prev.map(l =>
            l.menuItemId === item.id
              ? { ...l, quantity: nextQty, customizations: custMap, fulfillmentType: nextMode }
              : l
          )
        : [
            ...prev,
            {
              menuItemId: item.id,
              name: item.name,
              unitPricePaise: item.pricePaise,
              quantity: nextQty,
              customizations: custMap,
              fulfillmentType: nextMode
            }
          ];
      commitLines(nextLines);
      if (item.counterId && !selectedCounterId) {
        setSelectedCounterId(item.counterId);
      }
      if (user?.role === "student") {
        try {
          await apiRequest(`/api/v1/cart/items/${item.id}`, {
            method: "PUT",
            body: JSON.stringify({ quantity: nextQty, customizations: custMap, fulfillmentType: nextMode })
          });
        } catch {
          // Local state stays responsive; authoritative check runs at quote/checkout
        }
      }
    },
    [commitLines, selectedCounterId, user?.role]
  );

  const updateQuantity = useCallback(
    async (menuItemId: number, quantity: number) => {
      const prev = linesRef.current;
      if (quantity <= 0) {
        commitLines(prev.filter(l => l.menuItemId !== menuItemId));
        if (user?.role === "student") {
          await apiRequest(`/api/v1/cart/items/${menuItemId}`, { method: "DELETE" }).catch(() => null);
        }
        return;
      }
      const existing = prev.find(l => l.menuItemId === menuItemId);
      const mode: FulfillmentMode = existing?.fulfillmentType || "READY_TO_EAT";
      const custMap: Record<string, unknown> = existing?.customizations || {};
      commitLines(prev.map(l => (l.menuItemId === menuItemId ? { ...l, quantity } : l)));
      if (user?.role === "student") {
        await apiRequest(`/api/v1/cart/items/${menuItemId}`, {
          method: "PUT",
          body: JSON.stringify({ quantity, customizations: custMap, fulfillmentType: mode })
        }).catch(() => null);
      }
    },
    [commitLines, user?.role]
  );

  const setFulfillmentType = useCallback(
    async (menuItemId: number, fulfillmentType: FulfillmentMode) => {
      const prev = linesRef.current;
      const existing = prev.find(l => l.menuItemId === menuItemId);
      const currentQty = existing?.quantity ?? 1;
      const currentCust = existing?.customizations ?? {};
      commitLines(prev.map(l => (l.menuItemId === menuItemId ? { ...l, fulfillmentType } : l)));
      if (user?.role === "student") {
        await apiRequest(`/api/v1/cart/items/${menuItemId}`, {
          method: "PUT",
          body: JSON.stringify({ quantity: currentQty, customizations: currentCust, fulfillmentType })
        }).catch(() => null);
      }
    },
    [commitLines, user?.role]
  );

  const clearCart = useCallback(async () => {
    commitLines([]);
    if (user?.role === "student") {
      await apiRequest("/api/v1/cart", { method: "DELETE" }).catch(() => null);
    }
  }, [commitLines, user?.role]);

  const refreshQuote = useCallback(
    async (pickupSlot?: string | null, loyaltyPointsRedeemed = 0): Promise<OrderQuote | null> => {
      if (!lines.length || user?.role !== "student") {
        setQuote(null);
        return null;
      }
      const q = await apiRequest<OrderQuote>("/api/v1/orders/quote", {
        method: "POST",
        body: JSON.stringify({
          items: lines.map(l => ({
            menuItemId: l.menuItemId,
            quantity: l.quantity,
            customizations: l.customizations,
            fulfillmentType: l.fulfillmentType || "READY_TO_EAT"
          })),
          counterId: selectedCounterId,
          couponCode: discountCode.trim() || null,
          pickupSlot: pickupSlot || null,
          loyaltyPointsRedeemed
        })
      });
      setQuote(q);
      return q;
    },
    [discountCode, lines, selectedCounterId, user?.role]
  );

  return (
    <CartContext.Provider
      value={{
        lines,
        selectedCounterId,
        setSelectedCounterId,
        discountCode,
        setDiscountCode,
        quote,
        addItem,
        updateQuantity,
        setFulfillmentType,
        clearCart,
        refreshQuote
      }}
    >
      {children}
    </CartContext.Provider>
  );
};

export function useCart(): CartContextValue {
  const ctx = useContext(CartContext);
  if (!ctx) throw new Error("useCart must be used inside CartProvider");
  return ctx;
}
