import React, { useCallback, useEffect, useState } from "react";
import {
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View
} from "react-native";
import { apiRequest, formatRupeesFromPaise } from "../api/client";

interface MealPassPlanItem {
  id: number;
  planCode: string;
  name: string;
  passPeriod: string;
  totalMeals: number;
  pricePaise: number;
}

interface StudentMealPassItem {
  passId: number;
  planName: string;
  remainingMeals: number;
  totalMeals: number;
  status: string;
}

interface MealCreditItem {
  id: number;
  departmentName: string;
  programName: string;
  remainingPaise: number;
  allocatedPaise: number;
  status: string;
  expiresOn: string;
}

interface GroupOrderItem {
  id: number;
  inviteCode: string;
  title: string;
  pickupSlot: string;
  status: string;
  totalPaise: number;
}

interface SupportTicketItem {
  id: number;
  ticketNumber: string;
  subject: string;
  category: string;
  status: string;
  resolutionSummary?: string | null;
}

interface BulkOrderSummary {
  id: number;
  bulkOrderNumber: string;
  eventTitle: string;
  pickupDate: string;
  pickupSlot: string;
  status: string;
  totalRecipients: number;
}

interface BulkTokenEntry {
  recipientIdentifier: string;
  fullName: string;
  qrToken?: string | null;
  pickupStatus: string;
}

