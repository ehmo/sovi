"""Test mail.com signup — full flow through password to completion."""
from playwright.sync_api import sync_playwright
import time, random, string

def gen_pw():
    pw = [random.choice(string.ascii_uppercase), random.choice(string.ascii_lowercase),
          random.choice(string.digits), random.choice("!@#$%")]
    pw.extend(random.choices(string.ascii_letters + string.digits + "!@#$%", k=12))
    random.shuffle(pw)
    return "".join(pw)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )

    page.goto("https://signup.mail.com/", wait_until="load", timeout=30000)
    time.sleep(5)

    # Step 1: Name + DOB
    first = "Sarah"
    last = "Thompson"
    page.fill("#given-name", first)
    page.fill("#family-name", last)
    page.fill("#bday-month", "06")
    page.fill("#bday-day", "15")
    page.fill("#bday-year", "1993")
    page.evaluate("document.querySelectorAll('button[type=button]')[0].click()")
    time.sleep(5)

    # Step 2: Click first suggestion
    chosen = page.evaluate("""(() => {
        const row = document.querySelector('onereg-suggestion-item-advanced');
        if (row) {
            const text = row.querySelector('.onereg-suggestion-item-advanced__text');
            row.click();
            return text ? text.textContent : 'unknown';
        }
        return null;
    })()""")
    print(f"1. Email: {chosen}")
    time.sleep(5)

    # Step 3: Salutation + Country + State
    page.evaluate("""(() => {
        const radios = document.querySelectorAll('input[name=salutation]');
        if (radios.length > 0) { radios[0].checked = true; radios[0].dispatchEvent(new Event('change', {bubbles: true})); }
        const country = document.querySelector('#country');
        if (country) { country.value = 'US'; country.dispatchEvent(new Event('change', {bubbles: true})); }
    })()""")
    time.sleep(2)
    page.evaluate("""(() => {
        const region = document.querySelector('#region');
        if (region && region.options.length > 1) { region.value = region.options[1].value; region.dispatchEvent(new Event('change', {bubbles: true})); }
    })()""")
    time.sleep(1)
    page.evaluate("document.querySelector('[data-test=progress-meter-next]').click()")
    time.sleep(5)
    print("2. Salutation + Country done")

    # Step 4: Password
    password = gen_pw()
    page.fill("#password", password)
    time.sleep(0.5)
    page.fill("#confirm-password", password)
    time.sleep(0.5)
    page.evaluate("document.querySelector('[data-test=progress-meter-next]').click()")
    time.sleep(5)
    page.screenshot(path="/tmp/mc_step5.png")
    print(f"3. Password set: {password}")

    # Step 5: What's next?
    visible = page.evaluate("""(() => {
        return Array.from(document.querySelectorAll('input, select, button, textarea, img'))
            .filter(el => el.offsetParent !== null)
            .map(el => ({
                tag: el.tagName,
                name: el.name || '',
                type: el.type || '',
                id: el.id || '',
                dt: el.getAttribute('data-test') || '',
                src: el.src ? el.src.substring(0, 80) : '',
            }));
    })()""")
    print("\n=== STEP 5 elements ===")
    for el in visible:
        print(f"  {el['tag']} name={el['name']} type={el['type']} id={el['id']} dt={el['dt']}")

    h2s = page.evaluate("""(() => {
        return Array.from(document.querySelectorAll('h1, h2, h3, p'))
            .filter(el => el.offsetParent !== null)
            .map(el => el.textContent.trim().substring(0, 100));
    })()""")
    print("\nText:")
    for t in h2s:
        if t:
            print(f"  {t}")

    browser.close()
    print(f"\nCredentials: {chosen} / {password}")
