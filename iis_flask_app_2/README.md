# IIS-Ready MOM Scraper

This folder is the IIS deployment package for the latest Flask version of the scraper.

It is configured for `HttpPlatformHandler`, which starts a local Python process and proxies IIS traffic to it. The app keeps the same user-facing fields and outputs as the refined Streamlit version: UEN input, demerit threshold, fatal cases field, BUS exclusion, ZIP download, resolved PDF URLs, timestamps, and the two result tables.

## Included

- `app.py` - Flask app with the scraper logic
- `run_waitress.py` - startup script used by IIS
- `templates/index.html` - UI template
- `static/styles.css` - styling
- `web.config` - IIS `HttpPlatformHandler` config
- `requirements.txt` - Python dependencies
- `data/pdfs` and `data/downloads` - runtime folders

## Local run

```bash
cd iis_flask_app
pip install -r requirements.txt
python app.py
```

Then open `http://127.0.0.1:5000`.

## IIS setup

1. Install IIS with the following Windows features enabled:
   - Web Server
   - Static Content
   - HTTP Errors
   - Request Filtering
   - CGI
2. Install the Python dependencies into a virtual environment:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

3. Make sure `waitress` is installed from `requirements.txt`.
4. Install the Microsoft HttpPlatformHandler module on the IIS server if it is not already present.
5. Edit `web.config` and replace `C:\PATH\TO\PYTHON.EXE` with the full path to the virtualenv Python executable.
6. Keep `arguments="run_waitress.py"` unless you rename the startup script.
7. Point IIS at the `iis_flask_app` folder as the site root.
8. Grant the IIS application pool identity read/write access to `data\pdfs`, `data\downloads`, and `logs`.
9. Make sure outbound internet access is allowed so the app can fetch the MOM PDF links.

## Notes

- `HttpPlatformHandler` is fine for an internal IIS deployment, but it is a Windows/IIS-specific hosting model and is less portable than a standard WSGI reverse proxy.
- The app binds to the local port supplied by IIS through the `HTTP_PLATFORM_PORT` environment variable.
- If IIS does not already have `HttpPlatformHandler`, the module must be installed before the site will start.
