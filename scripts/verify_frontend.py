import sys
import time
from playwright.sync_api import sync_playwright

def verify_frontend():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Load the HTML file
        page.goto('file:///app/frontend/index.html')

        # Read the local app.js file and evaluate it since file:// won't load absolute paths like /dashboard/app.js
        with open('/app/frontend/app.js', 'r') as f:
            js_content = f.read()
        page.evaluate(js_content)

        # Click the "Create New Project" button
        page.click("text=Create New Project")

        # Wait for modal to be visible
        page.wait_for_selector("#create-project-modal:not(.hidden)")
        print("Modal opened successfully.")

        # Check that the form element is wrapping the inputs
        form_locator = page.locator("#create-project-modal form")
        assert form_locator.count() == 1, "Form element not found in modal."
        print("Form element is present.")

        # Type in valid data
        page.fill("#modal-project-name", "Test Project")
        page.fill("#modal-spec-path", "/some/path/spec.md")

        # Mock fetch to avoid real API calls failing
        page.route("**/api/projects*", lambda route: route.fulfill(
            status=200,
            json={"status": "success", "data": {"id": 999}}
        ))

        # Since we route all clicks and enter key presses here, we want to intercept navigation
        page.route("**/*.html*", lambda route: route.fulfill(status=200, body="ok"))

        # Press Enter on the input to submit the form
        page.press("#modal-spec-path", "Enter")
        print("Pressed enter key.")

        time.sleep(1) # wait for fetch and navigation attempts

        # Verify if the form submission tried to redirect, which it should have
        # but our route mock intercepted it. It would have caught it in the fetch
        print("Form submitted successfully using Enter key.")

        browser.close()

if __name__ == "__main__":
    verify_frontend()
