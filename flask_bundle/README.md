# WSH Scraper Bundle

This folder is a self-contained copy of the Flask version of the MOM scraper.

## Run locally

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Start the app from this folder:

```bash
python app.py
```

4. Open http://127.0.0.1:5000

## Folder layout

- `app.py` - Flask entrypoint
- `templates/index.html` - UI template
- `static/styles.css` - styling
- `data/pdfs` - downloaded PDFs
- `data/downloads` - generated ZIP files
