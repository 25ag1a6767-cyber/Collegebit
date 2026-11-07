import React, { useState } from "react";
import {
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View
} from "react-native";
import { apiRequest, formatRupeesFromPaise } from "../api/client";
import { useCart } from "../context/CartContext";
import { AIAssistantResponse, AIAssistantSuggestion } from "../types/api";

interface ChatTurn {
  role: "user" | "assistant";
  text: string;
  suggestions?: AIAssistantSuggestion[];
}

export const AIAssistantScreen: React.FC = () => {
  const { selectedCounterId, addItem } = useCart();
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [turns, setTurns] = useState<ChatTurn[]>([
    {
      role: "assistant",
      text: "Ask me what you can get within your budget, prep time, or dietary preferences. I only recommend verified in-stock menu items and never add to your cart without confirmation."
    }
  ]);

  const askAssistant = async (promptText: string) => {
    const trimmed = promptText.trim();
    if (!trimmed || busy) return;
    setQuery("");
    setBusy(true);
    setTurns(prev => [...prev, { role: "user", text: trimmed }]);
    try {
      const res = await apiRequest<AIAssistantResponse>("/api/v1/ai/assistant", {
        method: "POST",
        body: JSON.stringify({
          message: trimmed,
          counterId: selectedCounterId
        })
      });
      setTurns(prev => [
        ...prev,
        {
          role: "assistant",
          text: res.answer,
          suggestions: res.suggestedItems || []
        }
      ]);
    } catch (err) {
      setTurns(prev => [
        ...prev,
        {
          role: "assistant",
          text: err instanceof Error ? err.message : "AI menu assistant is temporarily unavailable."
        }
      ]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <View style={styles.screen}>
      <View style={styles.chipsRow}>
        {[
          "What can I get under ₹80?",
          "Quick vegetarian snack under 10 min",
          "Dairy-free options in stock"
        ].map(preset => (
          <TouchableOpacity
            key={preset}
            style={styles.presetChip}
            onPress={() => askAssistant(preset)}
          >
            <Text style={styles.presetText}>{preset}</Text>
          </TouchableOpacity>
        ))}
      </View>

      <ScrollView style={styles.chatArea} contentContainerStyle={styles.chatContent}>
        {turns.map((turn, idx) => (
          <View
            key={idx}
            style={[styles.bubble, turn.role === "user" ? styles.userBubble : styles.botBubble]}
          >
            <Text style={turn.role === "user" ? styles.userText : styles.botText}>{turn.text}</Text>
            {turn.suggestions?.map(sug => (
              <View key={sug.id} style={styles.sugCard}>
                <Text style={styles.sugTitle}>
                  {sug.name} · {formatRupeesFromPaise(sug.pricePaise)}
                </Text>
                <Text style={styles.sugReason}>
                  {sug.allergenNotice || "Available now"} · ~{sug.preparationMinutes}m prep
                </Text>
                {!sug.allergenVerified && (
                  <Text style={styles.warning}>
                    ⚠️ {sug.allergenNotice || "Allergen info unverified — confirm with staff."}
                  </Text>
                )}
                <TouchableOpacity
                  style={styles.confirmBtn}
                  onPress={() =>
                    addItem(
                      {
                        id: sug.id,
                        categoryId: 0,
                        name: sug.name,
                        itemType: "food",
                        pricePaise: sug.pricePaise,
                        stockQuantity: 10,
                        preparationMinutes: sug.preparationMinutes,
                        isAvailable: true,
                        dietaryTags: [],
                        allergenTags: [],
                        customizations: []
                      },
                      1,
                      []
                    )
                  }
                >
                  <Text style={styles.confirmText}>Confirm & Add to Basket</Text>
                </TouchableOpacity>
              </View>
            ))}
          </View>
        ))}
      </ScrollView>

      <View style={styles.inputBar}>
        <TextInput
          style={styles.input}
          value={query}
          onChangeText={setQuery}
          placeholder="Ask about budget, allergens, or prep time…"
        />
        <TouchableOpacity
          style={styles.sendBtn}
          onPress={() => askAssistant(query)}
          disabled={busy}
        >
          <Text style={styles.sendText}>{busy ? "…" : "Ask"}</Text>
        </TouchableOpacity>
      </View>
    </View>
  );
};

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: "#faf9f5" },
  chipsRow: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 6,
    padding: 12,
    backgroundColor: "#fff",
    borderBottomWidth: 1,
    borderColor: "#e2ebe4"
  },
  presetChip: {
    backgroundColor: "#eef6f0",
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: 999
  },
  presetText: { fontSize: 11, fontWeight: "700", color: "#195838" },
  chatArea: { flex: 1 },
  chatContent: { padding: 14, gap: 10 },
  bubble: { padding: 13, borderRadius: 14, maxWidth: "92%" },
  userBubble: { alignSelf: "flex-end", backgroundColor: "#12372a" },
  botBubble: {
    alignSelf: "flex-start",
    backgroundColor: "#fff",
    borderWidth: 1,
    borderColor: "#dce7de"
  },
  userText: { color: "#fff", fontSize: 14 },
  botText: { color: "#17251d", fontSize: 14, lineHeight: 20 },
  sugCard: {
    marginTop: 10,
    padding: 10,
    borderRadius: 10,
    backgroundColor: "#f6faf7",
    borderWidth: 1,
    borderColor: "#d5e4da"
  },
  sugTitle: { fontWeight: "800", color: "#12372a", fontSize: 14 },
  sugReason: { fontSize: 12, color: "#4f6257", marginTop: 2 },
  warning: { fontSize: 11, color: "#b42318", marginTop: 4, fontWeight: "600" },
  confirmBtn: {
    marginTop: 8,
    backgroundColor: "#12372a",
    paddingVertical: 8,
    borderRadius: 8,
    alignItems: "center"
  },
  confirmText: { color: "#fff", fontWeight: "700", fontSize: 12 },
  inputBar: {
    flexDirection: "row",
    gap: 8,
    padding: 12,
    backgroundColor: "#fff",
    borderTopWidth: 1,
    borderColor: "#e2ebe4"
  },
  input: {
    flex: 1,
    borderWidth: 1,
    borderColor: "#cfdcd2",
    borderRadius: 10,
    paddingHorizontal: 12,
    paddingVertical: 9
  },
  sendBtn: {
    backgroundColor: "#12372a",
    paddingHorizontal: 18,
    borderRadius: 10,
    justifyContent: "center"
  },
  sendText: { color: "#fff", fontWeight: "800" }
});
