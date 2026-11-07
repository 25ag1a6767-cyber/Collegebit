import React, { createContext, useCallback, useContext, useState } from "react";
import { apiRequest } from "../api/client";
import { CartLineInput, CustomizationOption, MenuItemSummary, OrderQuote } from "../types/api";
import { useAuth } from "./AuthContext";

interface CartContextValue {
  lines: CartLineInput[];
  selectedCounterId: number | null;
  setSelectedCounterId: (id: number | null) => void;
  discountCode: string;
  setDiscountCode: (code: string) => void;
  quote: OrderQuote | null;
  addItem: (item: MenuItemSummary, quantity?: number, customizations?: CustomizationOption[]) => Promise<void>;
  updateQuantity: (menuItemId: number, quantity: number) => Promise<void>;
  clearCart: () => Promise<void>;
  refreshQuote: (pickupSlot?: string | null) => Promise<OrderQuote | null>;
}

const CartContext = createContext<CartContextValue | null>(null);

export const CartProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { user } = useAuth();
  const [lines, setLines] = useState<CartLineInput[]>([]);
  const [selectedCounterId, setSelectedCounterId] = useState<number | null>(null);
  const [discountCode, setDiscountCode] = useState<string>("");
  const [quote, setQuote] = useState<OrderQuote | null>(null);

  const addItem = useCallback(
    async (item: MenuItemSummary, quantity = 1, customizations: CustomizationOption[] = []) => {
      const custMap: Record<string, unknown> = {};
      for (const c of customizations) custMap[c.name] = true;
      let nextQty = quantity;
      setLines(prev => {
        const existing = prev.find(l => l.menuItemId === item.id);
        if (existing) {
          nextQty = Math.min(50, existing.quantity + quantity);
          return prev.map(l =>
            l.menuItemId === item.id ? { ...l, quantity: nextQty, customizations: custMap } : l
          );
        }
        return [
          ...prev,
          {
            menuItemId: item.id,
            name: item.name,
            unitPricePaise: item.pricePaise,
            quantity,
            customizations: custMap
          }
        ];
      });
      if (item.counterId && !selectedCounterId) {
        setSelectedCounterId(item.counterId);
      }
      if (user?.role === "student") {
        try {
          await apiRequest(`/api/v1/cart/items/${item.id}`, {
            method: "PUT",
            body: JSON.stringify({ quantity: nextQty, customizations: custMap })
          });
        } catch {

        }
      }
    },
    [selectedCounterId, user?.role]
  );

  const updateQuantity = useCallback(
    async (menuItemId: number, quantity: number) => {
      setQuote(null);
      if (quantity <= 0) {
        setLines(prev => prev.filter(l => l.menuItemId !== menuItemId));
        if (user?.role === "student") {
          await apiRequest(`/api/v1/cart/items/${menuItemId}`, { method: "DELETE" }).catch(() => null);
        }
        return;
      }
      setLines(prev => prev.map(l => (l.menuItemId === menuItemId ? { ...l, quantity } : l)));
      if (user?.role === "student") {
        await apiRequest(`/api/v1/cart/items/${menuItemId}`, {
          method: "PUT",
          body: JSON.stringify({ quantity, customizations: {} })
        }).catch(() => null);
      }
    },
    [user?.role]
  );

  const clearCart = useCallback(async () => {
    setLines([]);
    setQuote(null);
    if (user?.role === "student") {
      await apiRequest("/api/v1/cart", { method: "DELETE" }).catch(() => null);
    }
  }, [user?.role]);

  const refreshQuote = useCallback(
    async (pickupSlot?: string | null): Promise<OrderQuote | null> => {
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
            customizations: l.customizations
          })),
          counterId: selectedCounterId,
          couponCode: discountCode.trim() || null,
          pickupSlot: pickupSlot || null
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
