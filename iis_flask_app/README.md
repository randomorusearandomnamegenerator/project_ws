# IIS-Ready MOM Scraper

This folder is an IIS-ready Flask version of the scraper logic, packaged for internal IIS hosting.

It is not the Streamlit app, but it mirrors the refined workflow as closely as possible for a server-hosted environment: PDFs are fetched on page load, retrieval timestamps are shown, the ZIP download is available immediately, and the results table keeps the same audit-oriented fields.

## Included

- `app.py` - Flask app adapted from the refined scraper logic
- `templates/index.html` - web UI with the same input and output sections as the Streamlit version
- `static/styles.css` - styling tuned for the internal portal layout
- `wsgi.py` - IIS/WFastCGI entrypoint
- `web.config` - IIS handler config
- `data/pdfs` and `data/downloads` - runtime folders

## Local run

```bash
cd iis_flask_app
pip install -r requirements.txt
python app.py
```

Then open `http://127.0.0.1:5000`.

On first load, the page will fetch the MOM PDFs, show retrieval time in Singapore time, and expose the ZIP download. Enter UENs and run the scraper to populate the result tables.

## IIS deployment notes

1. Install Python on the IIS server.
2. Create a virtual environment inside or beside this folder.
3. Install the requirements from `requirements.txt`.
4. Edit `web.config` and replace the `scriptProcessor` paths with:
   - the full path to the virtualenv Python executable
   - the full path to `wfastcgi.py` in that virtualenv
5. Point IIS to this folder as the site root.
6. Make sure the IIS app pool allows the account to read/write `data/pdfs` and `data/downloads`.

The Flask version keeps the same core PDF download, parsing, ZIP download, and audit timestamp behavior, but it intentionally drops the unavailable MOM API fields and keeps the logic server-side for internal hosting.

If you want the app to run under a sub-application or virtual directory, the Flask app already uses relative paths from its own folder.
