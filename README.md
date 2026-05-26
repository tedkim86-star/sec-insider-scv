# SEC Insider Form 4 CSV Generator

This repository generates `data/insider_raw_data.csv` for tickers listed in `tickers.txt`.

## How to use

1. Upload these files to a new GitHub repository.
2. Edit `tickers.txt` and put one ticker per line.
3. In GitHub, go to Actions and run `Update SEC insider CSV` manually once.
4. Use the raw CSV URL in Google Sheets with `IMPORTDATA`.

Example Google Sheets formula:

```excel
=IMPORTDATA("https://raw.githubusercontent.com/YOUR_ID/YOUR_REPO/main/data/insider_raw_data.csv")
```

The workflow also runs daily at 09:10 KST (00:10 UTC).

## Notes

- Source: SEC EDGAR public data APIs and filing XML documents.
- The script respects a request delay and uses a User-Agent header.
