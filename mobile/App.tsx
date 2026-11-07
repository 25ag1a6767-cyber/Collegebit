import { StatusBar } from "expo-status-bar";
import React, { useState } from "react";
import { ActivityIndicator, SafeAreaView, StyleSheet, Text, TouchableOpacity, View } from "react-native";
import { AuthProvider, useAuth } from "./src/context/AuthContext";
import { CartProvider, useCart } from "./src/context/CartContext";
import { AIAssistantScreen } from "./src/screens/AIAssistantScreen";
import { AuthScreen } from "./src/screens/AuthScreen";
import { CartCheckoutScreen } from "./src/screens/CartCheckoutScreen";
import { HomeScreen } from "./src/screens/HomeScreen";
import { MenuScreen } from "./src/screens/MenuScreen";
import { OrdersTrackingScreen } from "./src/screens/OrdersTrackingScreen";
import { ProfileScreen } from "./src/screens/ProfileScreen";

type TabKey = "home" | "menu" | "cart" | "orders" | "ai" | "profile";

const Shell: React.FC = () => {
  const { user, loading } = useAuth();
  const { lines } = useCart();
  const [tab, setTab] = useState<TabKey>("home");

  if (loading) {
    return (
      <SafeAreaView style={styles.centered}>
        <ActivityIndicator size="large" color="#12372a" />
      </SafeAreaView>
    );
  }

  if (!user) {
    return (
      <SafeAreaView style={styles.root}>
        <StatusBar style="dark" />
        <AuthScreen />
      </SafeAreaView>
    );
  }

  const cartCount = lines.reduce((sum, l) => sum + l.quantity, 0);

  return (
    <SafeAreaView style={styles.root}>
      <StatusBar style="dark" />
      <View style={styles.body}>
        {tab === "home" && <HomeScreen onNavigate={next => setTab(next)} />}
        {tab === "menu" && <MenuScreen />}
        {tab === "cart" && <CartCheckoutScreen onOrderCreated={() => setTab("orders")} />}
        {tab === "orders" && <OrdersTrackingScreen />}
        {tab === "ai" && <AIAssistantScreen />}
        {tab === "profile" && <ProfileScreen />}
      </View>

      <View style={styles.tabBar}>
        {(
          [
            ["home", "Home"],
            ["menu", "Menu"],
            ["cart", cartCount > 0 ? `Cart (${cartCount})` : "Cart"],
            ["orders", "Orders"],
            ["ai", "AI"],
            ["profile", "Profile"]
          ] as Array<[TabKey, string]>
        ).map(([key, label]) => {
          const active = tab === key;
          return (
            <TouchableOpacity
              key={key}
              style={styles.tabItem}
              onPress={() => setTab(key)}
            >
              <Text style={[styles.tabLabel, active && styles.tabLabelActive]}>{label}</Text>
            </TouchableOpacity>
          );
        })}
      </View>
    </SafeAreaView>
  );
};

export default function App() {
  return (
    <AuthProvider>
      <CartProvider>
        <Shell />
      </CartProvider>
    </AuthProvider>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: "#faf9f5" },
  centered: { flex: 1, justifyContent: "center", alignItems: "center", backgroundColor: "#faf9f5" },
  body: { flex: 1 },
  tabBar: {
    flexDirection: "row",
    backgroundColor: "#ffffff",
    borderTopWidth: 1,
    borderColor: "#dde7df",
    paddingVertical: 10
  },
  tabItem: { flex: 1, alignItems: "center", justifyContent: "center" },
  tabLabel: { fontSize: 11, fontWeight: "700", color: "#64756b" },
  tabLabelActive: { color: "#12372a", fontWeight: "800" }
});
