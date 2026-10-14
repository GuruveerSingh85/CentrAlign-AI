# Browser automation

The `browser.inspect` and `browser.interact` tools use Playwright Chromium. They run headless by default. To show the Chromium window on the machine running the backend, set `BROWSER_HEADLESS=false` before starting the backend. The backend process needs access to a desktop session; a headless server cannot display a window. Idle task sessions close after `BROWSER_SESSION_IDLE_TIMEOUT_SECONDS` (one hour by default).

The browser tool accepts HTTPS URLs only for hosts listed in `WEB_ALLOWED_HOSTS`. Add comma-separated site hostnames there (for example, `example.com,portal.example.com`). It also accepts HTTP URLs on loopback addresses (`localhost`, `127.0.0.1`, and `::1`) so it can inspect a local development site such as Vite at `http://localhost:5173`. Other HTTP URLs and private network addresses remain blocked. Set `BROWSER_AUTO_APPROVE=true` to let supported tasks start automatically and allow browser interactions on these configured hosts without a separate approval pause. The policy still writes an approval audit record for each interaction. The default local tool role includes `web:read` and `web:interact`; if `TOOL_ROLE_GRANTS` is configured, explicitly grant both scopes to the appropriate role. Other sensitive tools, including vendor registration, continue to require human approval.

Create a task in the dashboard with the target URL and desired outcome. Supported plans start automatically, and the agent inspects the rendered page and replans from the observed content. Browser calls in that task reuse the same page and login/session state. If the site requires sign-in, use the visible browser window to sign in yourself; never enter passwords into task text. The agent inspects the page again before reporting the result. The task's browser session closes when the task finishes, idles past its configured timeout, or the backend shuts down. Without `BROWSER_AUTO_APPROVE=true`, website actions still pause for approval.

Install the Python requirements and Playwright Chromium runtime before using the tool:

```powershell
python -m pip install -r backend/requirements.txt
python -m playwright install chromium
```

Start the frontend and backend in separate terminals. Set `BROWSER_HEADLESS=false` in the backend terminal if you want the browser window to appear. If the backend is launched by a service, container, or remote host, the window appears there (if a desktop exists), not on the machine running the frontend.
