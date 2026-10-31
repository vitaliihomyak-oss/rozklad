"""Збирач розкладу ЛДУБЖД → schedule.json.

Запуск:  python collect.py [full | quick | auto]
  full  — 7 днів (сьогодні + 6)
  quick — сьогодні й завтра
  auto  — за київським часом: до 08:00 і з 19:00 → full, удень → quick
          (так не треба міняти розклад запусків при переході на літній час)

Файл зберігає сирі рядки з сайту без змін. Фільтрація, підгрупи й скорочення — у віджеті.
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

DAYS_FULL = 7
DAYS_QUICK = 2
FULL_BEFORE_HOUR = 8   # режим auto: запуск раніше 08:00 → full
FULL_FROM_HOUR = 19    # режим auto: запуск з 19:00 → full

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


_dumped = 0


def _query(browser, d, group):
    """Один запит розкладу за дату d для назви групи group. Повертає рядки лише за цю дату."""
    global _dumped
    page = browser.new_page()
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=90000)
        field = page.get_by_placeholder("Назва групи")
        field.wait_for(timeout=60000)
        field.click()
        # набираємо як людина, щоб спрацювали обробники й підказки сайту
        field.press_sequentially(group, delay=80)
        page.wait_for_timeout(1500)
        dates = page.get_by_placeholder("дд.мм.рррр")
        for i in (0, 1):
            dates.nth(i).fill(d)
            dates.nth(i).blur()
        page.get_by_text("Показати розклад занять").first.click()
        page.wait_for_timeout(PAUSE_MS)
        rows = [r for r in page.evaluate(JS) if r[0] == d]
        if not rows and _dumped < 2:
            _dumped += 1
            inputs = page.evaluate(
                "() => [...document.querySelectorAll('input,select,textarea')]"
                ".map(e => [e.tagName, e.type || '', e.name || '', e.id || '', e.placeholder || '', e.value || ''].join('|'))"
            )
            body = page.evaluate("() => document.body.innerText")
            print(f"[debug {d} група={group!r}] поля: {inputs}")
            print(f"[debug {d}] текст: {' '.join(body.split())[:700]!r}")
        return rows
    finally:
        page.close()


def fetch_day(browser, day):
    """Один день. Пробує назву групи як є, потім малими літерами (на випадок чутливості до регістру)."""
    d = day.strftime("%d.%m.%Y")
    for g in dict.fromkeys([GROUP, GROUP.lower()]):
        rows = _query(browser, d, g)
        if rows:
            print(f"{d}: знайдено з назвою групи {g!r}")
            return rows
    return []


def load_old():
    try:
        with open(OUT, encoding="utf-8") as f:
            rows = json.load(f).get("rows", [])
        return [r for r in rows if isinstance(r, list) and len(r) >= 4]
    except Exception:
        return []


def build_data(old_rows, fresh, now):
    """Старі дані + свіжі дні (свіжий день повністю замінює старий).
    Дні, яких не вдалось отримати, і дні поза діапазоном запуску лишаються як були.
    Минулі дні відкидаються."""
    by_day = {}
    for r in old_rows:
        by_day.setdefault(r[0], []).append(r)
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


def publish(fresh, failed, now):
    data = build_data(load_old(), fresh, now)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    run = lambda *a: subprocess.run(a, check=True)
    run("git", "config", "user.name", "rozklad-collector")
    run("git", "config", "user.email", "rozklad-collector@users.noreply.github.com")
    run("git", "add", OUT)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
        run("git", "commit", "-m", "schedule " + now.strftime("%d.%m %H:%M"))
        run("git", "pull", "--rebase")
        run("git", "push")
    print(f"published {len(data['rows'])} rows; days ok: {len(fresh)}, failed: {failed}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    now = datetime.now(TZ)
    if mode == "auto":
        mode = "full" if (now.hour < FULL_BEFORE_HOUR or now.hour >= FULL_FROM_HOUR) else "quick"
    if mode not in ("full", "quick"):
        sys.exit("usage: python collect.py [full|quick|auto]")
    count = DAYS_FULL if mode == "full" else DAYS_QUICK
    print(f"mode: {mode}, days: {count}, kyiv time: {now:%d.%m.%Y %H:%M}")

    from playwright.sync_api import sync_playwright

    fresh, failed = {}, []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            # помилка одного дня не ламає решту — для нього лишаються старі дані
            for i in range(count):
                d = now + timedelta(days=i)
                dk = d.strftime("%d.%m.%Y")
                try:
                    fresh[dk] = fetch_day(browser, d)
                    print(f"{dk}: {len(fresh[dk])} rows")
                except Exception as e:
                    failed.append(dk)
                    print("day failed:", dk, e)
        finally:
            browser.close()

    if not fresh:
        print("ERROR: не вдалось отримати жоден день")
        sys.exit(1)
    publish(fresh, failed, now)


if __name__ == "__main__":
    main()
