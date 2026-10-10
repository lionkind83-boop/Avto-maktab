АВТОМАКТАБ — RENDER FREE + SUPABASE FREE
=======================================

МАҚСАД: Бепул СИНОВ муҳити. Реал молиявий ҳисоб учун 24/7 SLA, backup ва аудит муҳим.

ТУЗИЛИШ
- Render Free: Python API ва веб интерфейс, бепул onrender.com манзили, HTTPS.
- Supabase Free: PostgreSQL база. Render локал SQLite'ни сақлаш учун ИШЛАТМАНГ.
- Биринчи кириш: admin логин ва ўзингиз киритган BOOTSTRAP_ADMIN_PASSWORD.
- Киргандан кейин директор паролини алмаштиринг.

ҚАДАМЛАР
1) https://supabase.com/dashboard да Free лойиҳа очинг, ўз паролингизни белгиланг.
   Project Settings / Connect бўлимида DATABASE_URL учун Session pooler URI (порт 5432) олинг.
   Direct (db.PROJECT_REF.supabase.co:5432) IPv6 бўлиши мумкин; Render'дан
   IPv4 ишлаши учун Session pooler тавсия қилинади.
   Паролда махсус белгилар бўлса URI-percent-encode қилинг.
   URI охирига ?sslmode=require қўшинг (агар параметрлар бўлса &sslmode=require).
2) Github'да ХУСУСИЙ репозиторийга ушбу файлларни юкланг. .env, парол ва база URL'ни юкламаслик керак.
3) https://dashboard.render.com да New > Blueprint ёки New > Web Service.
   Blueprint вариантида render.yaml'ни танланг ва Free режасини тасдиқланг.
   GitHub репони Render аккаунтига боғланг.
4) Render > Environment:
   DATABASE_URL = Supabase'дан олинган Session pooler URI (махфий).
   BOOTSTRAP_ADMIN_PASSWORD = ўзига хос камида 12 белгили, ҳарф ва рақамли кучли парол.
   BOOTSTRAP_ADMIN_USERNAME = admin
   REQUIRE_POSTGRES = 1
   HTTPS_ONLY = 1
5) Deploy ни бошланг. Render манзили: https://avtomaktab-boshqaruv-...onrender.com
   (манзил аккаунтда ЯРАТИЛГАНИДА кўринади; олдиндан маълум эмас).
6) Логин admin; парол сиз 4-қадамда киритган пароль.
   Директор аккаунти биринчи марта базага ёзилганидан кейин bootstrap пароль
   Render Environment'дан олиб ташланиши мумкин.
7) Гуруҳ/ўқувчи/тўловлардан демо билан фойдаланиб кўринг.
   Ҳақиқий маълумотларни синов муҳитига киритмасликни тавсия қиламиз.
8) Директор логинидан Маълумотлар экспорти (JSON) орқали тез-тез сақланг.
   JSON export АККАУНТ, ПАРОЛ, АУДИТ ва СЕССИЯларни ўз ичига олмайди!

ХАРАЖАТ ВА ХАВФЛАР
- Render Free 15 дақиқа бекор турса ухлайди ва биринчи сўров кечикиши мумкин.
- Render Free сервер файловая система вақтинчалик: SQLite у ерда сақланмасин.
- Supabase Free лойиҳаси фаолсиз қолса паузага тушиши мумкин ва одатда
  автоматик архив/backups кафолатланмайди. Меъёрлар ўзгариши мумкин.
- Пул талаб қилмасдан ишлатиш учун Free тарифларни танланг, платёж усулини
  қўшсангиз лимитдан ошишда тўлов эҳтимолини текширинг.
- Иккита платформа ҳам ташқи провайдер; шахсий маълумотлар сақланади.
  Ўқувчилар телефони ва тўлов маълумотлари учун ҳуқуқий/ташкилий хавфни ҳисобга олинг.
- PostgreSQL бўйича ташқи live тест бу пакет тайёрланганда бажарилмаган;
  ҳақиқий ишлашни deploy пайтида текшириш керак.

LOKAL ТЕСТ
Python 3.12+ билан: python -m unittest discover -s tests -v
SQLite test учун DATABASE_URL ўзгарувчиси БЎШ бўлсин.
Postgres URL билан локалда: pip install -r requirements.txt; DATABASE_URL=... python server.py serve

ХАВФСИЗЛИК
- DATABASE_URL ва BOOTSTRAP_ADMIN_PASSWORD ни ҳеч кимга чатда ёзманг.
- Тест бўлиб турса ҳам паролни ходимлар билан битта қилиб ишлатманг.
- Free demo — ишлаб чиқариш (production) учун тавсия қилинмайди.

МУҲИМ: HTTPS сервер ҳатто DATABASE_URL жўнатилмаса ишга тушиб кетмаслиги
учун REQUIRE_POSTGRES=1 созламаси киритилди. Аввал database уланишини текширинг.
