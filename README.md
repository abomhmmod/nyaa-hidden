# التورنتات المخفية في Nyaa ← تيلغرام

يفحص هذا المستودع nyaa.si كلّ 30 دقيقة على خوادم GitHub Actions (لا يحتاج إلى جهازك)،
ويرسل كلّ تورنت مخفيّ جديد إلى بوت تيلغرام ‎@Nyaahiddenbot‎.

- **السكربت:** `nyaa_hidden_torrents.py`
- **الجدولة:** `.github/workflows/scan.yml` (في الدقيقة 7 و37 من كلّ ساعة، وقد يتأخّر GitHub دقائق).
- **البيانات:** `archive.json` (الأرشيف)، `state.json` (آخر نقطة وصل إليها الفحص وما بقي منه)،
  و`التورنتات المخفية.html` (القائمة؛ نزّلها وافتحها في المتصفّح). يحدّثها الفحص بنفسه.
- **الأسرار:** `TELEGRAM_TOKEN` و`TELEGRAM_CHAT_ID` في Settings ← Secrets and variables ← Actions.

**تشغيل فوريّ:** تبويب Actions ← Scan nyaa hidden torrents ← Run workflow.
**الإيقاف:** تبويب Actions ← Scan nyaa hidden torrents ← ⋯ ← Disable workflow.

الرصيد المجّانيّ للمستودع الخاصّ نحو 2000 دقيقة في الشهر، والفحص كلّ 30 دقيقة يستهلك قرابة 1450 منها.
