# CareerPilot Israel (JobiBot)

מערכת אישית לחיפוש עבודה בישראל: איתור משרות, התאמה לפי מסלולי קריירה, התאמת קורות חיים קיימים בלי להמציא עובדות, הכנת חומרי הגשה, ותיעוד מלא של כל הגשה.

המערכת רצה **על המחשב המקומי בלבד ובעלות אפס** (ראו [COST_POLICY.md](COST_POLICY.md)). כברירת מחדל היא במצב **DRY_RUN**: היא מוצאת משרות, מדרגת, מכינה מסמכים ומדמה הגשה, אבל **לא שולחת שום מועמדות אמיתית**.

## מה יש בגרסה הזו (MVP)

- ייבוא קורות החיים המקוריים (DOCX) ושמירתם כעותק לקריאה בלבד. הקובץ המקורי לא משתנה לעולם.
- פרופיל עובדות מאומתות: כל עובדה עם מקור. עובדות שלא מופיעות בקורות החיים מסומנות "דורש אישור" ולא נכנסות לשום מסמך עד שמאשרים אותן.
- ארבעה מסלולי קריירה עם ציון נפרד לכל אחד: Data/BI, GRC ואבטחת מידע, AI ואוטומציה, שיווק, ועוד "תפקידים סמוכים".
- מקורות משרות חינמיים ומותרים: לוחות Greenhouse ו-Lever ציבוריים, ייבוא ידני (JSON/CSV), וקבצי ‎.eml של התראות משרות.
- נרמול בעברית ובאנגלית, זיהוי כפילויות בין אתרים, ושמירת היסטוריה גם כשמשרה יורדת.
- ציון 0 עד 100 עם הסבר מלא, מסננים קשיחים שגוברים על הציון, ומשקלים שניתן לשנות.
- גרסת קורות חיים מותאמת לכל משרה: מזהה גרסה, SHA-256, יומן שינויים (מקור, תיקון, סיבה), ו-PDF דרך LibreOffice אם מותקן.
- מכתב מקדים בשפת המשרה ותשובות לשאלות רק מנתונים מאומתים. שאלות רגישות (שכר, אישור עבודה, הצהרות, בריאות, מגדר וכו') תמיד ממתינות לתשובה שלך.
- מעקב מלא: מצב, ציר זמן, ניסיונות הגשה עם שעה מקומית, ראיות לאישור, תזכורות מעקב, וצילום בלתי ניתן לשינוי של הקבצים שנשלחו.
- לוח בקרה מקומי (Streamlit), דוח יומי, ייצוא CSV/JSON, גיבוי ומחיקת היסטוריה.

## דרישות

- Python 3.12 ומעלה
- Git
- אופציונלי: LibreOffice (להפקת PDF)

אין צורך בשום מפתח API, חשבון בתשלום או שירות ענן.

## התקנה

```bash
git clone https://github.com/katya-alferov/JobiBot.git
cd JobiBot
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate
pip install -e ".[dev]"
```

## הגדרה ראשונית

1. להעתיק את קובץ ההגדרות:
   ```bash
   cp .env.example .env
   cp config/profile.example.yaml config/profile.yaml
   ```
2. לערוך את `config/profile.yaml`: להכניס את הכתובות **המדויקות** של האתר האישי ושל LinkedIn. שדה ריק נשאר ריק עד שמאמתים אותו.
3. לבדוק שההגדרות חינמיות ובטוחות:
   ```bash
   careerpilot check
   ```
4. ליצור את מסד הנתונים ולייבא את קורות החיים הראשיים:
   ```bash
   careerpilot init
   careerpilot import-cv "path/to/Katya_Alferov_CV.docx"
   ```
   גרסה ישנה יותר אפשר לשמור כגרסת השוואה בלבד:
   ```bash
   careerpilot import-cv "path/to/older_CV.docx" --reference
   careerpilot compare-cv older_CV.docx Katya_Alferov_CV.docx
   ```
5. לעבור על עובדות שדורשות אישור:
   ```bash
   careerpilot facts
   careerpilot confirm-fact <id>              # אישור
   careerpilot confirm-fact <id> --reject     # דחייה
   ```
6. לבדוק שהקישורים עובדים (LinkedIn חוסם לעיתים בדיקות אוטומטיות, ואז אפשר לפתוח את הקישור בדפדפן ולאשר ידנית):
   ```bash
   careerpilot check-links
   careerpilot confirm-link linkedin
   ```
7. להגדיר העדפות ב-`config/preferences.yaml` (שכר מינימלי, מיקומים, ימי משרד, סוג משרה, מעסיקים לא רצויים). ערך `null` פירושו "לא ידוע" והמערכת לא מניחה אותו.

## שימוש יומיומי

```bash
careerpilot import-jobs data/imports/my_jobs.csv   # ייבוא ידני
careerpilot import-jobs data/imports/               # תיקייה עם קבצי ‎.eml של התראות
careerpilot match                                    # דירוג כל המשרות לכל מסלול
careerpilot prepare <job_id>                         # הכנת קורות חיים, מכתב ותשובות
careerpilot answer <app_id> "<שאלה>" "<התשובה שלך>"
careerpilot submit <app_id>                          # ב-DRY_RUN: הדמיה בלבד
careerpilot mark-submitted <app_id> "הוגש באתר החברה, התקבל מייל אישור"
careerpilot status <app_id> interview --note "שיחת טלפון ביום א'"
careerpilot run-daily                                # כל השלבים ברצף + דוח יומי
careerpilot dashboard                                # לוח הבקרה בדפדפן
```

לוחות Greenhouse או Lever של מעסיקים מוסיפים ב-`config/sources.yaml`.

### דוגמה עם נתונים סינתטיים

```bash
careerpilot import-jobs tests/fixtures/synthetic_jobs.json --synthetic
careerpilot match
```

## תזמון

המערכת לא תלויה בלוח הבקרה. אפשר להריץ:

- **Linux/macOS (cron)**, כל יום ב-08:00:
  ```
  0 8 * * * cd /path/to/JobiBot && .venv/bin/careerpilot run-daily
  ```
- **Windows**: ב-Task Scheduler ליצור משימה יומית שמריצה `C:\path\to\JobiBot\.venv\Scripts\careerpilot.exe run-daily` מתוך תיקיית הפרויקט.
- או להשאיר טרמינל פתוח עם `careerpilot scheduler`.

**חשוב:** תהליך מקומי לא רץ כשהמחשב כבוי או במצב שינה. אין שימוש בשרת ענן בתשלום. אם תידרש הפעלה רציפה, נבדוק רק אפשרויות חינמיות באמת שלא דורשות פרטי תשלום, ונתעד את המגבלות שלהן.

## הגשה אמיתית

בגרסה הזו **אין הגשה אוטומטית אמיתית** לאף מעסיק. לכל משרה אמיתית המערכת מכינה את המסמכים ונותנת את הקישור הרשמי, וההגשה נעשית ידנית (ואז מתעדים אותה עם `mark-submitted`).

כדי שהגשה אוטומטית תהיה אפשרית בעתיד, כל התנאים חייבים להתקיים יחד: מתאם הגשה חינמי ומותר שנבדק, דומיין ברשימת `APPROVED_SUBMISSION_DOMAINS`, `DRY_RUN=false`, `SUBMISSIONS_KILL_SWITCH=false`, וכל הבדיקות הדטרמיניסטיות עוברות (מסננים, אימות משרה, קורות חיים תקינים, אין שאלות פתוחות, אין CAPTCHA, אין כפילות, מגבלת כמות יומית). שום מודל שפה לא יכול לאשר הגשה.

מתג הכיבוי (`SUBMISSIONS_KILL_SWITCH=true`) חוסם כל הגשה אמיתית מיד. אפשר להפעיל אותו גם מלוח הבקרה.

## גיבוי, ייצוא ומחיקה

```bash
careerpilot export --format both    # data/exports/
careerpilot backup                  # data/backups/<תאריך>/
careerpilot delete-history          # מחיקה לצמיתות, דורש הקלדת אישור
```

כל המידע האישי (קורות חיים, מסמכים, מסד נתונים, דוחות) נשמר בתיקייה `data/`, שלא נכנסת ל-Git. גם `.env` ו-`config/profile.yaml` לא נכנסים ל-Git. **המאגר ב-GitHub ציבורי**, ולכן אין בו שום מידע אישי, והבדיקות משתמשות רק בנתונים סינתטיים.

## בדיקות

```bash
pytest
```

הבדיקות משתמשות רק במשרות סינתטיות ובאתר מדומה (`mock-ats.careerpilot.test`). הן לא שולחות שום דבר לאף אחד.

## מבנה הפרויקט

```
careerpilot/
  profile/        ייבוא קורות חיים, עובדות מאומתות, קישורים והעדפות
  sources/        מתאמי מקורות משרות (חינמיים ומותרים בלבד)
  ingestion/      נרמול עברית/אנגלית וזיהוי כפילויות
  matching/       מסננים קשיחים, ציון לכל מסלול, ממשק מודל מקומי אופציונלי
  documents/      קורות חיים מותאמים, יומן שינויים, מכתב ותשובות
  applications/   מצבים, שער הרשאה, מתאמי הגשה, מעקב והיסטוריה
  storage/        מודלים (SQLAlchemy 2), מסד נתונים, יומן ביקורת
  dashboard/      לוח בקרה ב-Streamlit
  scheduler/      הרצה יומית
  notifications/  דוחות מקומיים
config/           הגדרות, מסלולים, אוצר מיומנויות, מקורות ומלאי שילובים
alembic/          מיגרציות מסד נתונים
tests/            בדיקות אוטומטיות
```

## מסמכים נוספים

- [COST_POLICY.md](COST_POLICY.md): מדיניות עלות אפס ואיך היא נאכפת בקוד
- [DEPENDENCIES.md](DEPENDENCIES.md): מלאי תלויות, עלויות ומגבלות
- [SOURCES.md](SOURCES.md): מקורות נתמכים, מגבלות גישה ואבן הדרך הבאה
