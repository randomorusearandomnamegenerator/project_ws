# WSH Scraper (Intranet)

This Flask app downloads MOM PDFs, extracts data by UEN, and shows which companies meet the criteria.

## Run locally

1. Create a virtual environment and install dependencies:

```
pip install -r requirements.txt
```

2. Start the app:

```
python app.py
```

3. Open the site at http://127.0.0.1:5000

## SharePoint embed notes

The app is designed for iframe embedding. If your host sets security headers, make sure the SharePoint domain is allowed in `Content-Security-Policy`.

test UEN: 199403976M, 53146389C