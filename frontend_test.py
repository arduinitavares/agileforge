import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(record_video_dir=".")
        page = await context.new_page()

        # We need to serve the local app since absolute paths like /dashboard/app.js
        # won't resolve correctly over file://. We already have python3 -m http.server 8000 running.

        await page.goto('http://localhost:8000/frontend/index.html')
        await page.wait_for_timeout(500)

        # To resolve the 404 for /dashboard/app.js due to simple HTTP server root mismatch:
        with open("/app/frontend/app.js", "r") as f:
             app_js = f.read()
        await page.evaluate(app_js)

        # Click "Create New Project"
        await page.click('button:has-text("Create New Project")')
        await page.wait_for_timeout(1000)

        # Try to submit the form empty to trigger HTML5 validation
        await page.click('button:has-text("Create Project")')
        await page.wait_for_timeout(1000)

        # Fill out form
        await page.fill('#modal-project-name', 'Test Project Validation')
        await page.fill('#modal-spec-path', '/fake/path/spec.md')
        await page.wait_for_timeout(1000)

        # Take a screenshot showing the filled form before submission
        await page.screenshot(path='form_validation_screenshot.png')

        # Submit the form using Enter key to verify form submission works
        await page.press('#modal-spec-path', 'Enter')
        await page.wait_for_timeout(1000)

        await context.close()
        await browser.close()

asyncio.run(run())
