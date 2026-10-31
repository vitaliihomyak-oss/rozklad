"""Збирач розкладу ЛДУБЖД → schedule.json.

Запуск:  python collect.py            (на GitHub Actions: з комітом і пушем)
         python collect.py --no-git   (на ПК: лише записати schedule.json)

Запит робиться БЕЗ дат: сайт сам віддає розклад групи на тиждень
(у суботу — уже на наступний). Один запит замість семи.
Файл зберігає сирі рядки з сайту без змін. Фільтрація, підгрупи й скорочення — у віджеті.
Позиційні аргументи (full / quick / auto) приймаються й ігноруються — для сумісності з workflow.
"""
import json
import subprocess
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

URL = "https://rozklad.ldubgd.edu.ua/cgi-bin/timetable.cgi?n=700"
GROUP = "ПБ23к"
OUT = "schedule.json"
TZ = ZoneInfo("Europe/Kyiv")
PAUSE_MS = 6000  # сайт повільний — чекаємо 6 с після натискання
ATTEMPTS = 2     # скільки разів пробувати, якщо сайт повернув порожньо
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

JS = """() => {
  const out = []; let cur = null;
  for (const el of document.body.querySelectorAll('*')) {
    if (el.tagName === 'TR') {
      const c = [...el.children].map(x => x.innerText.trim());
      if (c.length >= 3 && cur) out.push([cur, c[0], c[1], c[2]]);
    } else if (!el.closest('tr') && el.children.length <= 2) {
      const m = (el.innerText || '').trim().match(/^(\\d{2}\\.\\d{2}\\.\\d{4})\\s+\\S+$/);
      if (m) cur = m[1];
    }
  }
  return out;
}"""


def parse_date(k):
    return datetime.strptime(k, "%d.%m.%Y")


def fetch_week(browser):
    """Один запит без дат. Повертає всі рядки, які віддав сайт."""
    for attempt in range(1, ATTEMPTS + 1):
        context = browser.new_context(user_agent=USER_AGENT, locale="uk-UA", timezone_id="Europe/Kyiv")
        page = context.new_page()
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=90000)
            group = page.locator("input[name=group]")
            group.wait_for(timeout=60000)
            group.click()
            # набираємо як людина, щоб спрацювали обробники сайту
            group.press_sequentially(GROUP, delay=80)
            page.wait_for_timeout(1500)
            for name in ("sdate", "edate"):  # дати порожні → сайт дає розклад на тиждень
                page.locator(f"input[name={name}]").fill("")
            page.get_by_text("Показати розклад занять").first.click()
            page.wait_for_timeout(PAUSE_MS)
            rows = page.evaluate(JS)
            if rows:
                return rows
            body = page.evaluate("() => document.body.innerText")
            print(f"спроба {attempt}: рядків немає; текст: {' '.join(body.split())[:300]!r}")
        finally:
            context.close()
    return []


def load_old():
    try:
        with open(OUT, encoding="utf-8") as f:
            rows = json.load(f).get("rows", [])
        return [r for r in rows if isinstance(r, list) and len(r) >= 4]
    except Exception:
        return []


def build_data(old_rows, new_rows, now):
    """Старі дані + свіжі. Дні між першим і останнім днем відповіді сайту вважаються
    свіжими повністю (день без пар лишається порожнім), решта днів зі старих даних
    зберігається. Минулі дні відкидаються."""
    by_day = {}
    for r in old_rows:
        by_day.setdefault(r[0], []).append(r)
    fresh = {}
    for r in new_rows:
        fresh.setdefault(r[0], []).append(r)
    if fresh:
        first, last = min(map(parse_date, fresh)), max(map(parse_date, fresh))
        for i in range((last - first).days + 1):
            fresh.setdefault((first + timedelta(days=i)).strftime("%d.%m.%Y"), [])
    by_day.update(fresh)
    today = now.date()
    rows = []
    for d in by_day:
        try:
            if parse_date(d).date() >= today:
                rows += by_day[d]
        except ValueError:
            pass
    rows.sort(key=lambda r: (parse_date(r[0]), str(r[1]).zfill(3)))
    return {"generated": now.isoformat(), "group": GROUP, "rows": rows}


def publish(new_rows, now, use_git):
    data = build_data(load_old(), new_rows, now)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    days = sorted({r[0] for r in new_rows}, key=parse_date)
    print(f"записано {len(data['rows'])} рядків; з сайту: {len(new_rows)} за дні {days}")
    if not use_git:
        return
    run = lambda *a: subprocess.run(a, check=True)
    run("git", "config", "user.name", "rozklad-collector")
    run("git", "config", "user.email", "rozklad-collector@users.noreply.github.com")
    run("git", "add", OUT)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
        run("git", "commit", "-m", "schedule " + now.strftime("%d.%m %H:%M"))
        run("git", "pull", "--rebase")
        run("git", "push")


def main():
    use_git = "--no-git" not in sys.argv
    now = datetime.now(TZ)
    print(f"kyiv time: {now:%d.%m.%Y %H:%M}")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            rows = fetch_week(browser)
        finally:
            browser.close()

    if not rows:
        print("ERROR: сайт не повернув жодного рядка; schedule.json не змінено")
        sys.exit(1)
    publish(rows, now, use_git)


if __name__ == "__main__":
    main()
