export interface ApiErrorShape {
  code: string;
  message: string;
  details?: unknown;
}

export interface ApiEnvelope<T> {
  success: boolean;
  data?: T;
  error?: ApiErrorShape | null;
  requestId?: string;
}

export type UserRole = "student" | "staff" | "admin";

export interface UserProfile {
  id: number;
  fullName: string;
  email: string;
  phone: string | null;
  role: UserRole;
  isActive: boolean;
  emailVerified: boolean;
  mfaEnabled: boolean;
  dietaryPreference: string | null;
  allergenPreferences: string[];
  counterIds: number[];
}

export interface AuthTokenResponse {
  accessToken?: string;
  refreshToken?: string;
  tokenType?: string;
  expiresIn?: number;
  mfaRequired?: boolean;
  mfaChallengeToken?: string;
  user?: UserProfile;
}

export interface CounterSummary {
  id: number;
  name: string;
  code: string;
  description?: string | null;
  isActive: boolean;
  isOpen?: boolean;
  activeOrders?: number;
  estimatedWaitMinutes?: number;
  prepSlaMinutes?: number;
}

export interface CustomizationOption {
  name: string;
  priceDeltaPaise: number;
}

export interface MenuItemSummary {
  id: number;
  categoryId: number;
  categoryName?: string;
  counterId?: number | null;
  name: string;
  description?: string | null;
  itemType: "food" | "stationery";
  pricePaise: number;
  stockQuantity: number;
  preparationMinutes: number;
  isAvailable: boolean;
  imageUrl?: string | null;
  dietaryTags: string[];
  allergenTags: string[];
  customizations: CustomizationOption[];
}

export interface MenuItemDetail extends MenuItemSummary {
  ingredientsSummary?: string | null;
  recipeIngredients?: Array<{
    ingredientId: number;
    name: string;
    quantityPerServing: number;
    unit: string;
    allergens: string[];
  }>;
  ratingSummary?: {
    averageRating: number | null;
    reviewCount: number;
  };
}

export interface ActiveDiscount {
  id: number;
  code: string;
  title: string;
  description?: string | null;
  discountType: "percentage" | "fixed";
  valueBps?: number | null;
  valuePaise?: number | null;
  minOrderPaise: number;
}

export interface CartLineInput {
  menuItemId: number;
  name: string;
  unitPricePaise: number;
  quantity: number;
  customizations: Record<string, unknown>;
}

export interface OrderQuote {
  subtotalPaise: number;
  discountPaise: number;
  taxPaise: number;
  totalPaise: number;
  discountCode?: string | null;
  items: Array<{
    menuItemId: number;
    name: string;
    quantity: number;
    unitPricePaise: number;
    lineTotalPaise: number;
  }>;
}

export interface OrderRecord {
  id: number;
  tokenNumber: number | null;
  status: string;
  paymentStatus: string;
  counterId: number | null;
  subtotalPaise: number;
  discountPaise: number;
  taxPaise: number;
  totalPaise: number;
  pickupSlot: string | null;
  notes: string | null;
  createdAt: string;
  estimatedReadyAt?: string | null;
  items: Array<{
    menuItemId: number;
    name: string;
    quantity: number;
    unitPricePaise: number;
    lineTotalPaise: number;
    customizations?: CustomizationOption[];
  }>;
}

export interface UpiPaymentIntent {
  paymentId: number;
  orderId: number;
  gatewayOrderId: string;
  keyId: string;
  amountPaise: number;
  currency: string;
  upiIntentUri?: string;
}

export interface PickupTokenResponse {
  orderId: number;
  pickupToken: string;
  qrUrl: string;
  manualPickupCode: string;
  expiresAt: string;
}

export interface AIRecommendationEntry {
  item: MenuItemSummary;
  score: number;
  reasons: string[];
}

export interface AIAssistantSuggestion {
  id: number;
  name: string;
  pricePaise: number;
  preparationMinutes: number;
  allergenVerified?: boolean;
  allergenNotice?: string | null;
}

export interface AIAssistantResponse {
  answer: string;
  suggestedItems: AIAssistantSuggestion[];
  dietaryWarnings?: string[];
  requiresConfirmation: boolean;
  autoAddedToCart: boolean;
}

export interface NotificationItem {
  id: number;
  title: string;
  message: string;
  isRead: boolean;
  createdAt: string;
}
