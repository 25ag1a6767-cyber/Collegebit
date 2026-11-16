# CollegeBits — Native Mobile App for Android & iPhone (iOS)
*(Expo + React Native + TypeScript — one codebase, both platforms)*

The student app connects to the shared CollegeBits backend (/api/v1 REST + /api/v1/ws). Menus, stock reservations, UPI payments, pickup tokens and live order tracking stay synchronized with the kitchen.

## 1. How students install the app

| Platform | Install method | Cost |
| --- | --- | --- |
| Android | Direct APK: https://app-sai-e3e1.vercel.app/download/app.apk | Free |
| Android | Chrome → /customer/ → Install app (PWA) | Free |
| iPhone | Safari → /customer/ → Share → Add to Home Screen (PWA) | Free |
| iPhone | TestFlight / App Store (native IPA) | Apple Developer Program, US\/year |
| iPhone | Sideload unsigned IPA with Sideloadly/AltStore + free Apple ID | Free, re-sign every 7 days |

## 2. Automated builds (GitHub Actions)
- **Android** — .github/workflows/android-build.yml builds CollegeBits-Customer.apk and publishes it to the customer-app-latest release.
- **iOS** — .github/workflows/ios-build.yml builds the native iOS app on macOS and publishes CollegeBits-iOS-unsigned.ipa to the same release.
- **iOS TestFlight (optional)** — the same workflow runs a signed EAS build and auto-submits to TestFlight once the EXPO_TOKEN repository secret is set.

### Enabling TestFlight / App Store
1. Join the Apple Developer Program (developer.apple.com/programs).
2. Create an Expo account and run 
px eas-cli login then 
px eas-cli build:configure inside mobile/.
3. Run 
px eas-cli credentials once to let EAS create the iOS certificate and provisioning profile.
4. Create an App Store Connect app with bundle ID com.acecanteen.campus.
5. Add an Expo access token as the GitHub secret EXPO_TOKEN. Every push to mobile/** then builds and submits to TestFlight.
6. Share the public TestFlight link with students, or submit for App Store review.

## 3. Local development
`powershell
Set-Location mobile
npm install
npx expo start   # scan QR with Expo Go (iPhone camera or Android Expo Go)
`
Set EXPO_PUBLIC_API_URL to the backend URL; the default fallback is the Android emulator localhost.

---

## 2. Security Architecture
- **Zero embedded secrets:** Razorpay keys and AI tokens reside strictly on the backend.
- **Hardware-backed keystore:** Short-lived JWTs and refresh tokens are stored in Android Keystore / iOS Keychain via `expo-secure-store`.
- **Single-use pickup pass:** QR codes and backup codes are signed server-side and invalidated atomically upon counter handover.
