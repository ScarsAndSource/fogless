# Android WebView reference (NOT compiled or run by the author: build and test it yourself)

Why it exists: Fogless uses cookie sessions, `prompt()` (delete account, change category), the microphone and file
downloads (CSV/JSON export). A bare WebView supports none of those without the code in `MainActivity.kt`.

1. Replace `APP_URL` in MainActivity.kt with your deployed https URL (no trailing slash).
2. Merge the manifest snippet. Keep `usesCleartextTraffic="false"` and `allowBackup="false"`.
3. The app must load that live origin. Do not bundle the HTML locally: the server rejects other Origins and cookies would not stick.
4. Test on a real phone: sign up, log an expense, voice note, Export CSV, Delete Account prompt, rotate screen, back button.
5. Password-reset emails open in the phone browser; that is expected.