export const CampusHubScreen: React.FC = () => {
  const [refreshing, setRefreshing] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);

  const [plans, setPlans] = useState<MealPassPlanItem[]>([]);
  const [myPasses, setMyPasses] = useState<StudentMealPassItem[]>([]);
  const [mealCredits, setMealCredits] = useState<MealCreditItem[]>([]);
  const [groupOrders, setGroupOrders] = useState<GroupOrderItem[]>([]);
  const [tickets, setTickets] = useState<SupportTicketItem[]>([]);
  const [bulkOrders, setBulkOrders] = useState<BulkOrderSummary[]>([]);
  const [bulkTokens, setBulkTokens] = useState<BulkTokenEntry[]>([]);

  const [groupTitle, setGroupTitle] = useState("Lunch Squad");
  const [joinCode, setJoinCode] = useState("");
  const [ticketSubject, setTicketSubject] = useState("");
  const [ticketDesc, setTicketDesc] = useState("");

  const loadAll = useCallback(async () => {
    const [plRes, myPRes, credRes, grpRes, tckRes, blkRes] = await Promise.all([
      apiRequest<{ items?: MealPassPlanItem[] }>("/api/v1/meal-passes/plans").catch(() => ({ items: [] })),
      apiRequest<{ items?: StudentMealPassItem[] }>("/api/v1/meal-passes/me").catch(() => ({ items: [] })),
      apiRequest<{ items?: MealCreditItem[] }>("/api/v1/enterprise/finance/meal-credits/me").catch(() => ({ items: [] })),
      apiRequest<{ items?: GroupOrderItem[] }>("/api/v1/group-orders").catch(() => ({ items: [] })),
      apiRequest<{ items?: SupportTicketItem[] }>("/api/v1/support/tickets").catch(() => ({ items: [] })),
      apiRequest<{ items?: BulkOrderSummary[] }>("/api/v1/bulk-orders?limit=10").catch(() => ({ items: [] }))
    ]);
    setPlans(plRes.items || []);
    setMyPasses(myPRes.items || []);
    setMealCredits(credRes.items || []);
    setGroupOrders(grpRes.items || []);
    setTickets(tckRes.items || []);
    setBulkOrders(blkRes.items || []);
  }, []);

  useEffect(() => {
    loadAll().catch(() => null);
  }, [loadAll]);

  const onRefresh = async () => {
    setRefreshing(true);
    await loadAll();
    setRefreshing(false);
  };

  const handlePurchasePass = async (planId: number) => {
    try {
      const res = await apiRequest<{ passId: number; status: string }>(`/api/v1/meal-passes/${planId}/purchase`, {
        method: "POST"
      });
      setStatusMessage(`Meal Pass #${res.passId} created (${res.status}).`);
      await loadAll();
    } catch (err) {
      setStatusMessage(err instanceof Error ? err.message : "Could not purchase meal pass");
    }
  };

  const handleCreateGroupOrder = async () => {
    if (!groupTitle.trim()) return;
    try {
      const res = await apiRequest<{ inviteCode: string }>("/api/v1/group-orders", {
        method: "POST",
        body: JSON.stringify({
          title: groupTitle.trim(),
          pickupSlot: "12:45-13:00",
          closesInMinutes: 25
        })
      });
      setStatusMessage(`Group Order Created · Share code ${res.inviteCode}`);
      await loadAll();
    } catch (err) {
      setStatusMessage(err instanceof Error ? err.message : "Could not create group order");
    }
  };

  const handleInspectGroupCode = async () => {
    if (!joinCode.trim()) return;
    try {
      const res = await apiRequest<{ inviteCode: string; title: string; totalPaise: number; items?: unknown[] }>(
        `/api/v1/group-orders/${joinCode.trim().toUpperCase()}`
      );
      setStatusMessage(
        `Group ${res.inviteCode} (${res.title}): ${formatRupeesFromPaise(res.totalPaise || 0)} · ${(res.items || []).length} items`
      );
    } catch (err) {
      setStatusMessage(err instanceof Error ? err.message : "Group invite code not found");
    }
  };

  const handleSubmitTicket = async () => {
    if (ticketSubject.trim().length < 3 || ticketDesc.trim().length < 5) {
      setStatusMessage("Please provide ticket subject and description.");
      return;
    }
    try {
      const res = await apiRequest<{ ticketNumber: string; status: string }>("/api/v1/support/tickets", {
        method: "POST",
        body: JSON.stringify({
          category: "order",
          subject: ticketSubject.trim(),
          description: ticketDesc.trim()
        })
      });
      setTicketSubject("");
      setTicketDesc("");
      setStatusMessage(`Support Ticket ${res.ticketNumber} opened (${res.status}).`);
      await loadAll();
    } catch (err) {
      setStatusMessage(err instanceof Error ? err.message : "Could not submit ticket");
    }
  };

  const handleLoadBulkTokens = async (bulkOrderId: number) => {
    try {
      const res = await apiRequest<{ tokens?: BulkTokenEntry[] }>(`/api/v1/bulk-orders/${bulkOrderId}/tokens?limit=15`);
      setBulkTokens(res.tokens || []);
      setStatusMessage(`Loaded ${(res.tokens || []).length} bulk QR token(s) for Bulk Order #${bulkOrderId}.`);
    } catch (err) {
      setStatusMessage(err instanceof Error ? err.message : "Could not load bulk QR tokens");
    }
  };

  return (
    <ScrollView
      style={styles.screen}
      contentContainerStyle={styles.content}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} />}
    >
      <Text style={styles.heading}>Campus Hub: Passes, Groups &amp; Support</Text>
      {statusMessage && <Text style={styles.banner}>{statusMessage}</Text>}

      {/* 1. Meal Passes & Institutional Meal Credits */}
      <Text style={styles.sectionTitle}>Campus Meal Passes &amp; Scholarship Credits</Text>
      {mealCredits.map(c => (
        <View key={c.id} style={styles.card}>
          <Text style={styles.cardTitle}>{c.departmentName} · {c.programName}</Text>
          <Text style={styles.meta}>
            Remaining {formatRupeesFromPaise(c.remainingPaise)} / {formatRupeesFromPaise(c.allocatedPaise)} · Expires {c.expiresOn} ({c.status})
          </Text>
        </View>
      ))}
      {myPasses.map(mp => (
        <View key={mp.passId} style={styles.card}>
          <Text style={styles.cardTitle}>My Pass: {mp.planName}</Text>
          <Text style={styles.meta}>
            {mp.remainingMeals} / {mp.totalMeals} meals left · Status: {mp.status.toUpperCase()}
          </Text>
        </View>
      ))}
      {plans.map(pl => (
        <View key={pl.id} style={styles.card}>
          <View style={styles.rowBetween}>
            <View>
              <Text style={styles.cardTitle}>{pl.name}</Text>
              <Text style={styles.meta}>{pl.totalMeals} meals · {formatRupeesFromPaise(pl.pricePaise)}</Text>
            </View>
            <TouchableOpacity style={styles.actionBtn} onPress={() => handlePurchasePass(pl.id)}>
              <Text style={styles.actionBtnText}>Buy Pass</Text>
            </TouchableOpacity>
          </View>
        </View>
      ))}

      {/* 2. Group Ordering */}
      <Text style={styles.sectionTitle}>Collaborative Group Ordering</Text>
      <View style={styles.card}>
        <TextInput
          style={styles.input}
          value={groupTitle}
          onChangeText={setGroupTitle}
          placeholder="Group order title"
        />
        <TouchableOpacity style={styles.primaryBtn} onPress={handleCreateGroupOrder}>
          <Text style={styles.primaryBtnText}>Create Group Order Invite Code</Text>
        </TouchableOpacity>

        <View style={styles.rowGap}>
          <TextInput
            style={[styles.input, styles.flex1]}
            value={joinCode}
            onChangeText={setJoinCode}
            autoCapitalize="characters"
            placeholder="Check invite code (GRP-...)"
          />
          <TouchableOpacity style={styles.actionBtn} onPress={handleInspectGroupCode}>
            <Text style={styles.actionBtnText}>Inspect</Text>
          </TouchableOpacity>
        </View>
        {groupOrders.map(g => (
          <Text key={g.id} style={styles.meta}>
            {g.inviteCode} · {g.title} · {formatRupeesFromPaise(g.totalPaise)} ({g.status})
          </Text>
        ))}
      </View>

      {/* 3. Bulk QR Token Wallet */}
      <Text style={styles.sectionTitle}>Bulk Order QR Token Wallet</Text>
      <View style={styles.card}>
        {bulkOrders.length === 0 ? (
          <Text style={styles.meta}>No bulk orders visible for this account.</Text>
        ) : (
          bulkOrders.map(b => (
            <View key={b.id} style={styles.rowBetween}>
              <View>
                <Text style={styles.cardTitle}>{b.bulkOrderNumber} · {b.eventTitle}</Text>
                <Text style={styles.meta}>{b.pickupDate} ({b.pickupSlot}) · {b.status}</Text>
              </View>
              <TouchableOpacity style={styles.actionBtn} onPress={() => handleLoadBulkTokens(b.id)}>
                <Text style={styles.actionBtnText}>Tokens</Text>
              </TouchableOpacity>
            </View>
          ))
        )}
        {bulkTokens.map((t, idx) => (
          <Text key={`${t.recipientIdentifier}-${idx}`} style={styles.tokenLine}>
            {t.recipientIdentifier} ({t.fullName}): {t.qrToken || "No raw token"} [{t.pickupStatus}]
          </Text>
        ))}
      </View>

      {/* 4. Customer Support Tickets */}
      <Text style={styles.sectionTitle}>Support &amp; Complaint Desk</Text>
      <View style={styles.card}>
        <TextInput
          style={styles.input}
          value={ticketSubject}
          onChangeText={setTicketSubject}
          placeholder="Issue subject"
        />
        <TextInput
          style={styles.input}
          value={ticketDesc}
          onChangeText={setTicketDesc}
          placeholder="Describe order or pickup issue…"
        />
        <TouchableOpacity style={styles.primaryBtn} onPress={handleSubmitTicket}>
          <Text style={styles.primaryBtnText}>Submit Support Ticket</Text>
        </TouchableOpacity>
        {tickets.slice(0, 6).map(t => (
          <View key={t.id} style={styles.ticketRow}>
            <Text style={styles.cardTitle}>{t.ticketNumber} · {t.subject}</Text>
            <Text style={styles.meta}>
              Status: {t.status}{t.resolutionSummary ? ` · ${t.resolutionSummary}` : ""}
            </Text>
          </View>
        ))}
      </View>
    </ScrollView>
  );
};

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: "#faf9f5" },
  content: { padding: 16, paddingBottom: 32 },
  heading: { fontSize: 20, fontWeight: "800", color: "#12372a", marginBottom: 10 },
  banner: {
    backgroundColor: "#e6f5ec",
    color: "#114b2d",
    padding: 10,
    borderRadius: 10,
    fontWeight: "700",
    marginBottom: 12
  },
  sectionTitle: { fontSize: 15, fontWeight: "800", color: "#12372a", marginTop: 12, marginBottom: 6 },
  card: {
    backgroundColor: "#fff",
    borderRadius: 14,
    padding: 14,
    borderWidth: 1,
    borderColor: "#dce7de",
    marginBottom: 10
  },
  cardTitle: { fontWeight: "800", fontSize: 14, color: "#17251d" },
  meta: { fontSize: 12, color: "#5e6e64", marginTop: 3 },
  rowBetween: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", marginBottom: 6 },
  rowGap: { flexDirection: "row", gap: 8, marginTop: 10, alignItems: "center" },
  flex1: { flex: 1, marginBottom: 0 },
  input: {
    backgroundColor: "#fafbf9",
    borderWidth: 1,
    borderColor: "#d5dfd6",
    borderRadius: 10,
    paddingHorizontal: 10,
    paddingVertical: 8,
    marginBottom: 8
  },
  primaryBtn: { backgroundColor: "#12372a", paddingVertical: 10, borderRadius: 10, alignItems: "center" },
  primaryBtnText: { color: "#fff", fontWeight: "800", fontSize: 13 },
  actionBtn: { backgroundColor: "#26734e", paddingHorizontal: 12, paddingVertical: 8, borderRadius: 8 },
  actionBtnText: { color: "#fff", fontWeight: "700", fontSize: 12 },
  tokenLine: { fontSize: 11, color: "#12372a", marginTop: 4, fontWeight: "700" },
  ticketRow: { borderTopWidth: 1, borderColor: "#ecf2ed", paddingTop: 8, marginTop: 8 }
});
